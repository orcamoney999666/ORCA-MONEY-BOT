# Direct integrations

`orca_bridge.py` is a dependency-free local JSON bridge for companion repositories.

```bash
printf '%s\n' '{"command":"health"}' | python3 orca_bridge.py
printf '%s\n' '{"command":"signal","symbol":"BTCUSDT"}' | python3 orca_bridge.py
```

Supported commands: `health`, `config`, `market_data`, `signal`, `backtest`, and guarded
`live_cycle`. Set `ALLOW_ORCA_LIVE_BRIDGE=1` explicitly before allowing live cycles.
