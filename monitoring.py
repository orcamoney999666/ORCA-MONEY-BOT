#!/usr/bin/env python3
"""Dependency-free audit and monitoring primitives for ORCA-MONEY-BOT."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


@dataclass
class JsonlMonitor:
    path: Path
    dry_run: bool = False

    def emit(self, event: str, **fields: Any) -> dict[str, Any]:
        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "dry_run": self.dry_run,
            **fields,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        return record

    def health(self, mode: str, symbols: tuple[str, ...], oracle_ok: bool, consecutive_failures: int = 0) -> dict[str, Any]:
        return self.emit("health", mode=mode, symbols=list(symbols), oracle_ok=oracle_ok, consecutive_failures=consecutive_failures)
