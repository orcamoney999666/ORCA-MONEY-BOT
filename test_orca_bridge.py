import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("TRADING_MODE", "paper")

import orca_bridge
from binance_trading_bot import Config, Mode, RiskGate, save_risk_state

LIVE_ENV = {"ALLOW_ORCA_LIVE_BRIDGE": "1", "LIVE_TRADING_CONFIRM": "I_UNDERSTAND_RISK"}


def live_config(tmp: str) -> Config:
    return Config(mode=Mode.LIVE, live_confirmation="I_UNDERSTAND_RISK", api_key="k", api_secret="s",
                  risk_state_path=Path(tmp) / "risk_state.json")


def run_bridge(lines: list[str], cfg: Config = None) -> list[dict]:
    """Drive main() over canned stdin and decode every response line."""
    out = io.StringIO()
    with patch.object(orca_bridge, "Config", MagicMock(return_value=cfg if cfg is not None else Config())):
        orca_bridge.main(stdin=io.StringIO("".join(lines)), stdout=out)
    return [json.loads(line) for line in out.getvalue().splitlines()]


class RequestHandlingTests(unittest.TestCase):
    def test_health_is_the_default_command(self):
        self.assertEqual(run_bridge(['{}\n'])[0]["result"]["service"], "ORCA-MONEY-BOT")

    def test_config_reports_mode_and_symbols(self):
        result = run_bridge(['{"command":"config"}\n'])[0]["result"]
        self.assertEqual(result["mode"], "paper")
        self.assertFalse(result["live_enabled"])

    def test_unknown_command_is_an_error(self):
        response = run_bridge(['{"command":"nope"}\n'])[0]
        self.assertFalse(response["ok"])
        self.assertIn("unknown command", response["error"])

    def test_malformed_json_does_not_stop_the_loop(self):
        responses = run_bridge(['not json\n', '{"command":"health"}\n'])
        self.assertFalse(responses[0]["ok"])
        self.assertTrue(responses[1]["ok"])

    def test_blank_lines_are_ignored(self):
        self.assertEqual(len(run_bridge(['\n', '   \n', '{"command":"health"}\n'])), 1)

    def test_request_must_be_a_json_object(self):
        self.assertIn("JSON object", run_bridge(['[1,2,3]\n'])[0]["error"])


class OversizedInputTests(unittest.TestCase):
    def test_oversized_line_is_rejected_without_being_buffered_whole(self):
        flood = "x" * (orca_bridge.MAX_LINE_BYTES + 10) + "\n"
        responses = run_bridge([flood, '{"command":"health"}\n'])
        self.assertEqual(responses[0]["error"], "request is too large")
        self.assertTrue(responses[1]["ok"], "the bridge keeps serving after an oversized line")

    def test_read_line_bounds_each_read(self):
        stream = MagicMock()
        stream.readline.return_value = ""
        self.assertIsNone(orca_bridge._read_line(stream))
        stream.readline.assert_called_once_with(orca_bridge.MAX_LINE_BYTES + 1)


class ValidationTests(unittest.TestCase):
    def test_interval_must_be_supported(self):
        self.assertIn("unsupported interval", run_bridge(['{"command":"market_data","interval":"7h"}\n'])[0]["error"])

    def test_limit_must_be_within_range(self):
        with self.assertRaises(ValueError):
            orca_bridge._request_limit(orca_bridge.MAX_CANDLES + 1)
        with self.assertRaises(ValueError):
            orca_bridge._request_limit(0)
        with self.assertRaises(ValueError):
            orca_bridge._request_limit("abc")

    def test_symbol_must_be_alphanumeric(self):
        cfg = Config()
        self.assertEqual(orca_bridge._symbol("btcusdt", cfg), "BTCUSDT")
        with self.assertRaises(ValueError):
            orca_bridge._symbol("BTC/USDT", cfg)
        with self.assertRaises(ValueError):
            orca_bridge._symbol("", Config(symbols=()))


