#!/usr/bin/env python3
"""ORCA Money Bot: safe, extensible Binance paper-trading core.

Live orders are deliberately opt-in and require LIVE_TRADING_CONFIRM=I_UNDERSTAND_RISK.
No secret is read from source code; use environment variables or a secret manager.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import json
import logging
import math
import os
import signal
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

LOG = logging.getLogger("orca")


class BinanceError(RuntimeError):
    """Structured Binance API failure, preserving the exchange error body."""
    def __init__(self, status: int, code: int | None, message: str, path: str):
        super().__init__(f"{path} -> HTTP {status} Binance code={code}: {message}")
        self.status, self.code, self.message, self.path = status, code, message, path

    @property
    def is_fatal(self) -> bool:
        return self.status in (401, 403, 418) or self.code in (-1022, -2014, -2015)


class Mode(str, Enum):
    PAPER = "paper"
    TESTNET = "testnet"
    LIVE = "live"


class Signal(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


@dataclass(frozen=True)
class Config:
    mode: Mode = field(default_factory=lambda: Mode(os.getenv("TRADING_MODE", "paper").lower()))
    symbols: tuple[str, ...] = field(default_factory=lambda: tuple(s.strip().upper() for s in os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT").split(",") if s.strip()))
    quote_asset: str = field(default_factory=lambda: os.getenv("QUOTE_ASSET", "USDT"))
    initial_equity: float = field(default_factory=lambda: float(os.getenv("PAPER_START_BALANCE", "10000")))
    risk_per_trade_pct: float = field(default_factory=lambda: float(os.getenv("RISK_PER_TRADE_PCT", "0.25")))
    max_daily_loss_pct: float = field(default_factory=lambda: float(os.getenv("MAX_DAILY_LOSS_PCT", "2")))
    max_drawdown_pct: float = field(default_factory=lambda: float(os.getenv("MAX_DRAWDOWN_PCT", "10")))
    max_notional_pct: float = field(default_factory=lambda: float(os.getenv("MAX_NOTIONAL_PCT", "20")))
    max_open_positions: int = field(default_factory=lambda: int(os.getenv("MAX_OPEN_POSITIONS", "3")))
    max_trades_per_hour: int = field(default_factory=lambda: int(os.getenv("MAX_TRADES_PER_HOUR", "6")))
    atr_period: int = field(default_factory=lambda: int(os.getenv("ATR_PERIOD", "14")))
    atr_stop_mult: float = field(default_factory=lambda: float(os.getenv("ATR_STOP_MULTIPLIER", "1.5")))
    atr_take_mult: float = field(default_factory=lambda: float(os.getenv("ATR_TAKE_MULTIPLIER", "3")))
    api_key: str = field(default_factory=lambda: os.getenv("BINANCE_API_KEY", ""), repr=False)
    api_secret: str = field(default_factory=lambda: os.getenv("BINANCE_API_SECRET", ""), repr=False)
    live_confirmation: str = field(default_factory=lambda: os.getenv("LIVE_TRADING_CONFIRM", ""), repr=False)
    db_path: Path = field(default_factory=lambda: Path(os.getenv("TRADES_DB_PATH", "data/trades.jsonl")))
    risk_state_path: Path = field(default_factory=lambda: Path(os.getenv("RISK_STATE_PATH", "data/risk_state.json")))
    positions_path: Path = field(default_factory=lambda: Path(os.getenv("POSITIONS_PATH", "data/positions.json")))
    event_log_path: Path = field(default_factory=lambda: Path(os.getenv("EVENT_LOG_PATH", "data/events.jsonl")))
    live_poll_seconds: int = field(default_factory=lambda: int(os.getenv("LIVE_POLL_SECONDS", "60")))
    oracle_max_age_seconds: int = field(default_factory=lambda: int(os.getenv("ORACLE_MAX_AGE_SECONDS", "300")))
    oracle_interval_seconds: int = field(default_factory=lambda: int(os.getenv("ORACLE_INTERVAL_SECONDS", "3600")))

    def validate(self) -> None:
        if not self.symbols or any(not symbol.isalnum() for symbol in self.symbols):
            raise ValueError("SYMBOLS must contain valid alphanumeric Binance symbols")
        if self.initial_equity <= 0 or self.max_open_positions < 1 or self.max_trades_per_hour < 1:
            raise ValueError("Initial equity and trade limits must be positive")
        if self.live_poll_seconds < 5:
            raise ValueError("LIVE_POLL_SECONDS must be >= 5")
        if self.atr_period < 2 or self.atr_stop_mult <= 0 or self.atr_take_mult <= 0:
            raise ValueError("ATR settings must be positive")
        if self.risk_per_trade_pct <= 0 or self.risk_per_trade_pct > 2:
            raise ValueError("RISK_PER_TRADE_PCT must be > 0 and <= 2")
        if self.max_daily_loss_pct <= 0 or self.max_drawdown_pct <= 0 or self.max_notional_pct <= 0:
            raise ValueError("loss, drawdown, and notional limits must be positive")
        if self.mode is Mode.LIVE and self.live_confirmation != "I_UNDERSTAND_RISK":
            raise ValueError("Live trading is locked. Set LIVE_TRADING_CONFIRM=I_UNDERSTAND_RISK explicitly.")
        if self.mode in (Mode.TESTNET, Mode.LIVE) and (not self.api_key or not self.api_secret):
            raise ValueError("Binance API credentials are required for testnet/live")


@dataclass
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class Position:
    symbol: str
    side: str
    entry: float
    quantity: float
    stop: float
    take: float
    opened_at: str


@dataclass
class Trade:
    symbol: str
    side: str
    entry: float
    exit: float
    quantity: float
    pnl: float
    reason: str
    opened_at: str
    closed_at: str


class BinanceREST:
    """Minimal signed REST adapter; no third-party SDK required."""
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.base = "https://testnet.binance.vision" if cfg.mode is Mode.TESTNET else "https://api.binance.com"
        self._filters_cache: dict[str, tuple[float, dict]] = {}

    def _request(self, path: str, params: dict[str, object] | None = None, signed: bool = False, method: str = "GET"):
        params = dict(params or {})
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            query = urllib.parse.urlencode(params)
            params["signature"] = hmac.new(self.cfg.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        query = urllib.parse.urlencode(params)
        req = urllib.request.Request(f"{self.base}{path}?{query}", headers={"X-MBX-APIKEY": self.cfg.api_key}, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            body = exc.read().decode(errors="replace")
            try:
                payload = json.loads(body)
                code, message = payload.get("code"), payload.get("msg", body)
            except json.JSONDecodeError:
                code, message = None, body
            raise BinanceError(exc.code, code, message, path) from exc

    def klines(self, symbol: str, interval: str = "1h", limit: int = 300) -> list[Candle]:
        rows = self._request("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
        return [Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in rows]

    def account(self) -> dict:
        return self._request("/api/v3/account", signed=True)

    def market_order(self, symbol: str, side: str, quantity: float, client_order_id: str | None = None) -> dict:
        """Real market buy/sell. Binance side must be 'BUY' or 'SELL'."""
        if self.cfg.mode is not Mode.LIVE:
            raise RuntimeError("market_order is disabled outside live mode")
        params = {"symbol": symbol, "side": side, "type": "MARKET", "quantity": f"{quantity:.8f}"}
        if client_order_id:
            params["newClientOrderId"] = client_order_id
        return self._request("/api/v3/order", params, signed=True, method="POST")

    def place_oco_order(self, symbol: str, side: str, quantity: float, take_profit_price: float, stop_price: float, stop_limit_price: float, stop_limit_time_in_force: str = "GTC") -> dict:
        """Real stop-loss + take-profit bracket as a single Binance OCO order.

        side is the side that closes the position (e.g. 'SELL' to exit a long).
        take_profit_price is the limit leg; stop_price/stop_limit_price are the stop leg.
        """
        if self.cfg.mode is not Mode.LIVE:
            raise RuntimeError("place_oco_order is disabled outside live mode")
        params = {
            "symbol": symbol,
            "side": side,
            "quantity": f"{quantity:.8f}",
            "price": f"{take_profit_price:.8f}",
            "stopPrice": f"{stop_price:.8f}",
            "stopLimitPrice": f"{stop_limit_price:.8f}",
            "stopLimitTimeInForce": stop_limit_time_in_force,
        }
        return self._request("/api/v3/order/oco", params, signed=True, method="POST")

    def cancel_order(self, symbol: str, order_id: int) -> dict:
        if self.cfg.mode is not Mode.LIVE:
            raise RuntimeError("cancel_order is disabled outside live mode")
        return self._request("/api/v3/order", {"symbol": symbol, "orderId": order_id}, signed=True, method="DELETE")

    def cancel_oco_order(self, symbol: str, order_list_id: int) -> dict:
        if self.cfg.mode is not Mode.LIVE:
            raise RuntimeError("cancel_oco_order is disabled outside live mode")
        return self._request("/api/v3/orderList", {"symbol": symbol, "orderListId": order_list_id}, signed=True, method="DELETE")

    def get_open_orders(self, symbol: str) -> list[dict]:
        return self._request("/api/v3/openOrders", {"symbol": symbol}, signed=True)

    def get_order(self, symbol: str, order_id: int) -> dict:
        return self._request("/api/v3/order", {"symbol": symbol, "orderId": order_id}, signed=True)

    def get_convert_quote(self, from_asset: str, to_asset: str, from_amount: float, valid_time: str = "10s") -> dict:
        """Firm, time-limited quote to swap one asset directly into another — Binance's
        Convert feature, the same swap a user gets tapping 'Convert' in the app.
        Read-only: getting a quote does not move any funds."""
        params = {"fromAsset": from_asset, "toAsset": to_asset, "fromAmount": f"{from_amount:.8f}", "validTime": valid_time}
        return self._request("/sapi/v1/convert/getQuote", params, signed=True, method="POST")

    def accept_convert_quote(self, quote_id: str) -> dict:
        if self.cfg.mode is not Mode.LIVE:
            raise RuntimeError("accept_convert_quote is disabled outside live mode")
        return self._request("/sapi/v1/convert/acceptQuote", {"quoteId": quote_id}, signed=True, method="POST")

    def get_convert_order_status(self, order_id: str | None = None, quote_id: str | None = None) -> dict:
        if not order_id and not quote_id:
            raise ValueError("get_convert_order_status requires order_id or quote_id")
        params = {"orderId": order_id} if order_id else {"quoteId": quote_id}
        return self._request("/sapi/v1/convert/orderStatus", params, signed=True)

    def convert(self, from_asset: str, to_asset: str, from_amount: float) -> dict:
        """One-step convert: quote then immediately accept, like a single tap of
        'Convert' in the Binance app. Moves real funds; live mode only."""
        if self.cfg.mode is not Mode.LIVE:
            raise RuntimeError("convert is disabled outside live mode")
        quote = self.get_convert_quote(from_asset, to_asset, from_amount)
        return self.accept_convert_quote(quote["quoteId"])

    def place_limit_order(self, symbol: str, side: str, quantity: float, price: float, time_in_force: str = "GTC") -> dict:
        if self.cfg.mode is not Mode.LIVE:
            raise RuntimeError("place_limit_order is disabled outside live mode")
        params = {"symbol": symbol, "side": side, "type": "LIMIT", "timeInForce": time_in_force,
                  "quantity": f"{quantity:.8f}", "price": f"{price:.8f}"}
        return self._request("/api/v3/order", params, signed=True, method="POST")

    def place_stop_loss_limit_order(self, symbol: str, side: str, quantity: float, stop_price: float, limit_price: float, time_in_force: str = "GTC") -> dict:
        """Standalone stop-loss order (not paired with a take-profit like place_oco_order)."""
        if self.cfg.mode is not Mode.LIVE:
            raise RuntimeError("place_stop_loss_limit_order is disabled outside live mode")
        params = {"symbol": symbol, "side": side, "type": "STOP_LOSS_LIMIT", "timeInForce": time_in_force,
                  "quantity": f"{quantity:.8f}", "price": f"{limit_price:.8f}", "stopPrice": f"{stop_price:.8f}"}
        return self._request("/api/v3/order", params, signed=True, method="POST")

    def place_take_profit_limit_order(self, symbol: str, side: str, quantity: float, stop_price: float, limit_price: float, time_in_force: str = "GTC") -> dict:
        """Standalone take-profit order (not paired with a stop-loss like place_oco_order)."""
        if self.cfg.mode is not Mode.LIVE:
            raise RuntimeError("place_take_profit_limit_order is disabled outside live mode")
        params = {"symbol": symbol, "side": side, "type": "TAKE_PROFIT_LIMIT", "timeInForce": time_in_force,
                  "quantity": f"{quantity:.8f}", "price": f"{limit_price:.8f}", "stopPrice": f"{stop_price:.8f}"}
        return self._request("/api/v3/order", params, signed=True, method="POST")

    def cancel_all_open_orders(self, symbol: str) -> list[dict]:
        """Flattens every open order (including OCO legs) on one symbol in a single call."""
        if self.cfg.mode is not Mode.LIVE:
            raise RuntimeError("cancel_all_open_orders is disabled outside live mode")
        return self._request("/api/v3/openOrders", {"symbol": symbol}, signed=True, method="DELETE")

    def get_all_orders(self, symbol: str, limit: int = 500) -> list[dict]:
        """Full order history for a symbol (not just currently-open orders)."""
        return self._request("/api/v3/allOrders", {"symbol": symbol, "limit": limit}, signed=True)

    def get_my_trades(self, symbol: str, limit: int = 500) -> list[dict]:
        """Actual executions/fills for a symbol — what a user sees under Trade History."""
        return self._request("/api/v3/myTrades", {"symbol": symbol, "limit": limit}, signed=True)

    def get_exchange_info(self, symbol: str | None = None) -> dict:
        """Public endpoint: trading rules and filters. No signing needed, same as klines()."""
        return self._request("/api/v3/exchangeInfo", {"symbol": symbol} if symbol else {})

    def get_symbol_filters(self, symbol: str) -> dict:
        """step_size/tick_size/min_qty/min_notional for one symbol, so a computed order
        quantity or price can be rounded to what Binance will actually accept instead of
        being rejected. Verified field names against Binance's exchangeInfo docs."""
        cached = self._filters_cache.get(symbol)
        if cached and time.time() - cached[0] < 3600:
            return dict(cached[1])
        info = self.get_exchange_info(symbol)
        filters = {f["filterType"]: f for f in info["symbols"][0]["filters"]}
        lot = filters.get("LOT_SIZE", {})
        price_filter = filters.get("PRICE_FILTER", {})
        notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
        result = {
            "step_size": float(lot["stepSize"]) if lot.get("stepSize") else None,
            "min_qty": float(lot["minQty"]) if lot.get("minQty") else None,
            "tick_size": float(price_filter["tickSize"]) if price_filter.get("tickSize") else None,
            "min_notional": float(notional["minNotional"]) if notional.get("minNotional") else None,
        }
        self._filters_cache[symbol] = (time.time(), result)
        return dict(result)


