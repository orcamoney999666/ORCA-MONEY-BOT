#!/usr/bin/env python3
"""Local JSON bridge for ORCA Money Bot companion projects."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from binance_trading_bot import Config, BinanceREST, RegimeStrategy, atr, backtest, load_csv


def main() -> int:
    cfg = Config()
    client = None
    for line in sys.stdin:
        try:
            request = json.loads(line)
            command = request.get("command", "health")
            if command == "health":
                result = {"service": "ORCA-MONEY-BOT", "bridge": "local-json-v1"}
            elif command == "config":
                result = {"mode": cfg.mode.value, "symbols": list(cfg.symbols), "live_enabled": cfg.mode.value == "live"}
            elif command == "market_data":
                client = client or BinanceREST(cfg)
                symbol = str(request.get("symbol", cfg.symbols[0])).upper()
                candles = client.klines(symbol, str(request.get("interval", "1h")), int(request.get("limit", 50)))
                result = {"symbol": symbol, "candles": [c.__dict__ for c in candles]}
            elif command == "signal":
                client = client or BinanceREST(cfg)
                symbol = str(request.get("symbol", cfg.symbols[0])).upper()
                candles = client.klines(symbol)
                result = {"symbol": symbol, "signal": RegimeStrategy(cfg).decide(candles).value, "atr": atr(candles, cfg.atr_period)}
            elif command == "backtest":
                result = backtest(load_csv(str(Path(request["csv"]).resolve())), cfg)
            elif command == "live_cycle":
                if os.getenv("ALLOW_ORCA_LIVE_BRIDGE") != "1":
                    raise PermissionError("live_cycle requires ALLOW_ORCA_LIVE_BRIDGE=1")
                from binance_trading_bot import RiskGate, run_live_cycle
                cfg.validate()
                client = client or BinanceREST(cfg)
                result = run_live_cycle(cfg, client, RiskGate(cfg), RegimeStrategy(cfg))
            else:
                raise ValueError(f"unknown command: {command}")
            print(json.dumps({"ok": True, "result": result}, default=str), flush=True)
        except Exception as exc:
            print(json.dumps({"ok": False, "error": str(exc)}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
