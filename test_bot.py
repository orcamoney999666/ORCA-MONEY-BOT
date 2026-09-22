import json
import os
import signal
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("TRADING_MODE", "paper")
from binance_trading_bot import (
    BinanceError, BinanceREST, Candle, Config, Mode, RegimeStrategy, RiskGate, Signal, atr, backtest,
    load_risk_state, round_to_step, run_live, run_live_cycle, save_risk_state,
)

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


def _live_config(**overrides) -> Config:
    base = dict(mode=Mode.LIVE, api_key="key", api_secret="secret", live_confirmation="I_UNDERSTAND_RISK")
    base.update(overrides)
    return Config(**base)


def _trending_candles(n: int = 70, rising: bool = True) -> list[Candle]:
    candles = []
    price = 100.0
    step = 0.5 if rising else -0.5
    for i in range(n):
        price += step
        candles.append(Candle(i, price - 0.2, price + 1.0, price - 1.0, price))
    return candles


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

    @patch("binance_trading_bot.urllib.request.urlopen")
    def test_http_error_preserves_binance_body(self, mock_urlopen):
        from urllib.error import HTTPError
        mock_urlopen.side_effect = HTTPError("https://api.binance.com/api/v3/order", 400, "bad", {}, MagicMock(read=MagicMock(return_value=b'{"code":-2010,"msg":"insufficient balance"}')))
        with self.assertRaises(BinanceError) as caught:
            BinanceREST(_live_config()).get_order("BTCUSDT", 1)
        self.assertEqual(caught.exception.code, -2010)
        self.assertIn("insufficient balance", str(caught.exception))

    def test_config_repr_does_not_expose_secret(self):
        cfg = _live_config(api_secret="super-secret")
        self.assertNotIn("super-secret", repr(cfg))