def round_to_step(value: float, step: Optional[float]) -> float:
    """Round down to the nearest multiple of step (e.g. LOT_SIZE stepSize / PRICE_FILTER
    tickSize) so real orders aren't rejected for violating exchange precision rules."""
    if not step:
        return value
    return round(math.floor(round(value / step, 8)) * step, 8)


def sma(values: list[float], period: int) -> Optional[float]:
    return sum(values[-period:]) / period if len(values) >= period else None


def atr(candles: list[Candle], period: int = 14) -> Optional[float]:
    if len(candles) < period + 1:
        return None
    trs = []
    for prev, cur in zip(candles[-period-1:-1], candles[-period:]):
        trs.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
    return sum(trs) / len(trs)


class RegimeStrategy:
    """Conservative trend/mean-reversion hybrid with a volatility kill filter."""
    def __init__(self, cfg: Config): self.cfg = cfg

    def decide(self, candles: list[Candle]) -> Signal:
        if len(candles) < 60:
            return Signal.HOLD
        closes = [c.close for c in candles]
        fast, slow = sma(closes, 20), sma(closes, 50)
        a = atr(candles, self.cfg.atr_period)
        if fast is None or slow is None or a is None or closes[-1] <= 0:
            return Signal.HOLD
        vol = a / closes[-1]
        if vol > 0.08:  # abnormal volatility: preserve capital
            return Signal.HOLD
        change = closes[-1] / closes[-4] - 1
        if fast > slow and change > 0.002:
            return Signal.BUY
        if fast < slow and change < -0.002:
            return Signal.SELL
        return Signal.HOLD


