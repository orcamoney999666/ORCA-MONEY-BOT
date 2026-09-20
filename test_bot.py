import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("TRADING_MODE", "paper")
from binance_trading_bot import BinanceREST, Candle, Config, Mode, RegimeStrategy, RiskGate, Signal, atr, backtest

class BotTests(unittest.TestCase):
    def test_atr_is_positive(self):
        candles = [Candle(i, 100+i, 102+i, 98+i, 101+i) for i in range(20)]
        self.assertGreater(atr(candles, 14), 0)

    def test_risk_gate_rejects_hold(self):
        gate = RiskGate(Config())
        ok, qty, reason = gate.approve(Signal.HOLD, 100, 2, 0)
        self.assertFalse(ok); self.assertEqual(qty, 0); self.assertEqual(reason, "no-trade-condition")

    def test_backtest_returns_metrics(self):
        candles = []
        price = 100.0
        for i in range(140):
            price += 0.2 if (i // 20) % 2 == 0 else -0.15
            candles.append(Candle(i, price-0.2, price+1.0, price-1.0, price))
        result = backtest(candles, Config())
        self.assertIn("pnl", result); self.assertIn("trades", result)

    def test_empty_backtest_is_safe(self):
        result = backtest([], Config())
        self.assertEqual(result["trades"], 0)

    def test_hourly_limit(self):
        cfg = Config(max_trades_per_hour=1)
        gate = RiskGate(cfg)
        gate.closed(-1)
        ok, _, reason = gate.approve(Signal.BUY, 100, 2, 0)
        self.assertFalse(ok); self.assertEqual(reason, "hourly-trade-limit")

def _mock_response(payload: bytes):
    resp = MagicMock()
    resp.read.return_value = payload
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def _live_config() -> Config:
    return Config(mode=Mode.LIVE, api_key="key", api_secret="secret", live_confirmation="I_UNDERSTAND_RISK")


class BinanceRESTOrderTests(unittest.TestCase):
    """These never touch the network: urllib.request.urlopen is mocked throughout."""

    def test_market_order_blocked_outside_live_mode(self):
        client = BinanceREST(Config())
        with self.assertRaises(RuntimeError):
            client.market_order("BTCUSDT", "BUY", 0.01)

    def test_place_oco_order_blocked_outside_live_mode(self):
        client = BinanceREST(Config())
        with self.assertRaises(RuntimeError):
            client.place_oco_order("BTCUSDT", "SELL", 0.01, take_profit_price=120, stop_price=95, stop_limit_price=94)

    def test_cancel_order_blocked_outside_live_mode(self):
        client = BinanceREST(Config())
        with self.assertRaises(RuntimeError):
            client.cancel_order("BTCUSDT", 1)

    def test_cancel_oco_order_blocked_outside_live_mode(self):
        client = BinanceREST(Config())
        with self.assertRaises(RuntimeError):
            client.cancel_oco_order("BTCUSDT", 1)

    @patch("binance_trading_bot.urllib.request.urlopen")
    def test_market_order_sends_post(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(b'{"status": "FILLED"}')
        BinanceREST(_live_config()).market_order("BTCUSDT", "BUY", 0.01)
        request = mock_urlopen.call_args[0][0]
        self.assertEqual(request.get_method(), "POST")
        self.assertIn("/api/v3/order?", request.full_url)

    @patch("binance_trading_bot.urllib.request.urlopen")
    def test_place_oco_order_sends_post_to_oco_endpoint(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(b'{"orderListId": 1}')
        BinanceREST(_live_config()).place_oco_order("BTCUSDT", "SELL", 0.01, take_profit_price=120, stop_price=95, stop_limit_price=94)
        request = mock_urlopen.call_args[0][0]
        self.assertEqual(request.get_method(), "POST")
        self.assertIn("/api/v3/order/oco?", request.full_url)
        self.assertIn("stopPrice=95", request.full_url)

    @patch("binance_trading_bot.urllib.request.urlopen")
    def test_cancel_order_sends_delete(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(b'{"status": "CANCELED"}')
        BinanceREST(_live_config()).cancel_order("BTCUSDT", 123)
        request = mock_urlopen.call_args[0][0]
        self.assertEqual(request.get_method(), "DELETE")

    @patch("binance_trading_bot.urllib.request.urlopen")
    def test_cancel_oco_order_sends_delete_to_orderlist_endpoint(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(b'{"listOrderStatus": "ALL_DONE"}')
        BinanceREST(_live_config()).cancel_oco_order("BTCUSDT", 7)
        request = mock_urlopen.call_args[0][0]
        self.assertEqual(request.get_method(), "DELETE")
        self.assertIn("/api/v3/orderList?", request.full_url)

    @patch("binance_trading_bot.urllib.request.urlopen")
    def test_get_open_orders_sends_get(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(b"[]")
        result = BinanceREST(_live_config()).get_open_orders("BTCUSDT")
        request = mock_urlopen.call_args[0][0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(result, [])

    @patch("binance_trading_bot.urllib.request.urlopen")
    def test_get_order_sends_get(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(b'{"status": "NEW"}')
        result = BinanceREST(_live_config()).get_order("BTCUSDT", 123)
        request = mock_urlopen.call_args[0][0]
        self.assertEqual(request.get_method(), "GET")
        self.assertEqual(result["status"], "NEW")


if __name__ == "__main__": unittest.main()