class BinanceRESTConvertTests(unittest.TestCase):
    """Direct asset-to-asset conversion (Binance's Convert feature)."""

    def test_accept_convert_quote_blocked_outside_live_mode(self):
        client = BinanceREST(Config())
        with self.assertRaises(RuntimeError):
            client.accept_convert_quote("quote-1")

    def test_convert_blocked_outside_live_mode(self):
        client = BinanceREST(Config())
        with self.assertRaises(RuntimeError):
            client.convert("BTC", "ETH", 0.01)

    def test_get_convert_order_status_requires_an_id(self):
        client = BinanceREST(_live_config())
        with self.assertRaises(ValueError):
            client.get_convert_order_status()

    @patch("binance_trading_bot.urllib.request.urlopen")
    def test_get_convert_quote_sends_post_to_correct_endpoint(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(b'{"quoteId": "q1", "toAmount": "0.5"}')
        result = BinanceREST(_live_config()).get_convert_quote("BTC", "ETH", 0.01)
        request = mock_urlopen.call_args[0][0]
        self.assertEqual(request.get_method(), "POST")
        self.assertIn("/sapi/v1/convert/getQuote?", request.full_url)
        self.assertIn("fromAsset=BTC", request.full_url)
        self.assertIn("toAsset=ETH", request.full_url)
        self.assertEqual(result["quoteId"], "q1")

    @patch("binance_trading_bot.urllib.request.urlopen")
    def test_accept_convert_quote_sends_post(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(b'{"orderId": "o1", "orderStatus": "SUCCESS"}')
        result = BinanceREST(_live_config()).accept_convert_quote("q1")
        request = mock_urlopen.call_args[0][0]
        self.assertEqual(request.get_method(), "POST")
        self.assertIn("/sapi/v1/convert/acceptQuote?", request.full_url)
        self.assertIn("quoteId=q1", request.full_url)
        self.assertEqual(result["orderStatus"], "SUCCESS")

    @patch("binance_trading_bot.urllib.request.urlopen")
    def test_get_convert_order_status_sends_get(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(b'{"orderStatus": "SUCCESS"}')
        BinanceREST(_live_config()).get_convert_order_status(order_id="o1")
        request = mock_urlopen.call_args[0][0]
        self.assertEqual(request.get_method(), "GET")
        self.assertIn("orderId=o1", request.full_url)

    @patch.object(BinanceREST, "accept_convert_quote")
    @patch.object(BinanceREST, "get_convert_quote")
    def test_convert_quotes_then_accepts_the_returned_quote_id(self, mock_quote, mock_accept):
        mock_quote.return_value = {"quoteId": "q-42", "toAmount": "1.23"}
        mock_accept.return_value = {"orderId": "o-1", "orderStatus": "SUCCESS"}
        result = BinanceREST(_live_config()).convert("BTC", "ETH", 0.01)
        mock_quote.assert_called_once_with("BTC", "ETH", 0.01)
        mock_accept.assert_called_once_with("q-42")
        self.assertEqual(result["orderStatus"], "SUCCESS")


class BinanceRESTExtraOrderTests(unittest.TestCase):
    """Limit/stop/take-profit orders, cancel-all, history, and public trading-rule lookups."""

    def test_place_limit_order_blocked_outside_live_mode(self):
        with self.assertRaises(RuntimeError):
            BinanceREST(Config()).place_limit_order("BTCUSDT", "BUY", 0.01, 100)

    def test_place_stop_loss_limit_order_blocked_outside_live_mode(self):
        with self.assertRaises(RuntimeError):
            BinanceREST(Config()).place_stop_loss_limit_order("BTCUSDT", "SELL", 0.01, 90, 89)

    def test_place_take_profit_limit_order_blocked_outside_live_mode(self):
        with self.assertRaises(RuntimeError):
            BinanceREST(Config()).place_take_profit_limit_order("BTCUSDT", "SELL", 0.01, 110, 111)

    def test_cancel_all_open_orders_blocked_outside_live_mode(self):
        with self.assertRaises(RuntimeError):
            BinanceREST(Config()).cancel_all_open_orders("BTCUSDT")

    @patch("binance_trading_bot.urllib.request.urlopen")
    def test_place_limit_order_sends_post(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(b'{"status": "NEW"}')
        BinanceREST(_live_config()).place_limit_order("BTCUSDT", "BUY", 0.01, 100, time_in_force="GTC")
        request = mock_urlopen.call_args[0][0]
        self.assertEqual(request.get_method(), "POST")
        self.assertIn("type=LIMIT", request.full_url)
        self.assertIn("timeInForce=GTC", request.full_url)

    @patch("binance_trading_bot.urllib.request.urlopen")
    def test_place_stop_loss_limit_order_sends_post(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(b'{"status": "NEW"}')
        BinanceREST(_live_config()).place_stop_loss_limit_order("BTCUSDT", "SELL", 0.01, stop_price=90, limit_price=89)
        request = mock_urlopen.call_args[0][0]
        self.assertEqual(request.get_method(), "POST")
        self.assertIn("type=STOP_LOSS_LIMIT", request.full_url)
        self.assertIn("stopPrice=90", request.full_url)

    @patch("binance_trading_bot.urllib.request.urlopen")
    def test_place_take_profit_limit_order_sends_post(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(b'{"status": "NEW"}')
        BinanceREST(_live_config()).place_take_profit_limit_order("BTCUSDT", "SELL", 0.01, stop_price=110, limit_price=111)
        request = mock_urlopen.call_args[0][0]
        self.assertEqual(request.get_method(), "POST")
        self.assertIn("type=TAKE_PROFIT_LIMIT", request.full_url)

    @patch("binance_trading_bot.urllib.request.urlopen")
    def test_cancel_all_open_orders_sends_delete(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(b"[]")
        BinanceREST(_live_config()).cancel_all_open_orders("BTCUSDT")
        request = mock_urlopen.call_args[0][0]
        self.assertEqual(request.get_method(), "DELETE")
        self.assertIn("/api/v3/openOrders?", request.full_url)

    @patch("binance_trading_bot.urllib.request.urlopen")
    def test_get_all_orders_sends_get(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(b"[]")
        result = BinanceREST(_live_config()).get_all_orders("BTCUSDT")
        request = mock_urlopen.call_args[0][0]
        self.assertEqual(request.get_method(), "GET")
        self.assertIn("/api/v3/allOrders?", request.full_url)
        self.assertEqual(result, [])

    @patch("binance_trading_bot.urllib.request.urlopen")
    def test_get_my_trades_sends_get(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(b'[{"price": "100"}]')
        result = BinanceREST(_live_config()).get_my_trades("BTCUSDT")
        request = mock_urlopen.call_args[0][0]
        self.assertEqual(request.get_method(), "GET")
        self.assertIn("/api/v3/myTrades?", request.full_url)
        self.assertEqual(result[0]["price"], "100")

    @patch("binance_trading_bot.urllib.request.urlopen")
    def test_get_exchange_info_is_unsigned_get(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(b'{"symbols": []}')
        BinanceREST(Config()).get_exchange_info("BTCUSDT")
        request = mock_urlopen.call_args[0][0]
        self.assertEqual(request.get_method(), "GET")
        self.assertNotIn("signature=", request.full_url)

    @patch("binance_trading_bot.urllib.request.urlopen")
    def test_get_symbol_filters_extracts_known_fields(self, mock_urlopen):
        payload = json.dumps({"symbols": [{"filters": [
            {"filterType": "LOT_SIZE", "stepSize": "0.00010000", "minQty": "0.00010000", "maxQty": "9000.0"},
            {"filterType": "PRICE_FILTER", "tickSize": "0.01000000"},
            {"filterType": "NOTIONAL", "minNotional": "5.00000000"},
        ]}]}).encode()
        mock_urlopen.return_value = _mock_response(payload)
        filters = BinanceREST(Config()).get_symbol_filters("BTCUSDT")
        self.assertEqual(filters["step_size"], 0.0001)
        self.assertEqual(filters["min_qty"], 0.0001)
        self.assertEqual(filters["tick_size"], 0.01)
        self.assertEqual(filters["min_notional"], 5.0)


class RoundToStepTests(unittest.TestCase):
    def test_rounds_down_to_nearest_step(self):
        self.assertEqual(round_to_step(0.123456, 0.001), 0.123)

    def test_none_step_is_a_no_op(self):
        self.assertEqual(round_to_step(0.123456, None), 0.123456)

    def test_zero_step_is_a_no_op(self):
        self.assertEqual(round_to_step(0.123456, 0), 0.123456)

    def test_already_aligned_value_is_unchanged(self):
        self.assertEqual(round_to_step(1.5, 0.5), 1.5)


class RiskStatePersistenceTests(unittest.TestCase):
    def test_state_dict_round_trips_through_restore(self):
        gate = RiskGate(Config())
        gate.closed(50.0)
        restored = RiskGate(Config())
        restored.restore(gate.state_dict())
        self.assertEqual(restored.equity, gate.equity)
        self.assertEqual(restored.trades, gate.trades)
        self.assertEqual(restored.trade_times, gate.trade_times)

    def test_save_and_load_round_trip_through_disk(self):
        gate = RiskGate(Config())
        gate.closed(-20.0)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "nested" / "risk_state.json"
            save_risk_state(path, gate)
            loaded = load_risk_state(path)
        self.assertEqual(loaded["equity"], gate.equity)
        self.assertEqual(loaded["trades"], 1)

    def test_load_missing_file_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(load_risk_state(Path(tmp) / "missing.json"), {})

    def test_load_corrupt_file_returns_empty_dict(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "risk_state.json"
            path.write_text("not json")
            self.assertEqual(load_risk_state(path), {})


class LiveCycleTests(unittest.TestCase):
    """run_live_cycle is one pass; the network is always a MagicMock(spec=BinanceREST)."""

    def _client(self, candles, open_orders=None, filters=None):
        client = MagicMock(spec=BinanceREST)
        client.get_open_orders.return_value = open_orders or []
        client.klines.return_value = candles
        client.get_symbol_filters.return_value = filters or {
            "step_size": None, "min_qty": None, "tick_size": None, "min_notional": None,
        }
        client.market_order.return_value = {"status": "FILLED"}
        client.place_oco_order.return_value = {"orderListId": 1}
        return client

    def test_skips_symbol_with_existing_open_order(self):
        cfg = _live_config(symbols=("BTCUSDT",))
        client = self._client(_trending_candles(), open_orders=[{"orderId": 1}])
        result = run_live_cycle(cfg, client, RiskGate(cfg), RegimeStrategy(cfg))
        self.assertEqual(result["results"][0]["reason"], "open-order-exists")
        client.market_order.assert_not_called()
        client.place_oco_order.assert_not_called()

    def test_sell_signal_is_never_sent_as_open_short(self):
        cfg = _live_config(symbols=("BTCUSDT",))
        client = self._client(_trending_candles(rising=False))
        result = run_live_cycle(cfg, client, RiskGate(cfg), RegimeStrategy(cfg))
        self.assertEqual(result["results"][0]["reason"], "spot-no-short")
        client.market_order.assert_not_called()

    def test_approved_buy_places_real_order_and_oco_bracket(self):
        cfg = _live_config(symbols=("BTCUSDT",))
        client = self._client(_trending_candles(rising=True))
        result = run_live_cycle(cfg, client, RiskGate(cfg), RegimeStrategy(cfg))
        self.assertEqual(result["results"][0]["action"], "opened")
        client.market_order.assert_called_once()
        self.assertEqual(client.market_order.call_args[0][1], "BUY")
        client.place_oco_order.assert_called_once()
        self.assertEqual(client.place_oco_order.call_args[0][1], "SELL")

    def test_order_quantity_is_rounded_to_lot_size_before_sending(self):
        cfg = _live_config(symbols=("BTCUSDT",), risk_per_trade_pct=1.7)
        filters = {"step_size": 0.001, "min_qty": 0.001, "tick_size": 0.01, "min_notional": None}
        client = self._client(_trending_candles(rising=True), filters=filters)
        run_live_cycle(cfg, client, RiskGate(cfg), RegimeStrategy(cfg))
        sent_qty = client.market_order.call_args[0][2]
        # Tolerant of float noise: assert sent_qty lands on the step grid, not exact `%` == 0.
        self.assertAlmostEqual(sent_qty / 0.001, round(sent_qty / 0.001), places=6)

    def test_below_min_notional_is_rejected_before_sending(self):
        cfg = _live_config(symbols=("BTCUSDT",))
        filters = {"step_size": None, "min_qty": None, "tick_size": None, "min_notional": 10_000_000}
        client = self._client(_trending_candles(rising=True), filters=filters)
        result = run_live_cycle(cfg, client, RiskGate(cfg), RegimeStrategy(cfg))
        self.assertEqual(result["results"][0]["reason"], "below-min-notional")
        client.market_order.assert_not_called()

    def test_no_data_is_skipped_not_crashed(self):
        cfg = _live_config(symbols=("BTCUSDT",))
        client = self._client([])
        result = run_live_cycle(cfg, client, RiskGate(cfg), RegimeStrategy(cfg))
        self.assertEqual(result["results"][0]["reason"], "no-data")

    def test_failed_bracket_flattens_entry(self):
        cfg = _live_config(symbols=("BTCUSDT",))
        client = self._client(_trending_candles(rising=True))
        client.place_oco_order.side_effect = RuntimeError("rejected")
        result = run_live_cycle(cfg, client, RiskGate(cfg), RegimeStrategy(cfg), {})
        self.assertEqual(result["results"][0]["reason"], "bracket-failed-flattened")
        self.assertEqual(client.market_order.call_count, 2)
        self.assertEqual(client.market_order.call_args_list[-1].args[1], "SELL")
        client.cancel_all_open_orders.assert_called_once_with("BTCUSDT")

    def test_successful_entry_is_recorded_for_hourly_limit(self):
        cfg = _live_config(symbols=("BTCUSDT",))
        client = self._client(_trending_candles(rising=True))
        risk = RiskGate(cfg)
        run_live_cycle(cfg, client, risk, RegimeStrategy(cfg), {})
        self.assertEqual(len(risk.trade_times), 1)


class LiveLoopTests(unittest.TestCase):
    def test_run_live_blocked_outside_live_mode(self):
        with self.assertRaises(RuntimeError):
            run_live(Config(), MagicMock(spec=BinanceREST))

    def test_run_live_stops_on_first_cycle_when_signalled(self):
        """Simulates SIGINT arriving during the very first cycle: the loop must run
        run_live_cycle exactly once, persist state, and exit without sleeping."""
        client = MagicMock(spec=BinanceREST)
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _live_config(symbols=("BTCUSDT",), live_poll_seconds=5, risk_state_path=Path(tmp) / "risk_state.json")
            with patch("binance_trading_bot.run_live_cycle") as mock_cycle:
                def _act_then_stop(*_args, **_kwargs):
                    os.kill(os.getpid(), signal.SIGINT)
                    return {"results": []}
                mock_cycle.side_effect = _act_then_stop
                run_live(cfg, client)
            mock_cycle.assert_called_once()
            self.assertTrue((Path(tmp) / "risk_state.json").exists())


if __name__ == "__main__": unittest.main()