class RiskGate:
    def __init__(self, cfg: Config):
        self.cfg, self.start_equity, self.equity = cfg, cfg.initial_equity, cfg.initial_equity
        self.peak_equity, self.daily_pnl, self.trades = cfg.initial_equity, 0.0, 0
        self.day = datetime.now(timezone.utc).date()
        self.trade_times: list[float] = []

    def _roll_day(self) -> None:
        today = datetime.now(timezone.utc).date()
        if today != self.day:
            self.day, self.daily_pnl = today, 0.0

    def _trim_hour(self) -> None:
        cutoff = time.time() - 3600
        self.trade_times = [stamp for stamp in self.trade_times if stamp >= cutoff]

    def approve(self, signal: Signal, price: float, a: Optional[float], open_count: int) -> tuple[bool, float, str]:
        self._roll_day(); self._trim_hour()
        if signal is Signal.HOLD or price <= 0 or not a or open_count >= self.cfg.max_open_positions:
            return False, 0.0, "no-trade-condition"
        if len(self.trade_times) >= self.cfg.max_trades_per_hour:
            return False, 0.0, "hourly-trade-limit"
        if self.daily_pnl <= -self.start_equity * self.cfg.max_daily_loss_pct / 100:
            return False, 0.0, "daily-loss-limit"
        drawdown = (self.peak_equity - self.equity) / self.peak_equity * 100 if self.peak_equity else 0
        if drawdown >= self.cfg.max_drawdown_pct:
            return False, 0.0, "max-drawdown"
        stop_distance = a * self.cfg.atr_stop_mult
        qty = (self.equity * self.cfg.risk_per_trade_pct / 100) / stop_distance
        return (qty > 0, qty, "approved" if qty > 0 else "invalid-size")

    def opened(self) -> None:
        """Record an accepted entry so hourly limits apply before the exit."""
        self._roll_day(); self._trim_hour(); self.trade_times.append(time.time())

    def closed(self, pnl: float) -> None:
        self._roll_day()
        self.equity += pnl; self.daily_pnl += pnl; self.peak_equity = max(self.peak_equity, self.equity); self.trades += 1
        self.trade_times.append(time.time())

    def state_dict(self) -> dict:
        return {"equity": self.equity, "peak_equity": self.peak_equity, "daily_pnl": self.daily_pnl,
                "trades": self.trades, "day": self.day.isoformat(), "trade_times": self.trade_times}

    def restore(self, state: dict) -> None:
        """Reload persisted risk state so daily-loss/drawdown/hourly limits survive across
        separate manual `live` invocations instead of silently resetting each run."""
        self.equity = state.get("equity", self.equity)
        self.peak_equity = state.get("peak_equity", self.peak_equity)
        self.daily_pnl = state.get("daily_pnl", self.daily_pnl)
        self.trades = state.get("trades", self.trades)
        self.trade_times = state.get("trade_times", self.trade_times)
        if "day" in state:
            self.day = datetime.fromisoformat(state["day"]).date()
        self._roll_day()


