#!/usr/bin/env python3
"""Dependency-free NDJSON bridge for companion projects.

Read-only commands are available by default. Live execution requires two explicit
operator opt-ins and still passes through the bot's normal Config validation, the same
API-key check as the `live` command, and the same persisted risk state and position
ledger. A bridge that built a fresh RiskGate per request would reset the daily-loss,
drawdown and hourly limits on every call.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from binance_trading_bot import (
    ALLOWED_INTERVALS, BinanceREST, Config, Mode, PositionLedger, RegimeStrategy, RiskGate,
    assert_key_is_trade_only, atr, backtest, load_csv, load_risk_state, run_live_cycle,
    save_risk_state,
)

MAX_LINE_BYTES = 256 * 1024
MAX_CANDLES = 1000


def _request_limit(value: object) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc
    if not 1 <= limit <= MAX_CANDLES:
        raise ValueError("limit must be between 1 and %d" % MAX_CANDLES)
    return limit


def _symbol(value: object, cfg: Config) -> str:
    symbol = str(value or (cfg.symbols[0] if cfg.symbols else "")).upper()
    if not symbol or not symbol.isalnum():
        raise ValueError("symbol must be an alphanumeric Binance symbol")
    return symbol


def _csv_path(value: object) -> str:
    if not value:
        raise ValueError("csv is required")
    path = Path(str(value)).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError("CSV file not found: %s" % path)
    if path.suffix.lower() != ".csv":
        raise ValueError("csv must point to a .csv file")
    return str(path)


class LiveSession:
    """The live plumbing, built once and reused for every live_cycle request.

    Holding the RiskGate and the ledger across requests is the point: they are what the
    loss limits are measured against and what records open positions.
    """

    def __init__(self, cfg: Config, client: BinanceREST):
        assert_key_is_trade_only(client, cfg)
        client.sync_time()
        self.cfg = cfg
        self.risk = RiskGate(cfg)
        self.risk.restore(load_risk_state(cfg.risk_state_path, strict=True))
        self.ledger = PositionLedger(cfg.positions_path).load()
        self.strategy = RegimeStrategy(cfg)

    def cycle(self, client: BinanceREST) -> dict:
        try:
            return run_live_cycle(self.cfg, client, self.risk, self.strategy, self.ledger)
        finally:
            save_risk_state(self.cfg.risk_state_path, self.risk)


def handle(request: dict, cfg: Config, state: dict) -> dict:
    """One bridge request. state carries the client and the live session between calls."""
    command = request.get("command", "health")
    if command == "health":
        return {"service": "ORCA-MONEY-BOT", "bridge": "local-json-v1", "read_only_default": True}
    if command == "config":
        return {"mode": cfg.mode.value, "symbols": list(cfg.symbols),
                "live_enabled": cfg.mode is Mode.LIVE, "api_key": cfg.masked_key}
    if command == "backtest":
        return backtest(load_csv(_csv_path(request.get("csv"))), cfg)

    if state.get("client") is None:
        state["client"] = BinanceREST(cfg)
    client = state["client"]

    if command == "market_data":
        interval = str(request.get("interval", "1h"))
        if interval not in ALLOWED_INTERVALS:
            raise ValueError("unsupported interval")
        symbol = _symbol(request.get("symbol"), cfg)
        candles = client.klines(symbol, interval, _request_limit(request.get("limit", 50)))
        return {"symbol": symbol, "candles": [c.__dict__ for c in candles]}
    if command == "signal":
        symbol = _symbol(request.get("symbol"), cfg)
        candles = client.klines(symbol, cfg.kline_interval)
        # Decide on closed bars only, exactly as the live cycle does.
        closed = candles[:-1] if len(candles) > 1 else []
        if not closed:
            raise ValueError("not enough closed candles for a signal")
        return {"symbol": symbol, "signal": RegimeStrategy(cfg).decide(closed).value,
                "atr": atr(closed, cfg.atr_period)}
    if command == "live_cycle":
        if os.getenv("ALLOW_ORCA_LIVE_BRIDGE") != "1":
            raise PermissionError("live_cycle requires ALLOW_ORCA_LIVE_BRIDGE=1")
        if os.getenv("LIVE_TRADING_CONFIRM") != "I_UNDERSTAND_RISK":
            raise PermissionError("live_cycle requires LIVE_TRADING_CONFIRM=I_UNDERSTAND_RISK")
        if cfg.mode is not Mode.LIVE:
            raise PermissionError("live_cycle requires TRADING_MODE=live")
        cfg.validate()
        if state.get("live") is None:
            state["live"] = LiveSession(cfg, client)
        return state["live"].cycle(client)
    raise ValueError("unknown command: %s" % command)


def main() -> int:
    try:
        cfg = Config()
    except ValueError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}), flush=True)
        return 2
    state: dict = {"client": None, "live": None}
    for raw_line in sys.stdin:
        if len(raw_line.encode("utf-8")) > MAX_LINE_BYTES:
            print(json.dumps({"ok": False, "error": "request is too large"}), flush=True)
            continue
        try:
            request = json.loads(raw_line)
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
            print(json.dumps({"ok": True, "result": handle(request, cfg, state)}, default=str), flush=True)
        except Exception as exc:  # noqa: BLE001 - one bad request must not end the stream
            print(json.dumps({"ok": False, "error": str(exc)}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
