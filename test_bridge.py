import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("TRADING_MODE", "paper")
from binance_trading_bot import BinanceREST, Candle, Config, Mode, PositionLedger, RiskGate
from orca_bridge import LiveSession, handle


def _live_config(**overrides) -> Config:
    base = dict(mode=Mode.LIVE, api_key="key", api_secret="secret", live_confirmation="I_UNDERSTAND_RISK")
    base.update(overrides)
    return Config(**base)


def _candles(n=70):
    out, price = [], 100.0
    for i in range(n):
        price += 0.5
        out.append(Candle(i, price - 0.2, price + 1.0, price - 1.0, price))
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
        client.klines.return_value = _candles() + [Candle(999, 1.0, 1.0, 1.0, 1.0)]
        state = {"client": client, "live": None}
        result = handle({"command": "signal", "symbol": "BTCUSDT"}, Config(), state)
        # The absurd in-progress bar would force HOLD if it were counted.
        self.assertEqual(result["signal"], "BUY")


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
                               positions_path=Path(tmp) / "p.json")
            with self.assertRaises(RuntimeError):
                LiveSession(cfg, client)

    def test_risk_state_and_positions_persist_across_requests(self):
        """A fresh RiskGate per request would reset every loss limit on each call."""
        client = self._client()
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _live_config(symbols=("BTCUSDT",), max_trades_per_hour=1,
                               risk_state_path=Path(tmp) / "r.json", positions_path=Path(tmp) / "p.json")
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

    def test_a_restarted_bridge_reloads_the_open_position(self):
        client = self._client()
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _live_config(symbols=("BTCUSDT",), risk_state_path=Path(tmp) / "r.json",
                               positions_path=Path(tmp) / "p.json")
            LiveSession(cfg, client).cycle(client)
            reborn = LiveSession(cfg, client)
        self.assertTrue(reborn.ledger.holds("BTCUSDT"))
        self.assertGreater(reborn.risk.trade_times, [])


def json_dump(value) -> str:
    import json
    return json.dumps(value, default=str)


if __name__ == "__main__":
    unittest.main()
