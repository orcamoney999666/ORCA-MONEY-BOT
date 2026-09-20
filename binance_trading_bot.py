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
import os
import signal
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

LOG = logging.getLogger("orca")


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
    mode: Mode = Mode(os.getenv("TRADING_MODE", "paper").lower())
    symbols: tuple[str, ...] = tuple(s.strip().upper() for s in os.getenv("SYMBOLS", "BTCUSDT,ETHUSDT").split(",") if s.strip())
    quote_asset: str = os.getenv("QUOTE_ASSET", "USDT")
    initial_equity: float = float(os.getenv("PAPER_START_BALANCE", "10000"))
    risk_per_trade_pct: float = float(os.getenv("RISK_PER_TRADE_PCT", "0.25"))
    max_daily_loss_pct: float = float(os.getenv("MAX_DAILY_LOSS_PCT", "2"))
    max_drawdown_pct: float = float(os.getenv("MAX_DRAWDOWN_PCT", "10"))
    max_open_positions: int = int(os.getenv("MAX_OPEN_POSITIONS", "3"))
    max_trades_per_hour: int = int(os.getenv("MAX_TRADES_PER_HOUR", "6"))
    atr_period: int = int(os.getenv("ATR_PERIOD", "14"))
    atr_stop_mult: float = float(os.getenv("ATR_STOP_MULTIPLIER", "1.5"))
    atr_take_mult: float = float(os.getenv("ATR_TAKE_MULTIPLIER", "3"))
    api_key: str = os.getenv("BINANCE_API_KEY", "")
    api_secret: str = os.getenv("BINANCE_API_SECRET", "")
    live_confirmation: str = os.getenv("LIVE_TRADING_CONFIRM", "")
    db_path: Path = Path(os.getenv("TRADES_DB_PATH", "data/trades.jsonl"))
    risk_state_path: Path = Path(os.getenv("RISK_STATE_PATH", "data/risk_state.json"))
    live_poll_seconds: int = int(os.getenv("LIVE_POLL_SECONDS", "60"))

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

    def _request(self, path: str, params: dict[str, object] | None = None, signed: bool = False, method: str = "GET"):
        params = dict(params or {})
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            query = urllib.parse.urlencode(params)
            params["signature"] = hmac.new(self.cfg.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        query = urllib.parse.urlencode(params)
        req = urllib.request.Request(f"{self.base}{path}?{query}", headers={"X-MBX-APIKEY": self.cfg.api_key}, method=method)
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())

    def klines(self, symbol: str, interval: str = "1h", limit: int = 300) -> list[Candle]:
        rows = self._request("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
        return [Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in rows]

    def account(self) -> dict:
        return self._request("/api/v3/account", signed=True)

    def market_order(self, symbol: str, side: str, quantity: float) -> dict:
        """Real market buy/sell. Binance side must be 'BUY' or 'SELL'."""
        if self.cfg.mode is not Mode.LIVE:
            raise RuntimeError("market_order is disabled outside live mode")
        params = {"symbol": symbol, "side": side, "type": "MARKET", "quantity": f"{quantity:.8f}"}
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
    path.write_text(json.dumps(risk.state_dict()))


def run_live_cycle(cfg: Config, client: BinanceREST, risk: RiskGate, strategy: RegimeStrategy) -> dict:
    """One real evaluate-and-act pass over every symbol.

    Binance spot has no shorting: a SELL signal only ever closes a position you
    already hold, so it is skipped here rather than sent as a broken sell-to-open.
    Real P&L reconciliation (crediting risk.equity when a bracket actually fills)
    is not implemented yet; this cycle only prevents opening more than
    MAX_OPEN_POSITIONS at once.
    """
    open_symbols = {symbol: bool(client.get_open_orders(symbol)) for symbol in cfg.symbols}
    open_count = sum(open_symbols.values())
    results = []
    for symbol in cfg.symbols:
        if open_symbols[symbol]:
            results.append({"symbol": symbol, "action": "skipped", "reason": "open-order-exists"})
            continue
        candles = client.klines(symbol)
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
        if not ok:
            results.append({"symbol": symbol, "action": "no-trade", "reason": reason})
            continue
        order = client.market_order(symbol, "BUY", qty)
        stop, take = price - a * cfg.atr_stop_mult, price + a * cfg.atr_take_mult
        bracket = client.place_oco_order(symbol, "SELL", qty, take_profit_price=take, stop_price=stop, stop_limit_price=stop)
        open_count += 1
        LOG.info("opened %s qty=%s entry=%.8f stop=%.8f take=%.8f", symbol, qty, price, stop, take)
        results.append({"symbol": symbol, "action": "opened", "order": order, "bracket": bracket})
    return {"results": results}


def run_live(cfg: Config, client: BinanceREST) -> None:
    """Autonomous live loop: starts only when you run this command yourself, then
    keeps evaluating and trading on its own every LIVE_POLL_SECONDS until you stop
    it (Ctrl+C / SIGTERM)."""
    if cfg.mode is not Mode.LIVE:
        raise RuntimeError("live requires TRADING_MODE=live with LIVE_TRADING_CONFIRM set")
    risk, strategy = RiskGate(cfg), RegimeStrategy(cfg)
    risk.restore(load_risk_state(cfg.risk_state_path))
    stop_requested = {"flag": False}

    def _handle_stop(signum, _frame):
        LOG.info("stop signal %s received; exiting after the current cycle", signum)
        stop_requested["flag"] = True

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)
    LOG.info("live loop started for %s, polling every %ss — started by explicit command, not scheduled", cfg.symbols, cfg.live_poll_seconds)
    while not stop_requested["flag"]:
        try:
            run_live_cycle(cfg, client, risk, strategy)
        except Exception:
            LOG.exception("live cycle failed; will retry next poll")
        save_risk_state(cfg.risk_state_path, risk)
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
