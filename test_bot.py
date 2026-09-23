import json
import os
import signal
import tempfile
import time
import unittest
import urllib.error
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch

os.environ.setdefault("TRADING_MODE", "paper")
from binance_trading_bot import (
    BinanceError, BinanceREST, Candle, Config, ConfigError, LivePosition, MarketDataError, Mode,
    PositionLedger, RegimeStrategy, RiskGate, Signal, _atomic_write_text, _net_filled_quantity,
    _stop_limit_price, append_event, assert_key_is_trade_only, atr, backtest, exchange_now_ms, live_equity,
    load_risk_state, realized_pnl, reconcile_positions, round_to_step, run_live, run_live_cycle,
    save_risk_state, validate_candles,
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

    def test_hourly_limit_counts_entries_not_exits(self):
        """The limit has to count a position when it opens. Counting on close means a run
        that never closes anything never counts, and the limit never fires (finding C2)."""
        cfg = Config(max_trades_per_hour=1)
        gate = RiskGate(cfg)
        gate.opened()
        ok, _, reason = gate.approve(Signal.BUY, 100, 2, 0)
        self.assertFalse(ok); self.assertEqual(reason, "hourly-trade-limit")

    def test_closing_a_trade_does_not_consume_the_hourly_budget(self):
        gate = RiskGate(Config(max_trades_per_hour=1))
        gate.closed(-1.0)
        self.assertEqual(gate.trade_times, [])
        ok, _, _ = gate.approve(Signal.BUY, 100, 2, 0)
        self.assertTrue(ok)


def _mock_response(payload: bytes, headers=None):
    resp = MagicMock()
    resp.read.return_value = payload
    resp.headers = headers if headers is not None else {}
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def _http_error(status: int, body: dict, headers=None):
    return urllib.error.HTTPError("https://api.binance.com/api/v3/order", status, "Error",
                                  headers or {}, BytesIO(json.dumps(body).encode()))


def _live_config(**overrides) -> Config:
    base = dict(mode=Mode.LIVE, api_key="key", api_secret="secret", live_confirmation="I_UNDERSTAND_RISK")
    base.update(overrides)
    return Config(**base)


HOUR_MS = 3600 * 1000


def _hourly_timestamps(n: int) -> list:
    """Open times of n hourly bars, the last one being the bar forming right now - the
    shape Binance returns, so the live cycle's staleness check sees current data."""
    current = int(time.time() * 1000) // HOUR_MS * HOUR_MS
    return [current - (n - 1 - i) * HOUR_MS for i in range(n)]


def _trending_candles(n: int = 70, rising: bool = True) -> list:
    candles = []
    price = 100.0
    step = 0.5 if rising else -0.5
    for stamp in _hourly_timestamps(n):
        price += step
        candles.append(Candle(stamp, price - 0.2, price + 1.0, price - 1.0, price))
    return candles


def _fill(qty, price, commission="0", asset="USDT"):
    return {"commission": commission, "commissionAsset": asset, "qty": str(qty), "price": str(price)}


def _filled_order(qty, price, fills=None):
    return {"status": "FILLED", "executedQty": str(qty), "cummulativeQuoteQty": str(qty * price),
            "fills": fills if fills is not None else []}


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
    def test_market_order_carries_a_client_order_id(self, mock_urlopen):
        """H4: a timed-out order is only answerable if we chose its id up front."""
        mock_urlopen.return_value = _mock_response(b'{"status": "FILLED"}')
        BinanceREST(_live_config()).market_order("BTCUSDT", "BUY", 0.01, client_order_id="orca-BTCUSDT-1")
        self.assertIn("newClientOrderId=orca-BTCUSDT-1", mock_urlopen.call_args[0][0].full_url)

    @patch("binance_trading_bot.urllib.request.urlopen")
    def test_get_order_by_client_id_queries_orig_client_order_id(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(b'{"status": "FILLED"}')
        BinanceREST(_live_config()).get_order_by_client_id("BTCUSDT", "orca-1")
        self.assertIn("origClientOrderId=orca-1", mock_urlopen.call_args[0][0].full_url)


class RequestHardeningTests(unittest.TestCase):
    """H1, H2, M3, L4: what the transport does with errors, limits, clocks and the key."""

    @patch("binance_trading_bot.urllib.request.urlopen")
    def test_binance_error_body_is_preserved(self, mock_urlopen):
        """H1: the code and message live in the response body, which urlopen discards."""
        mock_urlopen.side_effect = _http_error(400, {"code": -2010, "msg": "Account has insufficient balance"})
        with self.assertRaises(BinanceError) as caught:
            BinanceREST(_live_config()).market_order("BTCUSDT", "BUY", 0.01)
        self.assertEqual(caught.exception.code, -2010)
        self.assertEqual(caught.exception.msg, "Account has insufficient balance")
        self.assertIn("insufficient balance", str(caught.exception))

    @patch("binance_trading_bot.time.sleep")
    @patch("binance_trading_bot.urllib.request.urlopen")
    def test_non_json_error_body_still_produces_a_binance_error(self, mock_urlopen, _mock_sleep):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            "https://api.binance.com/api/v3/order", 502, "Bad Gateway", {}, BytesIO(b"<html>nginx</html>"))
        with self.assertRaises(BinanceError) as caught:
            BinanceREST(_live_config()).get_open_orders("BTCUSDT")
        self.assertEqual(caught.exception.status, 502)
        self.assertIsNone(caught.exception.code)

    def test_fatal_errors_are_flagged_so_the_loop_can_stop(self):
        self.assertTrue(BinanceError(401, None, "", "/p").is_fatal)
        self.assertTrue(BinanceError(418, None, "", "/p").is_fatal)
        self.assertTrue(BinanceError(400, -2014, "bad key", "/p").is_fatal)
        self.assertFalse(BinanceError(400, -2010, "no balance", "/p").is_fatal)

    def test_an_ip_ban_is_never_retried(self):
        """418 is the ban that follows repeated 429s; retrying it lengthens the ban."""
        self.assertFalse(BinanceError(418, None, "banned", "/p").is_retryable)
        self.assertTrue(BinanceError(429, None, "too many", "/p").is_retryable)

    @patch("binance_trading_bot.time.sleep")
    @patch("binance_trading_bot.urllib.request.urlopen")
    def test_rate_limited_read_is_retried_after_the_retry_after_delay(self, mock_urlopen, mock_sleep):
        mock_urlopen.side_effect = [_http_error(429, {"code": -1003, "msg": "too many requests"},
                                               headers={"Retry-After": "3"}),
                                    _mock_response(b"[]")]
        result = BinanceREST(_live_config()).get_open_orders("BTCUSDT")
        self.assertEqual(result, [])
        mock_sleep.assert_called_once_with(3.0)

    @patch("binance_trading_bot.time.sleep")
    @patch("binance_trading_bot.urllib.request.urlopen")
    def test_a_rate_limited_order_post_is_never_retried(self, mock_urlopen, mock_sleep):
        """H2: without knowing whether the first attempt landed, a retried order is a
        second position."""
        mock_urlopen.side_effect = _http_error(429, {"code": -1003, "msg": "too many requests"})
        with self.assertRaises(BinanceError):
            BinanceREST(_live_config()).market_order("BTCUSDT", "BUY", 0.01)
        self.assertEqual(mock_urlopen.call_count, 1)
        mock_sleep.assert_not_called()

    @patch("binance_trading_bot.urllib.request.urlopen")
    def test_used_weight_header_is_tracked(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(b"[]", headers={"X-MBX-USED-WEIGHT-1M": "1100"})
        client = BinanceREST(_live_config(max_weight_per_minute=1200))
        client.get_open_orders("BTCUSDT")
        self.assertEqual(client.used_weight, 1100)
        self.assertTrue(client.weight_is_critical())

    @patch("binance_trading_bot.urllib.request.urlopen")
    def test_signed_requests_carry_a_recv_window(self, mock_urlopen):
        """M3: without recvWindow Binance applies its 5s default to a possibly drifting clock."""
        mock_urlopen.return_value = _mock_response(b"[]")
        BinanceREST(_live_config(recv_window_ms=9000)).get_open_orders("BTCUSDT")
        self.assertIn("recvWindow=9000", mock_urlopen.call_args[0][0].full_url)

    @patch("binance_trading_bot.urllib.request.urlopen")
    def test_sync_time_records_the_offset_and_shifts_timestamps(self, mock_urlopen):
        client = BinanceREST(_live_config())
        mock_urlopen.return_value = _mock_response(
            json.dumps({"serverTime": int(__import__("time").time() * 1000) + 30_000}).encode())
        offset = client.sync_time()
        self.assertGreater(offset, 25_000)
        mock_urlopen.return_value = _mock_response(b"[]")
        client.get_open_orders("BTCUSDT")
        sent = mock_urlopen.call_args[0][0].full_url
        stamp = int(sent.split("timestamp=")[1].split("&")[0])
        self.assertGreater(stamp, int(__import__("time").time() * 1000) + 25_000)

    @patch("binance_trading_bot.urllib.request.urlopen")
    def test_api_key_is_not_sent_on_public_endpoints(self, mock_urlopen):
        """L4: the key identifies the account, so it travels only where an account is needed."""
        mock_urlopen.return_value = _mock_response(b'{"symbols": []}')
        BinanceREST(_live_config()).get_exchange_info("BTCUSDT")
        self.assertNotIn("X-MBX-APIKEY", mock_urlopen.call_args[0][0].headers)
        mock_urlopen.return_value = _mock_response(b"[]")
        BinanceREST(_live_config()).get_open_orders("BTCUSDT")
        headers = {k.lower(): v for k, v in mock_urlopen.call_args[0][0].headers.items()}
        self.assertIn("x-mbx-apikey", headers)

    @patch("binance_trading_bot.urllib.request.urlopen")
    def test_symbol_filters_are_cached_between_calls(self, mock_urlopen):
        """H2: exchangeInfo is heavy and these values change weekly at most."""
        payload = json.dumps({"symbols": [{"filters": [
            {"filterType": "LOT_SIZE", "stepSize": "0.001", "minQty": "0.001"},
            {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
        ]}]}).encode()
        mock_urlopen.return_value = _mock_response(payload)
        client = BinanceREST(_live_config())
        for _ in range(5):
            client.get_symbol_filters("BTCUSDT")
        self.assertEqual(mock_urlopen.call_count, 1)


class CredentialTests(unittest.TestCase):
    """H3 and M5: what the process can leak, and what the key is allowed to do."""

    def test_api_secret_is_not_in_the_config_repr(self):
        cfg = _live_config(api_key="AKIAEXAMPLEKEY", api_secret="SUPER_SECRET_VALUE")
        self.assertNotIn("SUPER_SECRET_VALUE", repr(cfg))
        self.assertNotIn("AKIAEXAMPLEKEY", repr(cfg))

    def test_masked_key_shows_enough_to_identify_and_no_more(self):
        cfg = _live_config(api_key="ABCD12345678WXYZ")
        self.assertEqual(cfg.masked_key, "ABCD...WXYZ")
        self.assertEqual(_live_config(api_key="short").masked_key, "(unset)")

    def test_a_key_that_can_withdraw_is_refused(self):
        client = MagicMock(spec=BinanceREST)
        client.get_api_key_permissions.return_value = {"enableWithdrawals": True, "ipRestrict": True}
        with self.assertRaises(RuntimeError) as caught:
            assert_key_is_trade_only(client, _live_config())
        self.assertIn("withdraw", str(caught.exception))

    def test_a_key_with_no_ip_allowlist_is_refused_by_default(self):
        client = MagicMock(spec=BinanceREST)
        client.get_api_key_permissions.return_value = {"enableWithdrawals": False, "ipRestrict": False}
        with self.assertRaises(RuntimeError) as caught:
            assert_key_is_trade_only(client, _live_config())
        self.assertIn("IP allowlist", str(caught.exception))

    def test_the_ip_allowlist_requirement_can_be_waived_knowingly(self):
        client = MagicMock(spec=BinanceREST)
        client.get_api_key_permissions.return_value = {"enableWithdrawals": False, "ipRestrict": False}
        assert_key_is_trade_only(client, _live_config(require_key_ip_restriction=False))

    def test_an_unverifiable_key_is_refused(self):
        client = MagicMock(spec=BinanceREST)
        client.get_api_key_permissions.side_effect = BinanceError(403, -2015, "no permission", "/p")
        with self.assertRaises(RuntimeError):
            assert_key_is_trade_only(client, _live_config())

    def test_a_trade_only_key_passes(self):
        client = MagicMock(spec=BinanceREST)
        client.get_api_key_permissions.return_value = {"enableWithdrawals": False, "ipRestrict": True}
        assert_key_is_trade_only(client, _live_config())


class ConfigValidationTests(unittest.TestCase):
    """L1 and L2: limits that are not limits, and settings that crash before they report."""

    def test_loss_limits_must_be_a_real_percentage(self):
        for bad in (-5, 0, 101, 10_000):
            with self.assertRaises(ValueError):
                _live_config(max_daily_loss_pct=bad).validate()
            with self.assertRaises(ValueError):
                _live_config(max_drawdown_pct=bad).validate()

    def test_notional_cap_and_stop_buffer_are_bounded(self):
        with self.assertRaises(ValueError):
            _live_config(max_notional_pct=0).validate()
        with self.assertRaises(ValueError):
            _live_config(max_notional_pct=101).validate()
        with self.assertRaises(ValueError):
            _live_config(stop_limit_buffer_pct=100).validate()

    def test_kline_interval_must_be_one_binance_accepts(self):
        with self.assertRaises(ValueError):
            _live_config(kline_interval="7h").validate()

    def test_default_config_still_validates(self):
        _live_config().validate()

    def test_a_bad_environment_value_names_the_variable(self):
        with patch.dict(os.environ, {"TRADING_MODE": "lve"}):
            with self.assertRaises(ConfigError) as caught:
                Config()
        self.assertIn("TRADING_MODE", str(caught.exception))

    def test_a_non_numeric_setting_names_the_variable(self):
        with patch.dict(os.environ, {"RISK_PER_TRADE_PCT": "abc"}):
            with self.assertRaises(ConfigError) as caught:
                Config()
        self.assertIn("RISK_PER_TRADE_PCT", str(caught.exception))

    def test_settings_are_read_when_the_config_is_built_not_at_import(self):
        with patch.dict(os.environ, {"MAX_OPEN_POSITIONS": "7"}):
            self.assertEqual(Config().max_open_positions, 7)
        self.assertEqual(Config().max_open_positions, 3)


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

    @patch.object(BinanceREST, "accept_convert_quote")
    @patch.object(BinanceREST, "get_convert_quote")
    def test_convert_refuses_a_quote_below_the_floor(self, mock_quote, mock_accept):
        """L3: without a floor this accepts whatever rate comes back."""
        mock_quote.return_value = {"quoteId": "q-42", "toAmount": "0.5"}
        with self.assertRaises(ValueError):
            BinanceREST(_live_config()).convert("BTC", "ETH", 0.01, min_to_amount=1.0)
        mock_accept.assert_not_called()

    @patch.object(BinanceREST, "accept_convert_quote")
    @patch.object(BinanceREST, "get_convert_quote")
    def test_convert_accepts_a_quote_at_or_above_the_floor(self, mock_quote, mock_accept):
        mock_quote.return_value = {"quoteId": "q-42", "toAmount": "1.5"}
        mock_accept.return_value = {"orderStatus": "SUCCESS"}
        BinanceREST(_live_config()).convert("BTC", "ETH", 0.01, min_to_amount=1.0)
        mock_accept.assert_called_once_with("q-42")


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
    def test_ticker_price_is_an_unsigned_get(self, mock_urlopen):
        mock_urlopen.return_value = _mock_response(b'{"symbol": "BTCUSDT", "price": "64000.12"}')
        self.assertEqual(BinanceREST(Config()).ticker_price("BTCUSDT"), 64000.12)
        self.assertNotIn("signature=", mock_urlopen.call_args[0][0].full_url)

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


class BracketPricingTests(unittest.TestCase):
    """M1: a stop-limit priced at its own trigger often does not fill."""

    def test_stop_limit_sits_below_the_trigger(self):
        cfg = _live_config(stop_limit_buffer_pct=0.2)
        limit = _stop_limit_price(100.0, cfg, 0.01)
        self.assertLess(limit, 100.0)
        self.assertAlmostEqual(limit, 99.80, places=2)

    def test_a_zero_buffer_is_allowed_but_explicit(self):
        self.assertEqual(_stop_limit_price(100.0, _live_config(stop_limit_buffer_pct=0), 0.01), 100.0)


class FillAccountingTests(unittest.TestCase):
    def test_base_asset_commission_is_taken_off_the_sellable_quantity(self):
        """A fee charged in the coin just bought reduces what can be bracketed."""
        cfg = _live_config()
        order = _filled_order(1.0, 100, fills=[_fill(1.0, 100, commission="0.001", asset="BTC")])
        self.assertAlmostEqual(_net_filled_quantity(order, "BTCUSDT", cfg), 0.999)

    def test_quote_asset_commission_does_not_reduce_the_quantity(self):
        cfg = _live_config()
        order = _filled_order(1.0, 100, fills=[_fill(1.0, 100, commission="0.1", asset="USDT")])
        self.assertAlmostEqual(_net_filled_quantity(order, "BTCUSDT", cfg), 1.0)

    def test_realized_pnl_uses_exit_fills_after_the_entry(self):
        cfg = _live_config()
        position = LivePosition(symbol="BTCUSDT", entry_price=100.0, stop=95, take=110,
                                opened_at="now", entry_time_ms=1_000, entry_client_order_id="orca-1",
                                quantity=2.0, protected=True)
        client = MagicMock(spec=BinanceREST)
        client.get_my_trades.return_value = [
            {"isBuyer": True, "time": 900, "qty": "2.0", "quoteQty": "200.0"},        # the entry
            {"isBuyer": False, "time": 2_000, "qty": "2.0", "quoteQty": "220.0",
             "commission": "0.22", "commissionAsset": "USDT"},                        # the exit
        ]
        pnl, exited, last_exit = realized_pnl(client, cfg, position)
        self.assertAlmostEqual(exited, 2.0)
        self.assertEqual(last_exit, 2_000)
        self.assertAlmostEqual(pnl, 220.0 - 200.0 - 0.22)

    def test_no_exit_fills_reports_nothing_closed(self):
        client = MagicMock(spec=BinanceREST)
        client.get_my_trades.return_value = [{"isBuyer": True, "time": 2_000, "qty": "1", "quoteQty": "100"}]
        position = LivePosition(symbol="BTCUSDT", entry_price=100.0, stop=95, take=110, opened_at="now",
                                entry_time_ms=1_000, entry_client_order_id="orca-1", quantity=1.0)
        self.assertEqual(realized_pnl(client, _live_config(), position), (0.0, 0.0, 1_000))


class DurableStateTests(unittest.TestCase):
    """M4: state that resets itself is worse than state that refuses to load."""

    def test_state_dict_round_trips_through_restore(self):
        gate = RiskGate(Config())
        gate.opened()
        gate.closed(50.0)
        restored = RiskGate(Config())
        restored.restore(gate.state_dict())
        self.assertEqual(restored.equity, gate.equity)
        self.assertEqual(restored.trades, gate.trades)
        self.assertEqual(restored.trade_times, gate.trade_times)
        self.assertEqual(restored.day_start_equity, gate.day_start_equity)

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

    def test_a_corrupt_risk_file_stops_a_live_run_rather_than_resetting_the_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "risk_state.json"
            path.write_text("{truncated")
            with self.assertRaises(RuntimeError) as caught:
                load_risk_state(path, strict=True)
        self.assertIn("refuses to start", str(caught.exception))

    def test_a_failed_write_leaves_the_previous_file_intact(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "risk_state.json"
            path.write_text('{"equity": 1234}')
            with patch("binance_trading_bot.os.replace", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    _atomic_write_text(path, "replacement")
            self.assertEqual(json.loads(path.read_text())["equity"], 1234)
            self.assertEqual(list(p.name for p in Path(tmp).iterdir()), ["risk_state.json"])

    def test_the_position_ledger_survives_a_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "positions.json"
            ledger = PositionLedger(path)
            ledger.record(LivePosition(symbol="BTCUSDT", entry_price=100.0, stop=95, take=110,
                                       opened_at="now", entry_time_ms=1, entry_client_order_id="orca-1",
                                       quantity=2.0, protected=True))
            reloaded = PositionLedger(path).load()
        self.assertTrue(reloaded.holds("BTCUSDT"))
        self.assertEqual(reloaded.get("BTCUSDT").quantity, 2.0)
        self.assertTrue(reloaded.get("BTCUSDT").protected)

    def test_a_corrupt_ledger_refuses_to_load(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "positions.json"
            path.write_text("{oops")
            with self.assertRaises(RuntimeError):
                PositionLedger(path).load()


class EquityTests(unittest.TestCase):
    """C3: sizing has to come from the account, not from PAPER_START_BALANCE."""

    def test_live_equity_reads_the_quote_balance(self):
        client = MagicMock(spec=BinanceREST)
        client.account.return_value = {"balances": [{"asset": "BTC", "free": "1", "locked": "0"},
                                                    {"asset": "USDT", "free": "480.5", "locked": "19.5"}]}
        self.assertAlmostEqual(live_equity(client, _live_config(), PositionLedger()), 500.0)

    def test_live_equity_counts_what_open_positions_cost(self):
        """Money in a position is not a drawdown."""
        client = MagicMock(spec=BinanceREST)
        client.account.return_value = {"balances": [{"asset": "USDT", "free": "400", "locked": "0"}]}
        ledger = PositionLedger()
        ledger.record(LivePosition(symbol="BTCUSDT", entry_price=50.0, stop=45, take=60, opened_at="now",
                                   entry_time_ms=1, entry_client_order_id="orca-1", quantity=2.0))
        self.assertAlmostEqual(live_equity(client, _live_config(), ledger), 500.0)

    def test_the_daily_loss_limit_measures_against_the_real_balance(self):
        gate = RiskGate(_live_config(initial_equity=10_000, max_daily_loss_pct=2))
        gate.set_equity(500.0)                      # the account actually holds 500
        gate.closed(-11.0)                          # 2.2% of 500, over the limit
        ok, _, reason = gate.approve(Signal.BUY, 100, 2, 0)
        self.assertFalse(ok)
        self.assertEqual(reason, "daily-loss-limit")

    def test_a_loss_inside_the_limit_still_trades(self):
        gate = RiskGate(_live_config(initial_equity=10_000, max_daily_loss_pct=2))
        gate.set_equity(500.0)
        gate.closed(-5.0)                           # 1% of 500
        ok, _, _ = gate.approve(Signal.BUY, 100, 2, 0)
        self.assertTrue(ok)


class LiveCycleTests(unittest.TestCase):
    """run_live_cycle is one pass; the network is always a MagicMock(spec=BinanceREST)."""

    def _client(self, candles, open_orders=None, filters=None, balance="10000", order=None):
        client = MagicMock(spec=BinanceREST)
        client.get_open_orders.return_value = open_orders if open_orders is not None else []
        client.klines.return_value = candles
        client.weight_is_critical.return_value = False
        client.account.return_value = {"balances": [{"asset": "USDT", "free": balance, "locked": "0"}]}
        client.ticker_price.return_value = candles[-2].close if len(candles) > 1 else 100.0
        client.get_symbol_filters.return_value = filters or {
            "step_size": None, "min_qty": None, "tick_size": None, "min_notional": None,
        }
        price = client.ticker_price.return_value
        client.market_order.return_value = order if order is not None else _filled_order(1.0, price)
        client.place_oco_order.return_value = {"orderListId": 1}
        client.get_my_trades.return_value = []
        return client

    def _run(self, cfg, client, ledger=None, risk=None):
        ledger = ledger if ledger is not None else PositionLedger()
        return run_live_cycle(cfg, client, risk or RiskGate(cfg), RegimeStrategy(cfg), ledger), ledger

    def test_sell_signal_is_never_sent_as_open_short(self):
        cfg = _live_config(symbols=("BTCUSDT",))
        client = self._client(_trending_candles(rising=False))
        result, _ = self._run(cfg, client)
        self.assertEqual(result["results"][0]["reason"], "spot-no-short")
        client.market_order.assert_not_called()

    def test_approved_buy_places_real_order_and_oco_bracket(self):
        cfg = _live_config(symbols=("BTCUSDT",))
        client = self._client(_trending_candles(rising=True))
        result, ledger = self._run(cfg, client)
        self.assertEqual(result["results"][0]["action"], "opened")
        client.market_order.assert_called_once()
        self.assertEqual(client.market_order.call_args[0][1], "BUY")
        client.place_oco_order.assert_called_once()
        self.assertEqual(client.place_oco_order.call_args[0][1], "SELL")
        self.assertTrue(ledger.holds("BTCUSDT"))
        self.assertTrue(ledger.get("BTCUSDT").protected)

    def test_the_bracket_limit_leg_is_below_its_trigger(self):
        cfg = _live_config(symbols=("BTCUSDT",), stop_limit_buffer_pct=0.5)
        client = self._client(_trending_candles(rising=True))
        self._run(cfg, client)
        sent = client.place_oco_order.call_args[1]
        self.assertLess(sent["stop_limit_price"], sent["stop_price"])

    def test_a_held_position_is_never_bought_again(self):
        """C1: the ledger is what says the bot is in a trade. A filled market buy leaves
        no open order, so an open-order check would buy again every cycle."""
        cfg = _live_config(symbols=("BTCUSDT",))
        client = self._client(_trending_candles(rising=True))
        _, ledger = self._run(cfg, client)
        self.assertEqual(client.market_order.call_count, 1)
        client.get_open_orders.return_value = [{"orderId": 1}]   # bracket resting
        result, _ = self._run(cfg, client, ledger=ledger)
        self.assertEqual(result["results"][0]["reason"], "position-open")
        self.assertEqual(client.market_order.call_count, 1)

    def test_a_failed_bracket_flattens_the_position_instead_of_leaving_it_stopless(self):
        """C1: the whole finding. The entry filled, the bracket did not."""
        cfg = _live_config(symbols=("BTCUSDT",))
        client = self._client(_trending_candles(rising=True))
        client.place_oco_order.side_effect = BinanceError(400, -2010, "insufficient balance", "/oco")
        result, ledger = self._run(cfg, client)
        self.assertEqual(result["results"][0]["action"], "flattened")
        sides = [call[0][1] for call in client.market_order.call_args_list]
        self.assertEqual(sides, ["BUY", "SELL"])
        client.cancel_all_open_orders.assert_called_once_with("BTCUSDT")
        self.assertEqual(len(ledger), 0)

    def test_a_position_that_cannot_be_flattened_stays_on_the_books_and_is_not_doubled(self):
        cfg = _live_config(symbols=("BTCUSDT",))
        client = self._client(_trending_candles(rising=True))
        client.place_oco_order.side_effect = BinanceError(400, -2010, "insufficient balance", "/oco")
        client.market_order.side_effect = [_filled_order(1.0, 134.0),
                                           BinanceError(400, -1013, "cannot sell", "/order")]
        result, ledger = self._run(cfg, client)
        self.assertEqual(result["results"][0]["action"], "alert")
        self.assertTrue(ledger.holds("BTCUSDT"))
        self.assertFalse(ledger.get("BTCUSDT").protected)

        # Next cycle: it must try to protect what it holds, not open a second position.
        client.market_order.side_effect = None
        client.market_order.reset_mock()
        client.place_oco_order.side_effect = None
        client.place_oco_order.return_value = {"orderListId": 9}
        result, ledger = self._run(cfg, client, ledger=ledger)
        self.assertEqual(result["results"][0]["action"], "opened")
        client.market_order.assert_not_called()
        self.assertTrue(ledger.get("BTCUSDT").protected)

    def test_an_entry_whose_response_was_lost_is_resolved_by_client_order_id(self):
        """H4: a timeout is ambiguous; the client order id makes it answerable."""
        cfg = _live_config(symbols=("BTCUSDT",))
        client = self._client(_trending_candles(rising=True))
        client.market_order.side_effect = TimeoutError("read timed out")
        result, ledger = self._run(cfg, client)
        self.assertEqual(result["results"][0]["reason"], "entry-order-failed")
        self.assertTrue(ledger.holds("BTCUSDT"))
        self.assertEqual(ledger.get("BTCUSDT").quantity, 0.0)

        # The order had in fact reached Binance and filled.
        coid = ledger.get("BTCUSDT").entry_client_order_id
        client.market_order.side_effect = None
        client.market_order.reset_mock()
        client.get_order_by_client_id.return_value = _filled_order(1.0, 134.0)
        result, ledger = self._run(cfg, client, ledger=ledger)
        client.get_order_by_client_id.assert_called_once_with("BTCUSDT", coid)
        actions = [r["action"] for r in result["results"]]
        self.assertIn("recovered", actions)
        self.assertIn("opened", actions)
        client.market_order.assert_not_called()
        self.assertTrue(ledger.get("BTCUSDT").protected)

    def test_an_entry_that_never_reached_binance_clears_the_reservation(self):
        cfg = _live_config(symbols=("BTCUSDT",))
        client = self._client(_trending_candles(rising=True))
        client.market_order.side_effect = TimeoutError("read timed out")
        _, ledger = self._run(cfg, client)
        client.get_order_by_client_id.side_effect = BinanceError(400, -2013, "Order does not exist", "/order")
        client.market_order.side_effect = None
        result, ledger = self._run(cfg, client, ledger=ledger)
        self.assertEqual(result["results"][0]["reason"], "entry-never-placed")

    def test_a_resolved_bracket_books_its_pnl_against_the_risk_gate(self):
        """C2: until this happens daily_pnl never moves and no loss limit can fire."""
        cfg = _live_config(symbols=("BTCUSDT",))
        client = self._client(_trending_candles(rising=True))
        risk = RiskGate(cfg)
        _, ledger = self._run(cfg, client, risk=risk)
        entry = ledger.get("BTCUSDT")
        self.assertEqual(risk.daily_pnl, 0.0)

        client.get_open_orders.return_value = []            # the bracket resolved
        client.get_my_trades.return_value = [
            {"isBuyer": False, "time": entry.entry_time_ms + 1, "qty": str(entry.quantity),
             "quoteQty": str(entry.quantity * (entry.entry_price - 10)),
             "commission": "0", "commissionAsset": "USDT"},
        ]
        # Hold off new entries so this asserts on the close alone.
        client.weight_is_critical.return_value = True
        result, ledger = self._run(cfg, client, ledger=ledger, risk=risk)
        self.assertEqual(result["results"][0]["action"], "closed")
        self.assertAlmostEqual(risk.daily_pnl, -10 * entry.quantity)
        self.assertEqual(risk.trades, 1)
        self.assertEqual(len(ledger), 0)

    def test_a_position_with_no_bracket_and_no_exit_fills_is_flattened(self):
        cfg = _live_config(symbols=("BTCUSDT",))
        client = self._client(_trending_candles(rising=True))
        ledger = PositionLedger()
        ledger.record(LivePosition(symbol="BTCUSDT", entry_price=100.0, stop=95, take=110, opened_at="now",
                                   entry_time_ms=1, entry_client_order_id="orca-1", quantity=1.0,
                                   protected=True))
        client.get_open_orders.return_value = []
        client.get_my_trades.return_value = []
        results = reconcile_positions(cfg, client, RiskGate(cfg), ledger)
        self.assertEqual(results[0]["reason"], "unprotected-on-reconcile")
        self.assertEqual(len(ledger), 0)

    def test_the_hourly_limit_stops_new_entries_in_live_mode(self):
        """C2: the limit has to bind on entries, which is all a live cycle produces."""
        cfg = _live_config(symbols=("BTCUSDT", "ETHUSDT"), max_trades_per_hour=1)
        client = self._client(_trending_candles(rising=True))
        result, _ = self._run(cfg, client)
        reasons = [r.get("reason") for r in result["results"]]
        self.assertIn("hourly-trade-limit", reasons)
        self.assertEqual(client.market_order.call_count, 1)

    def test_position_size_follows_the_real_balance(self):
        """C3: a 500 USDT account must not be sized as if it held PAPER_START_BALANCE."""
        cfg = _live_config(symbols=("BTCUSDT",), initial_equity=10_000, risk_per_trade_pct=0.25)
        small = self._client(_trending_candles(rising=True), balance="500")
        self._run(cfg, small)
        large = self._client(_trending_candles(rising=True), balance="50000")
        self._run(cfg, large)
        self.assertLess(small.market_order.call_args[0][2], large.market_order.call_args[0][2])
        self.assertAlmostEqual(small.market_order.call_args[0][2] * 100,
                               large.market_order.call_args[0][2], places=4)

    def test_notional_is_capped_against_the_balance(self):
        """C3: the risk budget over a small ATR is a large order."""
        cfg = _live_config(symbols=("BTCUSDT",), max_notional_pct=5, risk_per_trade_pct=2)
        client = self._client(_trending_candles(rising=True), balance="10000")
        self._run(cfg, client)
        qty = client.market_order.call_args[0][2]
        price = client.ticker_price.return_value
        self.assertLessEqual(qty * price, 10_000 * 0.05 + 1e-6)

    def test_no_trade_when_the_balance_cannot_be_read(self):
        cfg = _live_config(symbols=("BTCUSDT",))
        client = self._client(_trending_candles(rising=True))
        client.account.side_effect = BinanceError(500, None, "server error", "/account")
        result, _ = self._run(cfg, client)
        self.assertEqual(result["results"][-1]["reason"], "equity-unavailable")
        client.market_order.assert_not_called()

    def test_a_fatal_error_is_not_swallowed_by_the_cycle(self):
        """H1: a bad key retried silently every poll is a loop that never recovers."""
        cfg = _live_config(symbols=("BTCUSDT",))
        client = self._client(_trending_candles(rising=True))
        client.account.side_effect = BinanceError(401, -2014, "API-key format invalid", "/account")
        with self.assertRaises(BinanceError):
            self._run(cfg, client)

    def test_a_partial_exit_books_what_closed_and_keeps_the_rest(self):
        """Treating a half-filled bracket as flat would abandon the coins still held."""
        cfg = _live_config(symbols=("BTCUSDT",))
        client = self._client(_trending_candles(rising=True))
        ledger = PositionLedger()
        ledger.record(LivePosition(symbol="BTCUSDT", entry_price=100.0, stop=95, take=110,
                                   opened_at="now", entry_time_ms=1_000,
                                   entry_client_order_id="orca-1", quantity=2.0, protected=True))
        client.get_open_orders.return_value = []
        client.get_my_trades.return_value = [
            {"isBuyer": False, "time": 5_000, "qty": "0.5", "quoteQty": "55.0",
             "commission": "0", "commissionAsset": "USDT"},
        ]
        risk = RiskGate(cfg)
        results = reconcile_positions(cfg, client, risk, ledger)
        self.assertEqual(results[0]["action"], "partially-closed")
        self.assertAlmostEqual(risk.daily_pnl, 55.0 - 50.0)
        self.assertTrue(ledger.holds("BTCUSDT"))
        self.assertAlmostEqual(ledger.get("BTCUSDT").quantity, 1.5)
        self.assertFalse(ledger.get("BTCUSDT").protected)
        # The watermark moved past the fills just booked, so they are not counted twice.
        self.assertEqual(ledger.get("BTCUSDT").entry_time_ms, 5_001)
        reconcile_positions(cfg, client, risk, ledger)
        self.assertAlmostEqual(risk.daily_pnl, 55.0 - 50.0)

    def test_fills_are_read_back_when_the_order_lookup_has_none(self):
        """A lookup by client order id carries no `fills`, so the base-asset fee is invisible."""
        from binance_trading_bot import filled_quantity_for_order
        client = MagicMock(spec=BinanceREST)
        client.get_my_trades.return_value = [
            {"orderId": 77, "qty": "1.0", "commission": "0.001", "commissionAsset": "BTC"},
            {"orderId": 99, "qty": "5.0", "commission": "0", "commissionAsset": "BTC"},
        ]
        order = {"status": "FILLED", "executedQty": "1.0", "orderId": 77}
        self.assertAlmostEqual(
            filled_quantity_for_order(client, _live_config(), "BTCUSDT", order), 0.999)

    def test_the_unclosed_candle_is_not_traded_on(self):
        """M2: the last kline repaints until its interval ends."""
        cfg = _live_config(symbols=("BTCUSDT",))
        candles = _trending_candles(rising=True)[:-1]
        candles.append(Candle(999, 1.0, 1.0, 1.0, 1.0))    # an absurd in-progress bar
        client = self._client(candles)
        captured = {}
        strategy = RegimeStrategy(cfg)
        original = strategy.decide

        def _record(seen):
            captured["last"] = seen[-1]
            return original(seen)

        strategy.decide = _record
        run_live_cycle(cfg, client, RiskGate(cfg), strategy, PositionLedger())
        self.assertNotEqual(captured["last"].timestamp, 999)

    def test_execution_price_comes_from_the_ticker_not_a_stale_candle(self):
        cfg = _live_config(symbols=("BTCUSDT",))
        client = self._client(_trending_candles(rising=True))
        client.ticker_price.return_value = 200.0
        self._run(cfg, client)
        client.ticker_price.assert_called_with("BTCUSDT")
        self.assertAlmostEqual(client.place_oco_order.call_args[1]["take_profit_price"], 200.0, delta=50)

    def test_order_quantity_is_rounded_to_lot_size_before_sending(self):
        cfg = _live_config(symbols=("BTCUSDT",), risk_per_trade_pct=1.7)
        filters = {"step_size": 0.001, "min_qty": 0.001, "tick_size": 0.01, "min_notional": None}
        client = self._client(_trending_candles(rising=True), filters=filters)
        self._run(cfg, client)
        sent_qty = client.market_order.call_args[0][2]
        # Tolerant of float noise: assert sent_qty lands on the step grid, not exact `%` == 0.
        self.assertAlmostEqual(sent_qty / 0.001, round(sent_qty / 0.001), places=6)

    def test_below_min_notional_is_rejected_before_sending(self):
        cfg = _live_config(symbols=("BTCUSDT",))
        filters = {"step_size": None, "min_qty": None, "tick_size": None, "min_notional": 10_000_000}
        client = self._client(_trending_candles(rising=True), filters=filters)
        result, _ = self._run(cfg, client)
        self.assertEqual(result["results"][0]["reason"], "below-min-notional")
        client.market_order.assert_not_called()

    def test_no_data_is_skipped_not_crashed(self):
        cfg = _live_config(symbols=("BTCUSDT",))
        client = self._client([])
        result, _ = self._run(cfg, client)
        self.assertEqual(result["results"][0]["reason"], "no-data")

    def test_an_unfilled_entry_leaves_nothing_on_the_books(self):
        cfg = _live_config(symbols=("BTCUSDT",))
        client = self._client(_trending_candles(rising=True),
                              order={"status": "EXPIRED", "executedQty": "0", "fills": []})
        result, ledger = self._run(cfg, client)
        self.assertEqual(result["results"][0]["reason"], "entry-unfilled")
        self.assertEqual(len(ledger), 0)
        client.place_oco_order.assert_not_called()

    def test_trading_pauses_when_the_rate_limit_budget_is_nearly_spent(self):
        cfg = _live_config(symbols=("BTCUSDT",))
        client = self._client(_trending_candles(rising=True))
        client.weight_is_critical.return_value = True
        result, _ = self._run(cfg, client)
        self.assertEqual(result["results"][0]["reason"], "rate-limit-budget")
        client.market_order.assert_not_called()

    def test_a_stale_feed_is_not_traded_on(self):
        """A feed that stopped updating returns the same old bars forever."""
        cfg = _live_config(symbols=("BTCUSDT",))
        day_old = [Candle(c.timestamp - 24 * HOUR_MS, c.open, c.high, c.low, c.close)
                   for c in _trending_candles(rising=True)]
        client = self._client(day_old)
        result, _ = self._run(cfg, client)
        self.assertEqual(result["results"][0]["reason"], "bad-market-data")
        self.assertIn("stale", result["results"][0]["detail"])
        client.market_order.assert_not_called()

    def test_a_broken_candle_is_not_traded_on(self):
        cfg = _live_config(symbols=("BTCUSDT",))
        candles = _trending_candles(rising=True)
        bad = candles[-5]
        candles[-5] = Candle(bad.timestamp, bad.open, bad.low - 5, bad.low, bad.close)  # high below low
        client = self._client(candles)
        result, _ = self._run(cfg, client)
        self.assertEqual(result["results"][0]["reason"], "bad-market-data")
        client.market_order.assert_not_called()

    def test_one_symbols_feed_failing_does_not_stop_the_others(self):
        cfg = _live_config(symbols=("BADUSDT", "BTCUSDT"))
        client = self._client(_trending_candles(rising=True))
        good = client.klines.return_value

        def _klines(symbol, *_args, **_kwargs):
            if symbol == "BADUSDT":
                raise TimeoutError("read timed out")
            return good

        client.klines.side_effect = _klines
        result, _ = self._run(cfg, client)
        reasons = {r["symbol"]: r.get("reason", r["action"]) for r in result["results"]}
        self.assertEqual(reasons["BADUSDT"], "no-data")
        self.assertEqual(reasons["BTCUSDT"], "opened")

    def test_a_fatal_error_while_reading_candles_still_stops_the_cycle(self):
        cfg = _live_config(symbols=("BTCUSDT",))
        client = self._client(_trending_candles(rising=True))
        client.klines.side_effect = BinanceError(401, -2014, "API-key format invalid", "/klines")
        with self.assertRaises(BinanceError):
            self._run(cfg, client)

    def test_rate_limiting_stops_the_cycle_instead_of_asking_for_every_symbol(self):
        """A 429 hits every symbol alike; asking for the next one only earns a 418 ban."""
        cfg = _live_config(symbols=("BTCUSDT", "ETHUSDT"))
        client = self._client(_trending_candles(rising=True))
        client.klines.side_effect = BinanceError(429, -1003, "Too many requests", "/klines")
        with self.assertRaises(BinanceError):
            self._run(cfg, client)
        self.assertEqual(client.klines.call_count, 1)

    def test_a_fatal_error_while_reading_the_price_stops_the_cycle(self):
        cfg = _live_config(symbols=("BTCUSDT",))
        client = self._client(_trending_candles(rising=True))
        client.ticker_price.side_effect = BinanceError(401, -2015, "Invalid API-key", "/ticker/price")
        with self.assertRaises(BinanceError):
            self._run(cfg, client)
        client.market_order.assert_not_called()

    def test_the_cycle_reports_how_many_feeds_it_could_use(self):
        cfg = _live_config(symbols=("BADUSDT", "BTCUSDT"))
        client = self._client(_trending_candles(rising=True))
        good = client.klines.return_value
        client.klines.side_effect = lambda symbol, *_a, **_k: [] if symbol == "BADUSDT" else good
        result, _ = self._run(cfg, client)
        self.assertEqual((result["market_data_checks"], result["market_data_failures"]), (2, 1))

    def test_freshness_is_judged_on_the_exchange_clock(self):
        """Bars a day old on this host's clock are current if the exchange clock (the
        offset sync_time measured) says so."""
        cfg = _live_config(symbols=("BTCUSDT",))
        day_old = [Candle(c.timestamp - 24 * HOUR_MS, c.open, c.high, c.low, c.close)
                   for c in _trending_candles(rising=True)]
        client = self._client(day_old)
        client._time_offset_ms = -24 * HOUR_MS
        result, _ = self._run(cfg, client)
        self.assertEqual(result["results"][0]["action"], "opened")


class LiveLoopTests(unittest.TestCase):
    def _client(self):
        client = MagicMock(spec=BinanceREST)
        client.get_api_key_permissions.return_value = {"enableWithdrawals": False, "ipRestrict": True}
        return client

    def test_run_live_blocked_outside_live_mode(self):
        with self.assertRaises(RuntimeError):
            run_live(Config(), MagicMock(spec=BinanceREST))

    def test_run_live_refuses_a_key_that_can_withdraw(self):
        client = self._client()
        client.get_api_key_permissions.return_value = {"enableWithdrawals": True, "ipRestrict": True}
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _live_config(symbols=("BTCUSDT",), risk_state_path=Path(tmp) / "r.json",
                               positions_path=Path(tmp) / "p.json", event_log_path=Path(tmp) / "e.jsonl")
            with self.assertRaises(RuntimeError):
                run_live(cfg, client)

    def test_run_live_stops_on_first_cycle_when_signalled(self):
        """Simulates SIGINT arriving during the very first cycle: the loop must run
        run_live_cycle exactly once, persist state, and exit without sleeping."""
        client = self._client()
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _live_config(symbols=("BTCUSDT",), live_poll_seconds=5,
                               risk_state_path=Path(tmp) / "risk_state.json",
                               positions_path=Path(tmp) / "positions.json",
                               event_log_path=Path(tmp) / "events.jsonl")
            with patch("binance_trading_bot.run_live_cycle") as mock_cycle:
                def _act_then_stop(*_args, **_kwargs):
                    os.kill(os.getpid(), signal.SIGINT)
                    return {"results": []}
                mock_cycle.side_effect = _act_then_stop
                run_live(cfg, client)
            mock_cycle.assert_called_once()
            self.assertTrue((Path(tmp) / "risk_state.json").exists())

    def test_a_fatal_error_stops_the_loop_instead_of_retrying_forever(self):
        """H1: an invalid key retried every 60s is a loop that never recovers and never says so."""
        client = self._client()
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _live_config(symbols=("BTCUSDT",), live_poll_seconds=5,
                               risk_state_path=Path(tmp) / "r.json", positions_path=Path(tmp) / "p.json",
                               event_log_path=Path(tmp) / "e.jsonl")
            with patch("binance_trading_bot.run_live_cycle",
                       side_effect=BinanceError(401, -2014, "API-key format invalid", "/order")):
                with patch("binance_trading_bot.time.sleep") as mock_sleep:
                    run_live(cfg, client)
                mock_sleep.assert_not_called()

    def test_repeated_failures_stop_the_loop(self):
        client = self._client()
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _live_config(symbols=("BTCUSDT",), live_poll_seconds=5, max_consecutive_failures=3,
                               risk_state_path=Path(tmp) / "r.json", positions_path=Path(tmp) / "p.json",
                               event_log_path=Path(tmp) / "e.jsonl")
            with patch("binance_trading_bot.run_live_cycle", side_effect=RuntimeError("boom")) as mock_cycle:
                with patch("binance_trading_bot.time.sleep"):
                    run_live(cfg, client)
            self.assertEqual(mock_cycle.call_count, 3)

    def test_the_clock_is_synced_before_the_first_cycle(self):
        client = self._client()
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _live_config(symbols=("BTCUSDT",), live_poll_seconds=5,
                               risk_state_path=Path(tmp) / "r.json", positions_path=Path(tmp) / "p.json",
                               event_log_path=Path(tmp) / "e.jsonl")
            with patch("binance_trading_bot.run_live_cycle") as mock_cycle:
                mock_cycle.side_effect = lambda *a, **k: os.kill(os.getpid(), signal.SIGINT) or {"results": []}
                run_live(cfg, client)
            client.sync_time.assert_called_once()

    def test_the_event_log_records_start_cycles_and_why_the_loop_stopped(self):
        client = self._client()
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "e.jsonl"
            cfg = _live_config(symbols=("BTCUSDT",), live_poll_seconds=5, max_consecutive_failures=2,
                               risk_state_path=Path(tmp) / "r.json", positions_path=Path(tmp) / "p.json",
                               event_log_path=log)
            outcomes = [{"results": [{"symbol": "BTCUSDT", "action": "no-trade"}]}, RuntimeError("boom"),
                        RuntimeError("boom")]
            with patch("binance_trading_bot.run_live_cycle", side_effect=outcomes):
                with patch("binance_trading_bot.time.sleep"):
                    run_live(cfg, client)
            text = log.read_text()
            events = [json.loads(line) for line in text.splitlines()]
        self.assertNotIn(cfg.api_secret, text)
        self.assertEqual([e["event"] for e in events],
                         ["live-started", "cycle", "cycle-failed", "cycle-failed", "live-stopped"])
        self.assertEqual(events[1]["results"][0]["action"], "no-trade")
        self.assertEqual(events[-1]["reason"], "too-many-failures")
        self.assertEqual(events[0]["api_key"], cfg.masked_key)


    def test_a_feed_outage_counts_toward_the_failure_limit(self):
        """A cycle where no symbol had usable data did nothing; the loop must not run
        blind forever just because no exception was raised."""
        client = self._client()
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "e.jsonl"
            cfg = _live_config(symbols=("BTCUSDT",), live_poll_seconds=5, max_consecutive_failures=3,
                               risk_state_path=Path(tmp) / "r.json", positions_path=Path(tmp) / "p.json",
                               event_log_path=log)
            blind = {"results": [], "market_data_checks": 1, "market_data_failures": 1}
            with patch("binance_trading_bot.run_live_cycle", return_value=blind) as mock_cycle:
                with patch("binance_trading_bot.time.sleep"):
                    run_live(cfg, client)
            events = [json.loads(line) for line in log.read_text().splitlines()]
        self.assertEqual(mock_cycle.call_count, 3)
        self.assertEqual(events[-1]["reason"], "too-many-failures")

    def test_one_usable_feed_resets_the_failure_count(self):
        client = self._client()
        with tempfile.TemporaryDirectory() as tmp:
            cfg = _live_config(symbols=("BTCUSDT",), live_poll_seconds=5, max_consecutive_failures=2,
                               risk_state_path=Path(tmp) / "r.json", positions_path=Path(tmp) / "p.json",
                               event_log_path=Path(tmp) / "e.jsonl")
            blind = {"results": [], "market_data_checks": 2, "market_data_failures": 2}
            partial = {"results": [], "market_data_checks": 2, "market_data_failures": 1}
            with patch("binance_trading_bot.run_live_cycle",
                       side_effect=[blind, partial, blind, blind]) as mock_cycle:
                with patch("binance_trading_bot.time.sleep"):
                    run_live(cfg, client)
        self.assertEqual(mock_cycle.call_count, 4)


class ExchangeClockTests(unittest.TestCase):
    def test_the_measured_offset_is_applied(self):
        client = MagicMock(spec=BinanceREST)
        client._time_offset_ms = 60_000
        self.assertAlmostEqual(exchange_now_ms(client), int(time.time() * 1000) + 60_000, delta=1000)

    def test_a_client_that_never_synced_uses_the_host_clock(self):
        self.assertAlmostEqual(exchange_now_ms(MagicMock(spec=BinanceREST)), int(time.time() * 1000),
                               delta=1000)


class EventLogTests(unittest.TestCase):
    def test_events_are_appended_one_json_object_per_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "nested" / "e.jsonl"
            append_event(log, "one", value=1)
            append_event(log, "two", when=Path("x"))
            lines = log.read_text().splitlines()
        self.assertEqual([json.loads(line)["event"] for line in lines], ["one", "two"])

    def test_a_failed_write_is_logged_not_raised(self):
        """An audit record is never a reason to leave a position unmanaged."""
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "file"
            blocker.write_text("")
            append_event(blocker / "e.jsonl", "cycle")   # parent is a file: the write fails

    def test_a_full_log_is_rotated_so_it_cannot_fill_the_disk(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "e.jsonl"
            for n in range(5):
                append_event(log, "cycle", max_bytes=100, n=n)
            rotated = log.with_name("e.jsonl.1")
            self.assertTrue(rotated.exists())
            self.assertLess(log.stat().st_size, 200)
            self.assertEqual(json.loads(log.read_text().splitlines()[-1])["n"], 4)


def _series(n=5, start=0, step=HOUR_MS):
    return [Candle(start + i * step, 100.0, 101.0, 99.0, 100.5, 1.0) for i in range(n)]


class MarketDataValidationTests(unittest.TestCase):
    def test_a_clean_series_passes_unchanged(self):
        candles = _series()
        self.assertIs(validate_candles(candles, "1h", now_ms=candles[-1].timestamp + HOUR_MS), candles)

    def test_non_positive_or_non_finite_prices_are_refused(self):
        for bad in (0.0, -1.0, float("nan"), float("inf")):
            candles = _series()
            candles[2] = Candle(candles[2].timestamp, bad, 101.0, 99.0, 100.5)
            with self.assertRaises(MarketDataError):
                validate_candles(candles)

    def test_negative_volume_is_refused(self):
        candles = _series()
        candles[1].volume = -1.0
        with self.assertRaises(MarketDataError):
            validate_candles(candles)

    def test_a_high_below_the_close_is_refused(self):
        candles = _series()
        candles[3] = Candle(candles[3].timestamp, 100.0, 100.2, 99.0, 100.5)
        with self.assertRaises(MarketDataError):
            validate_candles(candles)

    def test_out_of_order_timestamps_are_refused(self):
        candles = _series()
        candles[1], candles[2] = candles[2], candles[1]
        with self.assertRaises(MarketDataError):
            validate_candles(candles)

    def test_a_bar_off_the_interval_grid_is_refused(self):
        candles = _series()
        candles[-1].timestamp += 60 * 1000
        with self.assertRaises(MarketDataError):
            validate_candles(candles, "1h")

    def test_a_missing_bar_in_the_recent_window_is_refused(self):
        candles = _series(10)
        del candles[7]
        with self.assertRaises(MarketDataError):
            validate_candles(candles, "1h", lookback=5)

    def test_an_old_gap_outside_the_window_is_tolerated(self):
        """A maintenance gap 100 bars back would otherwise stop trading for 100 hours."""
        candles = _series(10)
        del candles[2]
        self.assertEqual(len(validate_candles(candles, "1h", lookback=5)), 9)

    def test_stale_data_is_refused(self):
        candles = _series()
        closed_at = candles[-1].timestamp + HOUR_MS
        validate_candles(candles, "1h", now_ms=closed_at + HOUR_MS - 1)   # next bar not due yet
        with self.assertRaises(MarketDataError):
            validate_candles(candles, "1h", now_ms=closed_at + HOUR_MS + 301 * 1000)

    def test_a_bar_that_has_not_closed_is_refused(self):
        candles = _series()
        with self.assertRaises(MarketDataError):
            validate_candles(candles, "1h", now_ms=candles[-1].timestamp + 1000)

    def test_a_forming_one_minute_bar_is_not_taken_as_closed(self):
        """Only clock error is tolerated, so even a 1m bar halfway through is refused."""
        candles = _series(step=60 * 1000)
        with self.assertRaises(MarketDataError):
            validate_candles(candles, "1m", now_ms=candles[-1].timestamp + 30 * 1000)

    def test_an_unknown_interval_is_refused(self):
        with self.assertRaises(MarketDataError):
            validate_candles(_series(), "7h")

    def test_backtest_refuses_an_unsorted_file(self):
        candles = _series(80)
        candles[10], candles[11] = candles[11], candles[10]
        with self.assertRaises(MarketDataError):
            backtest(candles, Config())


if __name__ == "__main__":
    unittest.main()
