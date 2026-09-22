# Direct integrations

`orca_bridge.py` is a dependency-free newline-delimited JSON bridge.

```bash
printf '%s\n' '{"command":"health"}' | python3 orca_bridge.py
printf '%s\n' '{"command":"signal","symbol":"BTCUSDT"}' | python3 orca_bridge.py
```

Supported commands: `health`, `config`, `market_data`, `signal`, `backtest`, and guarded
`live_cycle`. Live requires both `ALLOW_ORCA_LIVE_BRIDGE=1` and
`LIVE_TRADING_CONFIRM=I_UNDERSTAND_RISK`. Read-only requests never execute orders.