class PaperBroker:
    def __init__(self, cfg: Config, risk: RiskGate): self.cfg, self.risk, self.positions, self.trades = cfg, risk, {}, []
    def open(self, symbol: str, signal: Signal, price: float, qty: float, a: float) -> None:
        side = "LONG" if signal is Signal.BUY else "SHORT"
        self.positions[symbol] = Position(symbol, side, price, qty, price - a*self.cfg.atr_stop_mult if side == "LONG" else price + a*self.cfg.atr_stop_mult, price + a*self.cfg.atr_take_mult if side == "LONG" else price - a*self.cfg.atr_take_mult, datetime.now(timezone.utc).isoformat())
    def mark(self, symbol: str, price: float, signal: Signal = Signal.HOLD) -> Optional[Trade]:
        p = self.positions.get(symbol)
        if not p: return None
        reason = "signal" if (p.side == "LONG" and signal is Signal.SELL) or (p.side == "SHORT" and signal is Signal.BUY) else "risk"
        hit = (p.side == "LONG" and (price <= p.stop or price >= p.take)) or (p.side == "SHORT" and (price >= p.stop or price <= p.take))
        if not hit and reason == "risk": return None
        pnl = (price - p.entry) * p.quantity if p.side == "LONG" else (p.entry - price) * p.quantity
        t = Trade(p.symbol, p.side, p.entry, price, p.quantity, pnl, "stop/take" if hit else reason, p.opened_at, datetime.now(timezone.utc).isoformat())
        self.trades.append(t); self.risk.closed(pnl); del self.positions[symbol]; return t


