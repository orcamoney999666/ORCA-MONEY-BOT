#!/usr/bin/env python3
"""Dependency-free NDJSON bridge for companion projects.

Read-only commands are available by default. Live execution requires two explicit
operator opt-ins and still passes through the bot's normal Config validation.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from binance_trading_bot import Config, BinanceREST, RegimeStrategy, atr, backtest, load_csv, load_positions
from oracle import MarketOracle, OracleConfig
from monitoring import JsonlMonitor

MAX_LINE_BYTES = 256 * 1024
MAX_CANDLES = 1000
ALLOWED_INTERVALS = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w"}


def _request_limit(value: object) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc
    if not 2 <= limit <= MAX_CANDLES:
        raise ValueError(f"limit must be between 2 and {MAX_CANDLES}")
    return limit


def _symbol(value: object, cfg: Config) -> str:
    symbol = str(value or (cfg.symbols[0] if cfg.symbols else "")).upper()
    if not symbol.isalnum() or not symbol:
        raise ValueError("symbol must be an alphanumeric Binance symbol")
    return symbol


def _csv_path(value: object) -> str:
    if not value:
        raise ValueError("csv is required")
    path = Path(str(value)).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"CSV file not found: {path}")
    if path.suffix.lower() != ".csv":
        raise ValueError("csv must point to a .csv file")
    return str(path)


def main() -> int:
    # Config is constructed once, but live validation is only required for live_cycle.
    cfg = Config()
    client = None
    for raw_line in sys.stdin:
        if len(raw_line.encode("utf-8")) > MAX_LINE_BYTES:
            print(json.dumps({"ok": False, "error": "request is too large"}), flush=True)
            continue
        try:
            request = json.loads(raw_line)
            if not isinstance(request, dict):
                raise ValueError("request must be a JSON object")
            command = request.get("command", "health")
            if command == "health":
                result = {"service": "ORCA-MONEY-BOT", "bridge": "local-json-v1", "read_only_default": True}
            elif command == "config":
                result = {"mode": cfg.mode.value, "symbols": list(cfg.symbols), "live_enabled": cfg.mode.value == "live"}
            elif command == "market_data":
                interval = str(request.get("interval", "1h"))
                client = client or BinanceREST(cfg)
                symbol = _symbol(request.get("symbol"), cfg)
                oracle = MarketOracle(client.klines, OracleConfig(cfg.oracle_max_age_seconds, cfg.oracle_interval_seconds))
                candles = oracle.candles(symbol, interval, _request_limit(request.get("limit", 50)))
                result = {"symbol": symbol, "candles": [c.__dict__ for c in candles]}
            elif command == "signal":
                client = client or BinanceREST(cfg)
                symbol = _symbol(request.get("symbol"), cfg)
                oracle = MarketOracle(client.klines, OracleConfig(cfg.oracle_max_age_seconds, cfg.oracle_interval_seconds))
                candles = oracle.candles(symbol)
                result = {"symbol": symbol, "signal": RegimeStrategy(cfg).decide(candles).value, "atr": atr(candles, cfg.atr_period)}
            elif command == "backtest":
                result = backtest(load_csv(_csv_path(request.get("csv"))), cfg)
            elif command == "live_cycle":
                if os.getenv("ALLOW_ORCA_LIVE_BRIDGE") != "1":
                    raise PermissionError("live_cycle requires ALLOW_ORCA_LIVE_BRIDGE=1")
                if os.getenv("LIVE_TRADING_CONFIRM") != "I_UNDERSTAND_RISK":
                    raise PermissionError("live_cycle requires LIVE_TRADING_CONFIRM=I_UNDERSTAND_RISK")
                from binance_trading_bot import RiskGate, run_live_cycle
                cfg.validate()
                client = client or BinanceREST(cfg)
                oracle = MarketOracle(client.klines, OracleConfig(cfg.oracle_max_age_seconds, cfg.oracle_interval_seconds))
                result = run_live_cycle(cfg, client, RiskGate(cfg), RegimeStrategy(cfg), load_positions(cfg.positions_path), oracle, JsonlMonitor(cfg.event_log_path))
            else:
                raise ValueError(f"unknown command: {command}")
            print(json.dumps({"ok": True, "result": result}, default=str), flush=True)
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