class CsvPathTests(unittest.TestCase):
    def test_csv_outside_the_allowed_root_is_refused(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as elsewhere:
            secret = Path(elsewhere) / "private.csv"
            secret.write_text("timestamp,open,high,low,close\n")
            with patch.dict(os.environ, {"ORCA_BRIDGE_CSV_DIR": root}):
                with self.assertRaises(PermissionError):
                    orca_bridge._csv_path(str(secret))

    def test_csv_inside_the_allowed_root_is_accepted(self):
        with tempfile.TemporaryDirectory() as root:
            candles = Path(root) / "candles.csv"
            candles.write_text("timestamp,open,high,low,close\n")
            with patch.dict(os.environ, {"ORCA_BRIDGE_CSV_DIR": root}):
                self.assertEqual(orca_bridge._csv_path("candles.csv"), str(candles.resolve()))

    def test_traversal_out_of_the_root_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.dict(os.environ, {"ORCA_BRIDGE_CSV_DIR": root}):
                with self.assertRaises(PermissionError):
                    orca_bridge._csv_path("../escape.csv")

    def test_non_csv_suffix_is_refused(self):
        with tempfile.TemporaryDirectory() as root:
            with patch.dict(os.environ, {"ORCA_BRIDGE_CSV_DIR": root}):
                with self.assertRaises(ValueError):
                    orca_bridge._csv_path("notes.txt")


class LiveGateTests(unittest.TestCase):
    def test_live_cycle_needs_the_bridge_opt_in(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {**LIVE_ENV, "ALLOW_ORCA_LIVE_BRIDGE": ""}):
            response = run_bridge(['{"command":"live_cycle"}\n'], live_config(tmp))[0]
            self.assertIn("ALLOW_ORCA_LIVE_BRIDGE", response["error"])

    def test_live_cycle_needs_the_risk_confirmation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {**LIVE_ENV, "LIVE_TRADING_CONFIRM": ""}):
            response = run_bridge(['{"command":"live_cycle"}\n'], live_config(tmp))[0]
            self.assertIn("LIVE_TRADING_CONFIRM", response["error"])

    def test_live_cycle_is_refused_outside_live_mode(self):
        with patch.dict(os.environ, LIVE_ENV):
            response = run_bridge(['{"command":"live_cycle"}\n'], Config(mode=Mode.PAPER))[0]
            self.assertFalse(response["ok"])
            self.assertIn("TRADING_MODE=live", response["error"])

    def test_paper_mode_never_reaches_the_cycle(self):
        with patch.dict(os.environ, LIVE_ENV), patch.object(orca_bridge, "run_live_cycle") as cycle:
            run_bridge(['{"command":"live_cycle"}\n'], Config(mode=Mode.PAPER))
            cycle.assert_not_called()


class LiveRiskStateTests(unittest.TestCase):
    def test_live_cycle_resumes_the_persisted_risk_state(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, LIVE_ENV):
            cfg = live_config(tmp)
            spent = RiskGate(cfg)
            spent.closed(-120.0)
            save_risk_state(cfg.risk_state_path, spent)

            seen = {}
            with patch.object(orca_bridge, "run_live_cycle", side_effect=lambda c, cl, risk, s: seen.update(
                    equity=risk.equity, daily_pnl=risk.daily_pnl, trades=risk.trades) or {"results": []}):
                run_bridge(['{"command":"live_cycle"}\n'], cfg)

            self.assertEqual(seen["daily_pnl"], -120.0, "a fresh gate would have reset the daily loss to 0")
            self.assertEqual(seen["equity"], cfg.initial_equity - 120.0)
            self.assertEqual(seen["trades"], 1)

    def test_live_cycle_writes_the_risk_state_back(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, LIVE_ENV):
            cfg = live_config(tmp)
            with patch.object(orca_bridge, "run_live_cycle", side_effect=lambda c, cl, risk, s: risk.closed(-30.0) or {"results": []}):
                run_bridge(['{"command":"live_cycle"}\n'], cfg)
            self.assertEqual(json.loads(cfg.risk_state_path.read_text())["daily_pnl"], -30.0)

    def test_risk_state_survives_a_failing_cycle(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, LIVE_ENV):
            cfg = live_config(tmp)

            def blow_up(c, cl, risk, s):
                risk.closed(-45.0)
                raise RuntimeError("bracket order rejected")

            with patch.object(orca_bridge, "run_live_cycle", side_effect=blow_up):
                response = run_bridge(['{"command":"live_cycle"}\n'], cfg)[0]
            self.assertFalse(response["ok"])
            self.assertEqual(json.loads(cfg.risk_state_path.read_text())["daily_pnl"], -45.0,
                             "orders may already have gone out, so the loss must still be recorded")

    def test_repeated_bridge_calls_accumulate_instead_of_resetting(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, LIVE_ENV):
            cfg = live_config(tmp)
            with patch.object(orca_bridge, "run_live_cycle", side_effect=lambda c, cl, risk, s: risk.closed(-10.0) or {"results": []}):
                run_bridge(['{"command":"live_cycle"}\n'] * 3, cfg)
            self.assertEqual(json.loads(cfg.risk_state_path.read_text())["trades"], 3)
            self.assertEqual(json.loads(cfg.risk_state_path.read_text())["daily_pnl"], -30.0)


if __name__ == "__main__":
    unittest.main()