def load_csv(path: str) -> list[Candle]:
    with open(path, newline="", encoding="utf-8") as f:
        return [Candle(int(r["timestamp"]), float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"]), float(r.get("volume", 0))) for r in csv.DictReader(f)]


def backtest(candles: list[Candle], cfg: Config) -> dict[str, float]:
    if not candles or not cfg.symbols:
        return {"trades": 0, "pnl": 0.0, "win_rate_pct": 0.0, "profit_factor": 0.0}
    strategy, risk = RegimeStrategy(cfg), RiskGate(cfg)
    broker = PaperBroker(cfg, risk)
    for i in range(len(candles)):
        window = candles[:i+1]; c = candles[i]; sig = strategy.decide(window)
        closed = broker.mark(cfg.symbols[0], c.close, sig)
        if closed: LOG.info("closed %s pnl=%.2f", closed.symbol, closed.pnl)
        ok, qty, _ = risk.approve(sig, c.close, atr(window, cfg.atr_period), len(broker.positions))
        if ok and cfg.symbols[0] not in broker.positions: broker.open(cfg.symbols[0], sig, c.close, qty, atr(window, cfg.atr_period) or 0)
    if broker.positions: broker.mark(cfg.symbols[0], candles[-1].close, Signal.SELL)
    wins = sum(t.pnl > 0 for t in broker.trades); gross_win = sum(t.pnl for t in broker.trades if t.pnl > 0); gross_loss = -sum(t.pnl for t in broker.trades if t.pnl < 0)
    return {"trades": len(broker.trades), "pnl": round(sum(t.pnl for t in broker.trades), 8), "win_rate_pct": round(wins / len(broker.trades) * 100, 2) if broker.trades else 0.0, "profit_factor": round(gross_win / gross_loss, 4) if gross_loss else 0.0}


def load_risk_state(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        LOG.warning("risk state at %s is unreadable; starting fresh", path)
        return {}


def save_risk_state(path: Path, risk: RiskGate) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(risk.state_dict()))
    tmp.replace(path)


def load_positions(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        value = json.loads(path.read_text())
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, OSError):
        LOG.warning("positions at %s is unreadable; refusing new entries", path)
        return {"__unreadable__": True}


def save_positions(path: Path, positions: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(positions))
    tmp.replace(path)


def reconcile_position(client: BinanceREST, symbol: str, position: dict) -> Optional[float]:
    """Return realized P&L only when a closing sell fill is observable."""
    trades = client.get_my_trades(symbol)
    if not isinstance(trades, list):
        return None
    since = int(position.get("opened_at_ms", 0))
    relevant = [t for t in trades if int(t.get("time", 0)) >= since]
    sells = [t for t in relevant if not bool(t.get("isBuyer"))]
    if not sells:
        return None
    buys = [t for t in relevant if bool(t.get("isBuyer"))]
    buy_cost = sum(float(t.get("quoteQty", float(t.get("price", 0)) * float(t.get("qty", 0)))) for t in buys)
    sell_value = sum(float(t.get("quoteQty", float(t.get("price", 0)) * float(t.get("qty", 0)))) for t in sells)
    commission = sum(float(t.get("commission", 0)) for t in relevant if t.get("commissionAsset") == position.get("quote_asset"))
    return sell_value - buy_cost - commission


def live_equity(client: BinanceREST, quote_asset: str) -> Optional[float]:
    """Return the actual quote-asset balance; paper mode remains config-backed."""
    account = client.account()
    if not isinstance(account, dict):
        return None
    if account.get("canWithdraw") is True:
        raise RuntimeError("live trading refuses an API key with withdrawal permission")
    for balance in account.get("balances", []):
        if balance.get("asset") == quote_asset:
            return float(balance.get("free", 0)) + float(balance.get("locked", 0))
    return 0.0


def run_live_cycle(cfg: Config, client: BinanceREST, risk: RiskGate, strategy: RegimeStrategy, positions: Optional[dict] = None, oracle=None, monitor=None) -> dict:
    """One real evaluate-and-act pass over every symbol.

    Binance spot has no shorting: a SELL signal only ever closes a position you
    already hold, so it is skipped here rather than sent as a broken sell-to-open.
    A persisted position ledger is reconciled against observed trade history so
    realized P&L can update RiskGate after a bracket closes. The ledger is also
    used to prevent duplicate entries across restarts.
    """
    if positions is None:
        # The resident live loop owns the persisted ledger; direct callers may
        # pass one explicitly. This keeps one-shot paper/mock calls isolated.
        positions = {}
    ledger_unreadable = positions.get("__unreadable__") is True
    if cfg.mode is Mode.LIVE:
        actual_equity = live_equity(client, cfg.quote_asset)
        if actual_equity is not None:
            risk.equity = actual_equity
            risk.peak_equity = max(risk.peak_equity, actual_equity)
    for symbol, position in list(positions.items()):
        if symbol.startswith("__"):
            continue
        pnl = reconcile_position(client, symbol, position)
        if pnl is not None:
            risk.closed(pnl)
            del positions[symbol]
    open_symbols = {symbol: bool(client.get_open_orders(symbol)) or symbol in positions for symbol in cfg.symbols}
    open_count = sum(open_symbols.values())
    results = []
    for symbol in cfg.symbols:
        if open_symbols[symbol]:
            results.append({"symbol": symbol, "action": "skipped", "reason": "open-order-exists"})
            continue
        try:
            candles = oracle.candles(symbol) if oracle is not None else client.klines(symbol)
        except Exception as exc:
            LOG.warning("oracle rejected %s: %s", symbol, exc)
            results.append({"symbol": symbol, "action": "no-trade", "reason": "oracle-rejected"})
            continue
        if not candles:
            results.append({"symbol": symbol, "action": "skipped", "reason": "no-data"})
            continue
        signal = strategy.decide(candles)
        if signal is Signal.SELL:
            results.append({"symbol": symbol, "action": "skipped", "reason": "spot-no-short"})
            continue
        price = candles[-1].close
        a = atr(candles, cfg.atr_period)
        ok, qty, reason = risk.approve(signal, price, a, open_count)
        if ledger_unreadable:
            results.append({"symbol": symbol, "action": "no-trade", "reason": "positions-state-unreadable"})
            continue
        if not ok:
            results.append({"symbol": symbol, "action": "no-trade", "reason": reason})
            continue
        max_notional = risk.equity * cfg.max_notional_pct / 100
        qty = min(qty, max_notional / price) if max_notional > 0 else 0.0
        filters = client.get_symbol_filters(symbol)
        qty = round_to_step(qty, filters["step_size"])
        if filters["min_qty"] and qty < filters["min_qty"]:
            results.append({"symbol": symbol, "action": "no-trade", "reason": "below-min-qty"})
            continue
        if filters["min_notional"] and qty * price < filters["min_notional"]:
            results.append({"symbol": symbol, "action": "no-trade", "reason": "below-min-notional"})
            continue
        client_order_id = f"orca-{symbol}-{int(time.time() * 1000)}"
        order = client.market_order(symbol, "BUY", qty, client_order_id=client_order_id)
        filled = float(order.get("executedQty", qty) or 0) if isinstance(order, dict) else qty
        if filled <= 0:
            results.append({"symbol": symbol, "action": "aborted", "reason": "entry-not-filled"})
            continue
        filled = round_to_step(filled, filters["step_size"])
        stop = round_to_step(price - a * cfg.atr_stop_mult, filters["tick_size"])
        take = round_to_step(price + a * cfg.atr_take_mult, filters["tick_size"])
        try:
            bracket = client.place_oco_order(symbol, "SELL", filled, take_profit_price=take, stop_price=stop, stop_limit_price=stop)
        except Exception:
            LOG.exception("protective bracket failed for %s; flattening entry", symbol)
            try:
                client.cancel_all_open_orders(symbol)
                client.market_order(symbol, "SELL", filled, client_order_id=f"orca-flatten-{symbol}-{int(time.time() * 1000)}")
            except Exception:
                LOG.exception("failed to flatten unprotected entry for %s", symbol)
            results.append({"symbol": symbol, "action": "aborted", "reason": "bracket-failed-flattened"})
            continue
        risk.opened()
        positions[symbol] = {
            "quantity": filled,
            "entry": price,
            "opened_at_ms": int(time.time() * 1000),
            "quote_asset": cfg.quote_asset,
            "client_order_id": client_order_id,
            "bracket": bracket,
        }
        save_positions(cfg.positions_path, positions)
        open_count += 1
        LOG.info("opened %s qty=%s entry=%.8f stop=%.8f take=%.8f", symbol, qty, price, stop, take)
        results.append({"symbol": symbol, "action": "opened", "order": order, "bracket": bracket})
    if not ledger_unreadable:
        save_positions(cfg.positions_path, positions)
    if monitor is not None:
        for item in results:
            monitor.emit("decision", **item)
    return {"results": results}


def run_live(cfg: Config, client: BinanceREST) -> None:
    """Autonomous live loop: starts only when you run this command yourself, then
    keeps evaluating and trading on its own every LIVE_POLL_SECONDS until you stop
    it (Ctrl+C / SIGTERM)."""
    if cfg.mode is not Mode.LIVE:
        raise RuntimeError("live requires TRADING_MODE=live with LIVE_TRADING_CONFIRM set")
    risk, strategy = RiskGate(cfg), RegimeStrategy(cfg)
    positions = load_positions(cfg.positions_path)
    from oracle import MarketOracle, OracleConfig
    from monitoring import JsonlMonitor
    oracle = MarketOracle(client.klines, OracleConfig(cfg.oracle_max_age_seconds, cfg.oracle_interval_seconds))
    monitor = JsonlMonitor(cfg.event_log_path)
    monitor.health(cfg.mode.value, cfg.symbols, oracle_ok=True)
    risk.restore(load_risk_state(cfg.risk_state_path))
    stop_requested = {"flag": False}

    def _handle_stop(signum, _frame):
        LOG.info("stop signal %s received; exiting after the current cycle", signum)
        stop_requested["flag"] = True

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)
    LOG.info("live loop started for %s, polling every %ss — started by explicit command, not scheduled", cfg.symbols, cfg.live_poll_seconds)
    consecutive_failures = 0
    while not stop_requested["flag"]:
        try:
            run_live_cycle(cfg, client, risk, strategy, positions, oracle, monitor)
            consecutive_failures = 0
        except BinanceError as exc:
            consecutive_failures += 1
            LOG.error("live cycle failed: %s", exc)
            if exc.is_fatal or consecutive_failures >= 5:
                LOG.error("stopping live loop after unrecoverable/repeated Binance failures")
                break
        except Exception:
            consecutive_failures += 1
            LOG.exception("live cycle failed; will retry next poll")
            if consecutive_failures >= 5:
                LOG.error("stopping live loop after five consecutive failures")
                break
        save_risk_state(cfg.risk_state_path, risk)
        if positions.get("__unreadable__") is not True:
            save_positions(cfg.positions_path, positions)
        for _ in range(cfg.live_poll_seconds):
            if stop_requested["flag"]:
                break
            time.sleep(1)
    LOG.info("live loop stopped cleanly")


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["backtest", "fetch", "config-check", "live"])
    parser.add_argument("--csv", help="CSV columns: timestamp,open,high,low,close,volume")
    args = parser.parse_args(); cfg = Config(); cfg.validate()
    if args.command == "config-check": print(json.dumps({"mode": cfg.mode.value, "symbols": cfg.symbols, "live_enabled": cfg.mode is Mode.LIVE}, indent=2)); return 0
    if args.command == "backtest":
        if not args.csv: parser.error("--csv is required for backtest")
        print(json.dumps(backtest(load_csv(args.csv), cfg), indent=2)); return 0
    client = BinanceREST(cfg)
    if not cfg.symbols: raise SystemExit("SYMBOLS is empty")
    if args.command == "live":
        run_live(cfg, client); return 0
    print(json.dumps([c.__dict__ for c in client.klines(cfg.symbols[0])[-5:]], indent=2)); return 0

if __name__ == "__main__": raise SystemExit(main())
