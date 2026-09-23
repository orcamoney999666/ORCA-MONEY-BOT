import io
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("TRADING_MODE", "paper")
from binance_trading_bot import BinanceREST, Candle, Config, Mode, PositionLedger, RiskGate
import orca_bridge
from orca_bridge import LiveSession, handle


def _live_config(**overrides) -> Config:
    base = dict(mode=Mode.LIVE, api_key="key", api_secret="secret", live_confirmation="I_UNDERSTAND_RISK")
    base.update(overrides)
    return Config(**base)


def _candles(n=70):
    """n hourly bars, the last one forming right now, as Binance returns them."""
    hour_ms = 3600 * 1000
    current = int(time.time() * 1000) // hour_ms * hour_ms
    out, price = [], 100.0
    for i in range(n):
        price += 0.5
        out.append(Candle(current - (n - 1 - i) * hour_ms, price - 0.2, price + 1.0, price - 1.0, price))
    return out


class BridgeReadOnlyTests(unittest.TestCase):
    def test_health_needs_no_client(self):
        state = {"client": None, "live": None}
        result = handle({"command": "health"}, Config(), state)
        self.assertTrue(result["read_only_default"])
        self.assertIsNone(state["client"])

    def test_config_reports_masked_key_only(self):
        result = handle({"command": "config"}, _live_config(api_key="ABCD12345678WXYZ"),
                        {"client": None, "live": None})
        self.assertEqual(result["api_key"], "ABCD...WXYZ")
        self.assertNotIn("secret", json_dump(result))

    def test_unknown_command_is_rejected(self):
        with self.assertRaises(ValueError):
            handle({"command": "rm -rf"}, Config(), {"client": None, "live": None})

    def test_unsupported_interval_is_rejected(self):
        state = {"client": MagicMock(spec=BinanceREST), "live": None}
        with self.assertRaises(ValueError):
            handle({"command": "market_data", "interval": "7h"}, Config(), state)

    def test_signal_uses_closed_candles_only(self):
        client = MagicMock(spec=BinanceREST)
        client.klines.return_value = _candles()[:-1] + [Candle(999, 1.0, 1.0, 1.0, 1.0)]
        state = {"client": client, "live": None}
        result = handle({"command": "signal", "symbol": "BTCUSDT"}, Config(), state)
        # The absurd in-progress bar would force HOLD if it were counted.
        self.assertEqual(result["signal"], "BUY")

    def test_signal_refuses_a_stale_feed(self):
        client = MagicMock(spec=BinanceREST)
        client.klines.return_value = [Candle(c.timestamp - 48 * 3600 * 1000, c.open, c.high, c.low, c.close)
                                      for c in _candles()]
        with self.assertRaises(ValueError):
            handle({"command": "signal", "symbol": "BTCUSDT"}, Config(), {"client": client, "live": None})


class BridgeLiveGateTests(unittest.TestCase):
    """The bridge is a second door onto live trading, so it carries the same locks."""

    def setUp(self):
        self.env = patch.dict(os.environ, {"ALLOW_ORCA_LIVE_BRIDGE": "", "LIVE_TRADING_CONFIRM": ""})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_live_cycle_needs_the_bridge_opt_in(self):
        with self.assertRaises(PermissionError):
            handle({"command": "live_cycle"}, _live_config(), {"client": None, "live": None})

    def test_live_cycle_needs_the_trading_confirmation(self):
        with patch.dict(os.environ, {"ALLOW_ORCA_LIVE_BRIDGE": "1"}):
            with self.assertRaises(PermissionError):
                handle({"command": "live_cycle"}, _live_config(), {"client": None, "live": None})

    def test_live_cycle_refuses_outside_live_mode(self):
        with patch.dict(os.environ, {"ALLOW_ORCA_LIVE_BRIDGE": "1",
                                     "LIVE_TRADING_CONFIRM": "I_UNDERSTAND_RISK"}):
            with self.assertRaises(PermissionError):
                handle({"command": "live_cycle"}, Config(), {"client": None, "live": None})


