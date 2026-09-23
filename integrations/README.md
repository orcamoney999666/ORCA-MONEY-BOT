# Direct integrations

`orca_bridge.py` is a dependency-free newline-delimited JSON bridge.

```bash
printf '%s\n' '{"command":"health"}' | python3 orca_bridge.py
printf '%s\n' '{"command":"signal","symbol":"BTCUSDT"}' | python3 orca_bridge.py
```

Supported commands: `health`, `config`, `market_data`, `signal`, `backtest`, and guarded
`live_cycle`. Read-only requests never execute orders.

## Live cycles

`live_cycle` needs all three of these, and is refused otherwise:

- `ALLOW_ORCA_LIVE_BRIDGE=1`
- `LIVE_TRADING_CONFIRM=I_UNDERSTAND_RISK`
- `TRADING_MODE=live`

Each cycle reloads the risk state from `RISK_STATE_PATH` before it runs and writes it back
afterwards — including when the cycle fails partway, since orders may already have gone out.
The daily-loss, drawdown and hourly-trade limits therefore carry across bridge requests and
are shared with the `live` CLI loop, instead of resetting on every call.

## Reading CSVs

`backtest` only reads files inside one directory, `data/` by default. Point
`ORCA_BRIDGE_CSV_DIR` somewhere else to change it; paths outside it are refused, so a
companion process cannot read arbitrary CSVs off the machine.

## Tests

`test_orca_bridge.py` covers the command surface, the live gate, the risk-state round trip
and the input limits. It runs with the rest of the suite:

```bash
python3 -m unittest discover
```
