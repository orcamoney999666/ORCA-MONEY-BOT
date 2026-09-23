#!/usr/bin/env python3
"""Dependency-free NDJSON bridge for companion projects.

Read-only commands are available by default. Live execution requires two explicit
operator opt-ins, live mode itself, and still passes through the bot's normal
Config validation. Live cycles run against the risk state persisted at
RISK_STATE_PATH, so the daily-loss, drawdown and hourly-trade limits carry across
bridge requests exactly as they do across `live` runs.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from binance_trading_bot import (
    BinanceREST, Config, Mode, RegimeStrategy, RiskGate, atr, backtest, load_csv,
    load_risk_state, run_live_cycle, save_risk_state,
)

MAX_LINE_BYTES = 256 * 1024
MAX_CANDLES = 1000
ALLOWED_INTERVALS = {"1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w"}


def csv_root() -> Path:
    """Directory the `backtest` command may read CSV files from.

    The bridge talks to other local processes, so an unrestricted path would let a
    companion project read any .csv on the machine. Defaults to the bot's own data
    directory; override with ORCA_BRIDGE_CSV_DIR.
    """
    return Path(os.getenv("ORCA_BRIDGE_CSV_DIR", "data")).expanduser().resolve()


def _request_limit(value: object) -> int:
    try:
        limit = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc
    if not 1 <= limit <= MAX_CANDLES:
        raise ValueError(f"limit must be between 1 and {MAX_CANDLES}")
    return limit


def _symbol(value: object, cfg: Config) -> str:
    symbol = str(value or (cfg.symbols[0] if cfg.symbols else "")).upper()
    if not symbol.isalnum():
        raise ValueError("symbol must be an alphanumeric Binance symbol")
    return symbol


def _csv_path(value: object) -> str:
    if not value:
        raise ValueError("csv is required")
    root = csv_root()
    path = Path(str(value)).expanduser()
    path = (root / path).resolve() if not path.is_absolute() else path.resolve()
    if path != root and root not in path.parents:
        raise PermissionError(f"csv must be inside {root}")
    if path.suffix.lower() != ".csv":
        raise ValueError("csv must point to a .csv file")
    if not path.is_file():
        raise FileNotFoundError(f"CSV file not found: {path}")
    return str(path)


def _live_cycle(cfg: Config, client: BinanceREST) -> dict:
    """One live cycle, gated and bookkept the same way `run_live` does it.

    The gate is deliberately three separate opt-ins, and the risk state is reloaded
    before the cycle and written back afterwards — including when the cycle raises
    partway, since orders may already have gone out by then.
    """
    if os.getenv("ALLOW_ORCA_LIVE_BRIDGE") != "1":
        raise PermissionError("live_cycle requires ALLOW_ORCA_LIVE_BRIDGE=1")
    if os.getenv("LIVE_TRADING_CONFIRM") != "I_UNDERSTAND_RISK":
        raise PermissionError("live_cycle requires LIVE_TRADING_CONFIRM=I_UNDERSTAND_RISK")
    if cfg.mode is not Mode.LIVE:
        raise PermissionError("live_cycle requires TRADING_MODE=live")
    cfg.validate()
    risk = RiskGate(cfg)
    risk.restore(load_risk_state(cfg.risk_state_path))
    try:
        return run_live_cycle(cfg, client, risk, RegimeStrategy(cfg))
    finally:
        save_risk_state(cfg.risk_state_path, risk)


def handle(request: object, cfg: Config, session: dict) -> dict:
    """Run one decoded request. `session` caches the REST client between calls."""
    if not isinstance(request, dict):
        raise ValueError("request must be a JSON object")
    command = request.get("command", "health")

    def client() -> BinanceREST:
        if "client" not in session:
            session["client"] = BinanceREST(cfg)
        return session["client"]

    if command == "health":
        return {"service": "ORCA-MONEY-BOT", "bridge": "local-json-v1", "read_only_default": True}
    if command == "config":
        return {"mode": cfg.mode.value, "symbols": list(cfg.symbols), "live_enabled": cfg.mode is Mode.LIVE}
    if command == "market_data":
        interval = str(request.get("interval", "1h"))
        if interval not in ALLOWED_INTERVALS:
            raise ValueError("unsupported interval")
        symbol = _symbol(request.get("symbol"), cfg)
        candles = client().klines(symbol, interval, _request_limit(request.get("limit", 50)))
        return {"symbol": symbol, "candles": [c.__dict__ for c in candles]}
    if command == "signal":
        symbol = _symbol(request.get("symbol"), cfg)
        candles = client().klines(symbol)
        return {"symbol": symbol, "signal": RegimeStrategy(cfg).decide(candles).value, "atr": atr(candles, cfg.atr_period)}
    if command == "backtest":
        return backtest(load_csv(_csv_path(request.get("csv"))), cfg)
    if command == "live_cycle":
        return _live_cycle(cfg, client())
    raise ValueError(f"unknown command: {command}")


def _read_line(stream) -> str | None:
    """Read one line, bounded, so an unterminated flood cannot exhaust memory.

    Returns the line, "" for an over-long line (whose remainder is discarded), or
    None at end of input.
    """
    line = stream.readline(MAX_LINE_BYTES + 1)
    if not line:
        return None
    if len(line) > MAX_LINE_BYTES:
        while line and not line.endswith("\n"):
            line = stream.readline(MAX_LINE_BYTES + 1)
        return ""
    return line


def main(stdin=None, stdout=None) -> int:
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    cfg = Config()
    session: dict = {}
    while True:
        raw_line = _read_line(stdin)
        if raw_line is None:
            break
        if raw_line == "":
            print(json.dumps({"ok": False, "error": "request is too large"}), file=stdout, flush=True)
            continue
        if not raw_line.strip():
            continue
        try:
            payload = {"ok": True, "result": handle(json.loads(raw_line), cfg, session)}
        except Exception as exc:
            payload = {"ok": False, "error": str(exc)}
        print(json.dumps(payload, default=str), file=stdout, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
