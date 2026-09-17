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
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Iterable, Optional

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

    def validate(self) -> None:
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

    def _request(self, path: str, params: dict[str, object] | None = None, signed: bool = False):
        params = dict(params or {})
        if signed:
            params["timestamp"] = int(time.time() * 1000)
            query = urllib.parse.urlencode(params)
            params["signature"] = hmac.new(self.cfg.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        query = urllib.parse.urlencode(params)
        req = urllib.request.Request(f"{self.base}{path}?{query}", headers={"X-MBX-APIKEY": self.cfg.api_key})
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode())

    def klines(self, symbol: str, interval: str = "1h", limit: int = 300) -> list[Candle]:
        rows = self._request("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
        return [Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in rows]

    def account(self) -> dict:
        return self._request("/api/v3/account", signed=True)

    def market_order(self, symbol: str, side: str, quantity: float) -> dict:
        if self.cfg.mode is not Mode.LIVE:
            raise RuntimeError("market_order is disabled outside live mode")
        return self._request("/api/v3/order", {"symbol": symbol, "side": side, "type": "MARKET", "quantity": f"{quantity:.8f}"}, signed=True)


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

    def approve(self, signal: Signal, price: float, a: Optional[float], open_count: int) -> tuple[bool, float, str]:
        if signal is Signal.HOLD or price <= 0 or not a or open_count >= self.cfg.max_open_positions:
            return False, 0.0, "no-trade-condition"
        if self.daily_pnl <= -self.start_equity * self.cfg.max_daily_loss_pct / 100:
            return False, 0.0, "daily-loss-limit"
        drawdown = (self.peak_equity - self.equity) / self.peak_equity * 100 if self.peak_equity else 0
        if drawdown >= self.cfg.max_drawdown_pct:
            return False, 0.0, "max-drawdown"
        stop_distance = a * self.cfg.atr_stop_mult
        qty = (self.equity * self.cfg.risk_per_trade_pct / 100) / stop_distance
        return (qty > 0, qty, "approved" if qty > 0 else "invalid-size")

    def closed(self, pnl: float) -> None:
        self.equity += pnl; self.daily_pnl += pnl; self.peak_equity = max(self.peak_equity, self.equity); self.trades += 1


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
    strategy, risk, broker = RegimeStrategy(cfg), RiskGate(cfg), PaperBroker(cfg, RiskGate(cfg))
    broker.risk = risk
    for i in range(len(candles)):
        window = candles[:i+1]; c = candles[i]; sig = strategy.decide(window)
        closed = broker.mark(cfg.symbols[0], c.close, sig)
        if closed: LOG.info("closed %s pnl=%.2f", closed.symbol, closed.pnl)
        ok, qty, _ = risk.approve(sig, c.close, atr(window, cfg.atr_period), len(broker.positions))
        if ok and cfg.symbols[0] not in broker.positions: broker.open(cfg.symbols[0], sig, c.close, qty, atr(window, cfg.atr_period) or 0)
    if broker.positions: broker.mark(cfg.symbols[0], candles[-1].close, Signal.SELL)
    wins = sum(t.pnl > 0 for t in broker.trades); gross_win = sum(t.pnl for t in broker.trades if t.pnl > 0); gross_loss = -sum(t.pnl for t in broker.trades if t.pnl < 0)
    return {"trades": len(broker.trades), "pnl": round(sum(t.pnl for t in broker.trades), 8), "win_rate_pct": round(wins / len(broker.trades) * 100, 2) if broker.trades else 0.0, "profit_factor": round(gross_win / gross_loss, 4) if gross_loss else 0.0}


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["backtest", "fetch", "config-check"])
    parser.add_argument("--csv", help="CSV columns: timestamp,open,high,low,close,volume")
    args = parser.parse_args(); cfg = Config(); cfg.validate()
    if args.command == "config-check": print(json.dumps({"mode": cfg.mode.value, "symbols": cfg.symbols, "live_enabled": cfg.mode is Mode.LIVE}, indent=2)); return 0
    if args.command == "backtest":
        if not args.csv: parser.error("--csv is required for backtest")
        print(json.dumps(backtest(load_csv(args.csv), cfg), indent=2)); return 0
    client = BinanceREST(cfg)
    if not cfg.symbols: raise SystemExit("SYMBOLS is empty")
    print(json.dumps([c.__dict__ for c in client.klines(cfg.symbols[0])[-5:]], indent=2)); return 0

if __name__ == "__main__": raise SystemExit(main())
