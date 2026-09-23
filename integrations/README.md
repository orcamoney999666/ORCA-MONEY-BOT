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

## Input limits

The bridge talks to other local processes, so it bounds what they can send:

- Each request line is read at most 256 KiB at a time. A longer line is answered with
  `request is too large` and discarded, and the bridge keeps serving the next one.
- Blank lines are ignored; anything that is not a JSON object is answered with an error.
- `backtest` only reads `.csv` files inside one folder, `data/` by default. Set
  `ORCA_BRIDGE_CSV_DIR` to change it. A relative path is taken from that folder, and a path
  outside it (including one that climbs out with `..`) is refused.

## Data checks and the audit log

`signal` runs the same market-data checks as the live cycle: a stale feed, a missing bar,
or a malformed candle is answered with an error instead of a signal. Every `live_cycle`
is also appended to `EVENT_LOG_PATH` with `"source": "bridge"`, next to the events the
`live` command writes.