class BridgeLiveSessionTests(unittest.TestCase):
    def _client(self):
        client = MagicMock(spec=BinanceREST)
        client.get_api_key_permissions.return_value = {"enableWithdrawals": False, "ipRestrict": True}
        client.get_open_orders.return_value = []
        client.klines.return_value = _candles()
        client.weight_is_critical.return_value = False
        client.account.return_value = {"balances": [{"asset": "USDT", "free": "10000", "locked": "0"}]}
        client.ticker_price.return_value = 134.0
        client.get_symbol_filters.return_value = {"step_size": None, "min_qty": None,
                                                  "tick_size": None, "min_notional": None}
        client.market_order.return_value = {"status": "FILLED", "executedQty": "1.0",
                                            "cummulativeQuoteQty": "134.0", "fills": []}
        client.place_oco_order.return_value = {"orderListId": 1}
        client.get_my_trades.return_value = []
        return client

    def test_the_session_refuses_a_key_that_can_withdraw(self):
        client = self._client()
        client.get_api_key_permissions.return_value = {"enableWithdrawals": True, "ipRestrict": True}
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _live_config(symbols=("BTCUSDT",), risk_state_path=Path(tmp) / "r.json",
                               positions_path=Path(tmp) / "p.json", event_log_path=Path(tmp) / "e.jsonl")
            with self.assertRaises(RuntimeError):
                LiveSession(cfg, client)

    def test_risk_state_and_positions_persist_across_requests(self):
        """A fresh RiskGate per request would reset every loss limit on each call."""
        client = self._client()
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _live_config(symbols=("BTCUSDT",), max_trades_per_hour=1,
                               risk_state_path=Path(tmp) / "r.json", positions_path=Path(tmp) / "p.json",
                               event_log_path=Path(tmp) / "e.jsonl")
            session = LiveSession(cfg, client)
            session.cycle(client)
            self.assertEqual(client.market_order.call_count, 1)
            self.assertTrue((Path(tmp) / "r.json").exists())
            self.assertTrue(session.ledger.holds("BTCUSDT"))

            # Second request through the same bridge process: still in the trade.
            client.get_open_orders.return_value = [{"orderId": 1}]
            result = session.cycle(client)
            self.assertEqual(result["results"][0]["reason"], "position-open")
            self.assertEqual(client.market_order.call_count, 1)

    def test_each_bridge_cycle_is_written_to_the_event_log(self):
        client = self._client()
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "e.jsonl"
            cfg = _live_config(symbols=("BTCUSDT",), risk_state_path=Path(tmp) / "r.json",
                               positions_path=Path(tmp) / "p.json", event_log_path=log)
            LiveSession(cfg, client).cycle(client)
            event = json.loads(log.read_text().splitlines()[-1])
        self.assertEqual((event["event"], event["source"]), ("cycle", "bridge"))
        self.assertEqual(event["results"][0]["action"], "opened")

    def test_a_restarted_bridge_reloads_the_open_position(self):
        client = self._client()
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _live_config(symbols=("BTCUSDT",), risk_state_path=Path(tmp) / "r.json",
                               positions_path=Path(tmp) / "p.json", event_log_path=Path(tmp) / "e.jsonl")
            LiveSession(cfg, client).cycle(client)
            reborn = LiveSession(cfg, client)
        self.assertTrue(reborn.ledger.holds("BTCUSDT"))
        self.assertGreater(reborn.risk.trade_times, [])


def run_bridge(lines, cfg=None) -> list:
    """Drive main() over canned stdin and decode every response line."""
    out = io.StringIO()
    with patch.object(orca_bridge, "Config", MagicMock(return_value=cfg if cfg is not None else Config())):
        orca_bridge.main(stdin=io.StringIO("".join(lines)), stdout=out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


class BridgeStreamTests(unittest.TestCase):
    def test_malformed_json_does_not_stop_the_stream(self):
        responses = run_bridge(['not json\n', '{"command":"health"}\n'])
        self.assertFalse(responses[0]["ok"])
        self.assertTrue(responses[1]["ok"])

    def test_blank_lines_are_ignored(self):
        self.assertEqual(len(run_bridge(['\n', '   \n', '{"command":"health"}\n'])), 1)

    def test_a_request_must_be_a_json_object(self):
        self.assertIn("JSON object", run_bridge(['[1,2,3]\n'])[0]["error"])

    def test_an_oversized_line_is_refused_and_the_stream_keeps_serving(self):
        flood = "x" * (orca_bridge.MAX_LINE_BYTES + 10) + "\n"
        responses = run_bridge([flood, '{"command":"health"}\n'])
        self.assertEqual(responses[0]["error"], "request is too large")
        self.assertTrue(responses[1]["ok"])

    def test_each_read_is_bounded(self):
        """Iterating stdin would buffer a whole unterminated flood before any size check."""
        stream = MagicMock()
        stream.readline.return_value = ""
        self.assertIsNone(orca_bridge._read_line(stream))
        stream.readline.assert_called_once_with(orca_bridge.MAX_LINE_BYTES + 1)

    def test_multibyte_text_is_measured_in_bytes(self):
        line = "\u0639" * (orca_bridge.MAX_LINE_BYTES // 2 + 1) + "\n"   # 2 bytes per character
        self.assertEqual(orca_bridge._read_line(io.StringIO(line)), "")


class BridgeCsvTests(unittest.TestCase):
    def _root(self, root):
        return patch.dict(os.environ, {"ORCA_BRIDGE_CSV_DIR": root})

    def test_a_csv_outside_the_allowed_folder_is_refused(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as elsewhere:
            private = Path(elsewhere) / "private.csv"
            private.write_text("timestamp,open,high,low,close\n")
            with self._root(root), self.assertRaises(PermissionError):
                orca_bridge._csv_path(str(private))

    def test_a_relative_csv_is_read_from_the_allowed_folder(self):
        with tempfile.TemporaryDirectory() as root:
            candles = Path(root) / "candles.csv"
            candles.write_text("timestamp,open,high,low,close\n")
            with self._root(root):
                self.assertEqual(orca_bridge._csv_path("candles.csv"), str(candles.resolve()))

    def test_climbing_out_of_the_folder_is_refused(self):
        with tempfile.TemporaryDirectory() as root, self._root(root), self.assertRaises(PermissionError):
            orca_bridge._csv_path("../escape.csv")

    def test_a_file_that_is_not_a_csv_is_refused(self):
        with tempfile.TemporaryDirectory() as root, self._root(root), self.assertRaises(ValueError):
            orca_bridge._csv_path("notes.txt")


def json_dump(value) -> str:
    return json.dumps(value, default=str)


if __name__ == "__main__":
    unittest.main()
