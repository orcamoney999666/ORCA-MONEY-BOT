# Direct integrations

`orca_bridge.py` is a dependency-free newline-delimited JSON bridge.

```bash
printf '%s\n' '{"command":"health"}' | python3 orca_bridge.py
printf '%s\n' '{"command":"signal","symbol":"BTCUSDT"}' | python3 orca_bridge.py
```

Supported commands: `health`, `config`, `market_data`, `signal`, `backtest`, and guarded
`live_cycle`. Live requires both `ALLOW_ORCA_LIVE_BRIDGE=1` and
`LIVE_TRADING_CONFIRM=I_UNDERSTAND_RISK`, plus `TRADING_MODE=live`. Read-only requests
never execute orders.

`config` reports the API key masked (`ABCD...WXYZ`), never in full.

## Live requests share one session

The first `live_cycle` request builds a `LiveSession`: it checks the API key's
restrictions, syncs the clock, loads the persisted `RiskGate` state and the position
ledger, and every later request in the same process reuses them. That is deliberate — a
fresh `RiskGate` per request would reset the daily-loss, drawdown and hourly limits on
every call, and a bridge with no ledger would not know it already holds a position.

State is written back to `RISK_STATE_PATH` after each cycle, so a restarted bridge picks
up where it left off.
