#!/usr/bin/env python3
"""ORCA Money Bot: safe, extensible Binance paper-trading core.

Live orders are deliberately opt-in and require LIVE_TRADING_CONFIRM=I_UNDERSTAND_RISK.
No secret is read from source code; use environment variables or a secret manager.

Every open live position is tracked in a persisted ledger and carries a protective
bracket. What the bot holds is never inferred from open orders: a filled market buy
leaves none behind, so an open-order check would re-enter a symbol it is already in.
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import json
import logging
import math
import os
import random
import signal
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from dataclasses import fields as dataclass_fields
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Optional

LOG = logging.getLogger("orca")

ALLOWED_INTERVALS = ("1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h", "8h", "12h", "1d", "3d", "1w")

# Binance error codes worth naming rather than matching on a bare number.
ERROR_ORDER_DOES_NOT_EXIST = -2013
ERROR_INVALID_SIGNATURE = -1022
ERROR_INVALID_API_KEY = -2014
ERROR_KEY_NOT_PERMITTED = -2015


class Mode(str, Enum):
    PAPER = "paper"
    TESTNET = "testnet"
    LIVE = "live"


class Signal(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


class ConfigError(ValueError):
    """A setting could not be read from the environment.

    Raised while Config is constructed rather than at import, so `config-check` can
    report which variable is wrong instead of dying in a traceback from enum.py.
    """


def _env_str(name: str, default: str) -> str:
    return os.getenv(name, default)


def _env_int(name: str, default: str) -> int:
    raw = os.getenv(name, default)
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise ConfigError("%s must be a whole number, got %r" % (name, raw)) from None


def _env_float(name: str, default: str) -> float:
    raw = os.getenv(name, default)
    try:
        return float(raw)
    except (TypeError, ValueError):
        raise ConfigError("%s must be a number, got %r" % (name, raw)) from None


def _env_bool(name: str, default: str) -> bool:
    raw = os.getenv(name, default).strip().lower()
    if raw in ("1", "true", "yes", "on"):
        return True
    if raw in ("0", "false", "no", "off"):
        return False
    raise ConfigError("%s must be true or false (1/0), got %r" % (name, raw))


def _env_mode(name: str, default: str) -> Mode:
    raw = os.getenv(name, default).strip().lower()
    try:
        return Mode(raw)
    except ValueError:
        choices = ", ".join(mode.value for mode in Mode)
        raise ConfigError("%s must be one of %s, got %r" % (name, choices, raw)) from None


def _env_symbols(name: str, default: str) -> tuple:
    return tuple(s.strip().upper() for s in os.getenv(name, default).split(",") if s.strip())


@dataclass(frozen=True)
class Config:
    mode: Mode = field(default_factory=lambda: _env_mode("TRADING_MODE", "paper"))
    symbols: tuple[str, ...] = field(default_factory=lambda: _env_symbols("SYMBOLS", "BTCUSDT,ETHUSDT"))
    quote_asset: str = field(default_factory=lambda: _env_str("QUOTE_ASSET", "USDT"))
    initial_equity: float = field(default_factory=lambda: _env_float("PAPER_START_BALANCE", "10000"))
    risk_per_trade_pct: float = field(default_factory=lambda: _env_float("RISK_PER_TRADE_PCT", "0.25"))
    max_daily_loss_pct: float = field(default_factory=lambda: _env_float("MAX_DAILY_LOSS_PCT", "2"))
    max_drawdown_pct: float = field(default_factory=lambda: _env_float("MAX_DRAWDOWN_PCT", "10"))
    max_open_positions: int = field(default_factory=lambda: _env_int("MAX_OPEN_POSITIONS", "3"))
    max_trades_per_hour: int = field(default_factory=lambda: _env_int("MAX_TRADES_PER_HOUR", "6"))
    max_notional_pct: float = field(default_factory=lambda: _env_float("MAX_NOTIONAL_PCT", "20"))
    atr_period: int = field(default_factory=lambda: _env_int("ATR_PERIOD", "14"))
    atr_stop_mult: float = field(default_factory=lambda: _env_float("ATR_STOP_MULTIPLIER", "1.5"))
    atr_take_mult: float = field(default_factory=lambda: _env_float("ATR_TAKE_MULTIPLIER", "3"))
    stop_limit_buffer_pct: float = field(default_factory=lambda: _env_float("STOP_LIMIT_BUFFER_PCT", "0.2"))
    kline_interval: str = field(default_factory=lambda: _env_str("KLINE_INTERVAL", "1h"))
    api_key: str = field(default_factory=lambda: _env_str("BINANCE_API_KEY", ""), repr=False)
    api_secret: str = field(default_factory=lambda: _env_str("BINANCE_API_SECRET", ""), repr=False)
    live_confirmation: str = field(default_factory=lambda: _env_str("LIVE_TRADING_CONFIRM", ""))
    db_path: Path = field(default_factory=lambda: Path(_env_str("TRADES_DB_PATH", "data/trades.jsonl")))
    risk_state_path: Path = field(default_factory=lambda: Path(_env_str("RISK_STATE_PATH", "data/risk_state.json")))
    positions_path: Path = field(default_factory=lambda: Path(_env_str("POSITIONS_PATH", "data/positions.json")))
    live_poll_seconds: int = field(default_factory=lambda: _env_int("LIVE_POLL_SECONDS", "60"))
    recv_window_ms: int = field(default_factory=lambda: _env_int("RECV_WINDOW_MS", "5000"))
    filters_cache_seconds: int = field(default_factory=lambda: _env_int("FILTERS_CACHE_SECONDS", "3600"))
    max_consecutive_failures: int = field(default_factory=lambda: _env_int("MAX_CONSECUTIVE_FAILURES", "5"))
    max_weight_per_minute: int = field(default_factory=lambda: _env_int("MAX_WEIGHT_PER_MINUTE", "1200"))
    require_key_ip_restriction: bool = field(default_factory=lambda: _env_bool("REQUIRE_KEY_IP_RESTRICTION", "1"))

    @property
    def masked_key(self) -> str:
        """Enough of the key to tell two apart in a log, never enough to use."""
        return "%s...%s" % (self.api_key[:4], self.api_key[-4:]) if len(self.api_key) > 8 else "(unset)"

    def validate(self) -> None:
        if not self.symbols or any(not symbol.isalnum() for symbol in self.symbols):
            raise ValueError("SYMBOLS must contain valid alphanumeric Binance symbols")
        if self.initial_equity <= 0 or self.max_open_positions < 1 or self.max_trades_per_hour < 1:
            raise ValueError("Initial equity and trade limits must be positive")
        if self.live_poll_seconds < 5:
            raise ValueError("LIVE_POLL_SECONDS must be >= 5")
        if self.atr_period < 2 or self.atr_stop_mult <= 0 or self.atr_take_mult <= 0:
            raise ValueError("ATR settings must be positive")
        if self.risk_per_trade_pct <= 0 or self.risk_per_trade_pct > 2:
            raise ValueError("RISK_PER_TRADE_PCT must be > 0 and <= 2")
        # A limit outside (0, 100] is not a limit: 0 blocks everything and 10000 disables
        # the breaker while still reading like a setting.
        if not 0 < self.max_daily_loss_pct <= 100:
            raise ValueError("MAX_DAILY_LOSS_PCT must be > 0 and <= 100")
        if not 0 < self.max_drawdown_pct <= 100:
            raise ValueError("MAX_DRAWDOWN_PCT must be > 0 and <= 100")
        if not 0 < self.max_notional_pct <= 100:
            raise ValueError("MAX_NOTIONAL_PCT must be > 0 and <= 100")
        if not 0 <= self.stop_limit_buffer_pct < 100:
            raise ValueError("STOP_LIMIT_BUFFER_PCT must be >= 0 and < 100")
        if not 0 < self.recv_window_ms <= 60000:
            raise ValueError("RECV_WINDOW_MS must be > 0 and <= 60000")
        if self.max_consecutive_failures < 1:
            raise ValueError("MAX_CONSECUTIVE_FAILURES must be >= 1")
        if self.kline_interval not in ALLOWED_INTERVALS:
            raise ValueError("KLINE_INTERVAL must be one of %s" % ", ".join(ALLOWED_INTERVALS))
        if self.mode is Mode.LIVE and self.live_confirmation != "I_UNDERSTAND_RISK":
            raise ValueError("Live trading is locked. Set LIVE_TRADING_CONFIRM=I_UNDERSTAND_RISK explicitly.")
        if self.mode in (Mode.TESTNET, Mode.LIVE) and (not self.api_key or not self.api_secret):
            raise ValueError("Binance API credentials are required for testnet/live")


@dataclass
class Candle:
    timestamp: int
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class Position:
    symbol: str
    side: str
    entry: float
    quantity: float
    stop: float
    take: float
    opened_at: str


@dataclass
class Trade:
    symbol: str
    side: str
    entry: float
    exit: float
    quantity: float
    pnl: float
    reason: str
    opened_at: str
    closed_at: str


@dataclass
class LivePosition:
    """One real position the bot believes it holds.

    quantity == 0 means the entry order was sent but its outcome is not yet known;
    entry_client_order_id is what makes that answerable after a timeout or a crash.
    """
    symbol: str
    entry_price: float
    stop: float
    take: float
    opened_at: str
    entry_time_ms: int
    entry_client_order_id: str
    quantity: float = 0.0
    bracket_order_list_id: Optional[int] = None
    protected: bool = False


class BinanceError(RuntimeError):
    """A Binance REST failure with its numeric code and message preserved.

    urlopen throws the response body away with the exception, and that body is the only
    place Binance says what was actually wrong.
    """

    def __init__(self, status: int, code: Optional[int], msg: str, path: str):
        super().__init__("%s -> HTTP %s Binance code=%s: %s" % (path, status, code, msg))
        self.status = status
        self.code = code
        self.msg = msg
        self.path = path

    @property
    def is_retryable(self) -> bool:
        """429 is back-pressure and 5xx is Binance's side; both are worth one more try.
        418 is an IP ban and is deliberately excluded - retrying lengthens it."""
        return self.status == 429 or 500 <= self.status < 600

    @property
    def is_fatal(self) -> bool:
        """A bad key, a bad signature or a banned IP will not fix itself on the next poll."""
        return self.status in (401, 403, 418) or self.code in (
            ERROR_INVALID_SIGNATURE, ERROR_INVALID_API_KEY, ERROR_KEY_NOT_PERMITTED)


class BinanceREST:
    """Minimal signed REST adapter; no third-party SDK required."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.base = "https://testnet.binance.vision" if cfg.mode is Mode.TESTNET else "https://api.binance.com"
        self._time_offset_ms = 0
        self._used_weight = 0
        self._filters_cache: dict = {}

    # ---- transport -------------------------------------------------------

    def _request(self, path: str, params: dict | None = None, signed: bool = False,
                 method: str = "GET", _attempt: int = 0):
        sent = dict(params or {})
        if signed:
            if not self.cfg.api_secret:
                raise RuntimeError("%s needs API credentials, but none are configured" % path)
            sent.setdefault("recvWindow", self.cfg.recv_window_ms)
            sent["timestamp"] = int(time.time() * 1000) + self._time_offset_ms
            query = urllib.parse.urlencode(sent)
            sent["signature"] = hmac.new(self.cfg.api_secret.encode(), query.encode(), hashlib.sha256).hexdigest()
        # The key identifies the account, so it only travels on calls that need an account.
        headers = {"X-MBX-APIKEY": self.cfg.api_key} if signed and self.cfg.api_key else {}
        req = urllib.request.Request(
            "%s%s?%s" % (self.base, path, urllib.parse.urlencode(sent)), headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                self._note_weight(getattr(resp, "headers", None))
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            error = self._as_binance_error(exc, path)
            # Reads may be retried; an order POST may not. Without knowing whether the
            # first attempt landed, a retried order is a second position.
            if error.is_retryable and method == "GET" and _attempt < 2:
                delay = self._retry_delay(exc, _attempt)
                LOG.warning("HTTP %s on %s; sleeping %.1fs before retry %d",
                            error.status, path, delay, _attempt + 1)
                time.sleep(delay)
                return self._request(path, params, signed, method, _attempt + 1)
            raise error from None

    @staticmethod
    def _as_binance_error(exc: urllib.error.HTTPError, path: str) -> BinanceError:
        try:
            body = exc.read().decode(errors="replace")
        except Exception:  # noqa: BLE001 - a body we cannot read must not mask the status
            body = ""
        code = None
        msg = body or str(getattr(exc, "reason", "")) or "no error body"
        try:
            payload = json.loads(body)
            if isinstance(payload, dict):
                code = payload.get("code")
                msg = payload.get("msg", msg)
        except (json.JSONDecodeError, TypeError, ValueError):
            pass
        return BinanceError(exc.code, code, str(msg), path)

    @staticmethod
    def _retry_delay(exc: urllib.error.HTTPError, attempt: int) -> float:
        try:
            return max(1.0, float(exc.headers.get("Retry-After")))
        except (AttributeError, TypeError, ValueError):
            return float(2 ** attempt) + random.random()

    def _note_weight(self, headers) -> None:
        if headers is None:
            return
        try:
            self._used_weight = int(headers.get("X-MBX-USED-WEIGHT-1M"))
        except (AttributeError, TypeError, ValueError):
            return

    @property
    def used_weight(self) -> int:
        return self._used_weight

    def weight_is_critical(self) -> bool:
        """True once this minute's request weight is close enough to the cap that one more
        burst would earn a 429, and the 418 ban that follows repeated ones."""
        return self._used_weight >= self.cfg.max_weight_per_minute * 0.8

    def sync_time(self) -> int:
        """Binance rejects a signed request whose timestamp falls outside recvWindow, so a
        drifting host clock breaks every order until the clock is fixed."""
        payload = self._request("/api/v3/time")
        server_ms = payload.get("serverTime") if isinstance(payload, dict) else None
        if server_ms is None:
            LOG.warning("no server time in the response; leaving the clock offset at %dms", self._time_offset_ms)
            return self._time_offset_ms
        self._time_offset_ms = int(server_ms) - int(time.time() * 1000)
        LOG.info("clock offset against Binance: %dms", self._time_offset_ms)
        return self._time_offset_ms

    # ---- market data -----------------------------------------------------

    def klines(self, symbol: str, interval: str = "1h", limit: int = 300) -> list[Candle]:
        rows = self._request("/api/v3/klines", {"symbol": symbol, "interval": interval, "limit": limit})
        return [Candle(int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5])) for r in rows]

    def ticker_price(self, symbol: str) -> float:
        """The price right now. The last kline can be an interval old, which is no basis
        for placing a stop."""
        return float(self._request("/api/v3/ticker/price", {"symbol": symbol})["price"])

    def get_exchange_info(self, symbol: str | None = None) -> dict:
        """Public endpoint: trading rules and filters. No signing needed, same as klines()."""
        return self._request("/api/v3/exchangeInfo", {"symbol": symbol} if symbol else {})

    def get_symbol_filters(self, symbol: str) -> dict:
        """step_size/tick_size/min_qty/min_notional for one symbol, so a computed order
        quantity or price can be rounded to what Binance will actually accept instead of
        being rejected. Cached: exchangeInfo is heavy and these values change weekly at
        most, so re-fetching per order spends weight for nothing."""
        cached = self._filters_cache.get(symbol)
        if cached and time.time() - cached[0] < self.cfg.filters_cache_seconds:
            return cached[1]
        info = self.get_exchange_info(symbol)
        filters = {f["filterType"]: f for f in info["symbols"][0]["filters"]}
        lot = filters.get("LOT_SIZE", {})
        price_filter = filters.get("PRICE_FILTER", {})
        notional = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL") or {}
        resolved = {
            "step_size": float(lot["stepSize"]) if lot.get("stepSize") else None,
            "min_qty": float(lot["minQty"]) if lot.get("minQty") else None,
            "tick_size": float(price_filter["tickSize"]) if price_filter.get("tickSize") else None,
            "min_notional": float(notional["minNotional"]) if notional.get("minNotional") else None,
        }
        self._filters_cache[symbol] = (time.time(), resolved)
        return resolved

    # ---- account ---------------------------------------------------------

    def account(self) -> dict:
        return self._request("/api/v3/account", signed=True)

    def get_api_key_permissions(self) -> dict:
        """What this API key is allowed to do. Checked before the first live order."""
        return self._request("/sapi/v1/account/apiRestrictions", signed=True)

    # ---- orders ----------------------------------------------------------

    def market_order(self, symbol: str, side: str, quantity: float, client_order_id: str | None = None) -> dict:
        """Real market buy/sell. Binance side must be 'BUY' or 'SELL'.

        client_order_id is what makes a timed-out order answerable: Binance rejects a
        duplicate, and the order can be looked up by it afterwards.
        """
        if self.cfg.mode is not Mode.LIVE:
            raise RuntimeError("market_order is disabled outside live mode")
        params = {"symbol": symbol, "side": side, "type": "MARKET", "quantity": "%.8f" % quantity}
        if client_order_id:
            params["newClientOrderId"] = client_order_id
        return self._request("/api/v3/order", params, signed=True, method="POST")

    def place_oco_order(self, symbol: str, side: str, quantity: float, take_profit_price: float,
                        stop_price: float, stop_limit_price: float,
                        stop_limit_time_in_force: str = "GTC") -> dict:
        """Real stop-loss + take-profit bracket as a single Binance OCO order.

        side is the side that closes the position (e.g. 'SELL' to exit a long).
        take_profit_price is the limit leg; stop_price/stop_limit_price are the stop leg.
        """
        if self.cfg.mode is not Mode.LIVE:
            raise RuntimeError("place_oco_order is disabled outside live mode")
        params = {
            "symbol": symbol,
            "side": side,
            "quantity": "%.8f" % quantity,
            "price": "%.8f" % take_profit_price,
            "stopPrice": "%.8f" % stop_price,
            "stopLimitPrice": "%.8f" % stop_limit_price,
            "stopLimitTimeInForce": stop_limit_time_in_force,
        }
        return self._request("/api/v3/order/oco", params, signed=True, method="POST")

    def cancel_order(self, symbol: str, order_id: int) -> dict:
        if self.cfg.mode is not Mode.LIVE:
            raise RuntimeError("cancel_order is disabled outside live mode")
        return self._request("/api/v3/order", {"symbol": symbol, "orderId": order_id}, signed=True, method="DELETE")

    def cancel_oco_order(self, symbol: str, order_list_id: int) -> dict:
        if self.cfg.mode is not Mode.LIVE:
            raise RuntimeError("cancel_oco_order is disabled outside live mode")
        return self._request("/api/v3/orderList", {"symbol": symbol, "orderListId": order_list_id},
                             signed=True, method="DELETE")

    def cancel_all_open_orders(self, symbol: str) -> list[dict]:
        """Flattens every open order (including OCO legs) on one symbol in a single call."""
        if self.cfg.mode is not Mode.LIVE:
            raise RuntimeError("cancel_all_open_orders is disabled outside live mode")
        return self._request("/api/v3/openOrders", {"symbol": symbol}, signed=True, method="DELETE")

    def get_open_orders(self, symbol: str) -> list[dict]:
        return self._request("/api/v3/openOrders", {"symbol": symbol}, signed=True)

    def get_order(self, symbol: str, order_id: int) -> dict:
        return self._request("/api/v3/order", {"symbol": symbol, "orderId": order_id}, signed=True)

    def get_order_by_client_id(self, symbol: str, client_order_id: str) -> dict:
        """Look an order up by the id we chose for it, which is the only handle that
        survives a request whose response we never saw."""
        return self._request("/api/v3/order", {"symbol": symbol, "origClientOrderId": client_order_id}, signed=True)

    def get_all_orders(self, symbol: str, limit: int = 500) -> list[dict]:
        """Full order history for a symbol (not just currently-open orders)."""
        return self._request("/api/v3/allOrders", {"symbol": symbol, "limit": limit}, signed=True)

    def get_my_trades(self, symbol: str, limit: int = 500) -> list[dict]:
        """Actual executions/fills for a symbol - what a user sees under Trade History."""
        return self._request("/api/v3/myTrades", {"symbol": symbol, "limit": limit}, signed=True)

    def place_limit_order(self, symbol: str, side: str, quantity: float, price: float,
                          time_in_force: str = "GTC") -> dict:
        if self.cfg.mode is not Mode.LIVE:
            raise RuntimeError("place_limit_order is disabled outside live mode")
        params = {"symbol": symbol, "side": side, "type": "LIMIT", "timeInForce": time_in_force,
                  "quantity": "%.8f" % quantity, "price": "%.8f" % price}
        return self._request("/api/v3/order", params, signed=True, method="POST")

    def place_stop_loss_limit_order(self, symbol: str, side: str, quantity: float, stop_price: float,
                                    limit_price: float, time_in_force: str = "GTC") -> dict:
        """Standalone stop-loss order (not paired with a take-profit like place_oco_order)."""
        if self.cfg.mode is not Mode.LIVE:
            raise RuntimeError("place_stop_loss_limit_order is disabled outside live mode")
        params = {"symbol": symbol, "side": side, "type": "STOP_LOSS_LIMIT", "timeInForce": time_in_force,
                  "quantity": "%.8f" % quantity, "price": "%.8f" % limit_price, "stopPrice": "%.8f" % stop_price}
        return self._request("/api/v3/order", params, signed=True, method="POST")

    def place_take_profit_limit_order(self, symbol: str, side: str, quantity: float, stop_price: float,
                                      limit_price: float, time_in_force: str = "GTC") -> dict:
        """Standalone take-profit order (not paired with a stop-loss like place_oco_order)."""
        if self.cfg.mode is not Mode.LIVE:
            raise RuntimeError("place_take_profit_limit_order is disabled outside live mode")
        params = {"symbol": symbol, "side": side, "type": "TAKE_PROFIT_LIMIT", "timeInForce": time_in_force,
                  "quantity": "%.8f" % quantity, "price": "%.8f" % limit_price, "stopPrice": "%.8f" % stop_price}
        return self._request("/api/v3/order", params, signed=True, method="POST")

    # ---- convert ---------------------------------------------------------

    def get_convert_quote(self, from_asset: str, to_asset: str, from_amount: float,
                          valid_time: str = "10s") -> dict:
        """Firm, time-limited quote to swap one asset directly into another - Binance's
        Convert feature, the same swap a user gets tapping 'Convert' in the app.
        Read-only: getting a quote does not move any funds."""
        params = {"fromAsset": from_asset, "toAsset": to_asset,
                  "fromAmount": "%.8f" % from_amount, "validTime": valid_time}
        return self._request("/sapi/v1/convert/getQuote", params, signed=True, method="POST")

    def accept_convert_quote(self, quote_id: str) -> dict:
        if self.cfg.mode is not Mode.LIVE:
            raise RuntimeError("accept_convert_quote is disabled outside live mode")
        return self._request("/sapi/v1/convert/acceptQuote", {"quoteId": quote_id}, signed=True, method="POST")

    def get_convert_order_status(self, order_id: str | None = None, quote_id: str | None = None) -> dict:
        if not order_id and not quote_id:
            raise ValueError("get_convert_order_status requires order_id or quote_id")
        params = {"orderId": order_id} if order_id else {"quoteId": quote_id}
        return self._request("/sapi/v1/convert/orderStatus", params, signed=True)

    def convert(self, from_asset: str, to_asset: str, from_amount: float,
                min_to_amount: float | None = None) -> dict:
        """One-step convert: quote then immediately accept, like a single tap of
        'Convert' in the Binance app. Moves real funds; live mode only.

        min_to_amount is the floor you will accept. Without one this accepts whatever
        rate comes back, so pass it for anything unattended.
        """
        if self.cfg.mode is not Mode.LIVE:
            raise RuntimeError("convert is disabled outside live mode")
        quote = self.get_convert_quote(from_asset, to_asset, from_amount)
        if min_to_amount is not None:
            offered = float(quote.get("toAmount", 0) or 0)
            if offered < min_to_amount:
                raise ValueError(
                    "convert quote of %s %s is below the %s floor; not accepting"
                    % (offered, to_asset, min_to_amount))
        return self.accept_convert_quote(quote["quoteId"])


def round_to_step(value: float, step: Optional[float]) -> float:
    """Round down to the nearest multiple of step (e.g. LOT_SIZE stepSize / PRICE_FILTER
    tickSize) so real orders aren't rejected for violating exchange precision rules."""
    if not step:
        return value
    return round(math.floor(round(value / step, 8)) * step, 8)


def sma(values: list[float], period: int) -> Optional[float]:
    return sum(values[-period:]) / period if len(values) >= period else None


def atr(candles: list[Candle], period: int = 14) -> Optional[float]:
    if len(candles) < period + 1:
        return None
    trs = []
    for prev, cur in zip(candles[-period - 1:-1], candles[-period:]):
        trs.append(max(cur.high - cur.low, abs(cur.high - prev.close), abs(cur.low - prev.close)))
    return sum(trs) / len(trs)


class RegimeStrategy:
    """Conservative trend/mean-reversion hybrid with a volatility kill filter."""

    def __init__(self, cfg: Config):
        self.cfg = cfg

    def decide(self, candles: list[Candle]) -> Signal:
        if len(candles) < 60:
            return Signal.HOLD
        closes = [c.close for c in candles]
        fast, slow = sma(closes, 20), sma(closes, 50)
        a = atr(candles, self.cfg.atr_period)
        if fast is None or slow is None or a is None or closes[-1] <= 0:
            return Signal.HOLD
        vol = a / closes[-1]
        if vol > 0.08:  # abnormal volatility: preserve capital
            return Signal.HOLD
        change = closes[-1] / closes[-4] - 1
        if fast > slow and change > 0.002:
            return Signal.BUY
        if fast < slow and change < -0.002:
            return Signal.SELL
        return Signal.HOLD


class RiskGate:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.equity = cfg.initial_equity
        self.peak_equity = cfg.initial_equity
        self.day_start_equity = cfg.initial_equity
        self.daily_pnl = 0.0
        self.trades = 0
        self.day = datetime.now(timezone.utc).date()
        self.trade_times: list[float] = []
        self.live_anchored = False
        self._anchored = False

    def _roll_day(self) -> None:
        today = datetime.now(timezone.utc).date()
        if today != self.day:
            self.day = today
            self.daily_pnl = 0.0
            self.day_start_equity = self.equity
            self._anchored = False

    def _trim_hour(self) -> None:
        cutoff = time.time() - 3600
        self.trade_times = [stamp for stamp in self.trade_times if stamp >= cutoff]

    def set_equity(self, equity: float) -> None:
        """Anchor the gate to the real account balance.

        Sizing and the loss limits are percentages, and a percentage of a number from
        .env is not a percentage of your account.
        """
        self._roll_day()
        if not self._anchored:
            # Back out today's realized P&L so the daily-loss limit measures the whole day.
            self.day_start_equity = equity - self.daily_pnl
            if not self.live_anchored:
                # First contact with the real balance. A peak carried over from
                # PAPER_START_BALANCE would read as a 95% drawdown on a small account and
                # block every trade; only a peak set from real equity means anything.
                self.peak_equity = equity
                self.live_anchored = True
            self._anchored = True
        self.equity = equity
        self.peak_equity = max(self.peak_equity, equity)

    def approve(self, signal: Signal, price: float, a: Optional[float], open_count: int) -> tuple[bool, float, str]:
        self._roll_day()
        self._trim_hour()
        if signal is Signal.HOLD or price <= 0 or not a or open_count >= self.cfg.max_open_positions:
            return False, 0.0, "no-trade-condition"
        if len(self.trade_times) >= self.cfg.max_trades_per_hour:
            return False, 0.0, "hourly-trade-limit"
        basis = self.day_start_equity if self.day_start_equity else self.equity
        if self.daily_pnl <= -basis * self.cfg.max_daily_loss_pct / 100:
            return False, 0.0, "daily-loss-limit"
        drawdown = (self.peak_equity - self.equity) / self.peak_equity * 100 if self.peak_equity else 0
        if drawdown >= self.cfg.max_drawdown_pct:
            return False, 0.0, "max-drawdown"
        stop_distance = a * self.cfg.atr_stop_mult
        qty = (self.equity * self.cfg.risk_per_trade_pct / 100) / stop_distance
        return (qty > 0, qty, "approved" if qty > 0 else "invalid-size")

    def opened(self) -> None:
        """Count an entry against the hourly limit when it happens.

        Counting on close instead means a run that never closes anything never counts,
        and the limit never fires.
        """
        self._trim_hour()
        self.trade_times.append(time.time())

    def closed(self, pnl: float) -> None:
        self._roll_day()
        self.equity += pnl
        self.daily_pnl += pnl
        self.peak_equity = max(self.peak_equity, self.equity)
        self.trades += 1

    def state_dict(self) -> dict:
        return {"equity": self.equity, "peak_equity": self.peak_equity, "daily_pnl": self.daily_pnl,
                "day_start_equity": self.day_start_equity, "trades": self.trades,
                "day": self.day.isoformat(), "trade_times": self.trade_times,
                "live_anchored": self.live_anchored}

    def restore(self, state: dict) -> None:
        """Reload persisted risk state so daily-loss/drawdown/hourly limits survive across
        separate manual `live` invocations instead of silently resetting each run."""
        self.equity = state.get("equity", self.equity)
        self.peak_equity = state.get("peak_equity", self.peak_equity)
        self.daily_pnl = state.get("daily_pnl", self.daily_pnl)
        self.day_start_equity = state.get("day_start_equity", self.day_start_equity)
        self.trades = state.get("trades", self.trades)
        self.trade_times = state.get("trade_times", self.trade_times)
        self.live_anchored = state.get("live_anchored", self.live_anchored)
        if "day" in state:
            self.day = datetime.fromisoformat(state["day"]).date()
        self._roll_day()


class PaperBroker:
    def __init__(self, cfg: Config, risk: RiskGate):
        self.cfg, self.risk, self.positions, self.trades = cfg, risk, {}, []

    def open(self, symbol: str, signal: Signal, price: float, qty: float, a: float) -> None:
        side = "LONG" if signal is Signal.BUY else "SHORT"
        stop = price - a * self.cfg.atr_stop_mult if side == "LONG" else price + a * self.cfg.atr_stop_mult
        take = price + a * self.cfg.atr_take_mult if side == "LONG" else price - a * self.cfg.atr_take_mult
        self.positions[symbol] = Position(symbol, side, price, qty, stop, take,
                                          datetime.now(timezone.utc).isoformat())
        self.risk.opened()

    def mark(self, symbol: str, price: float, signal: Signal = Signal.HOLD) -> Optional[Trade]:
        p = self.positions.get(symbol)
        if not p:
            return None
        reason = "signal" if (p.side == "LONG" and signal is Signal.SELL) or (
            p.side == "SHORT" and signal is Signal.BUY) else "risk"
        hit = (p.side == "LONG" and (price <= p.stop or price >= p.take)) or (
            p.side == "SHORT" and (price >= p.stop or price <= p.take))
        if not hit and reason == "risk":
            return None
        pnl = (price - p.entry) * p.quantity if p.side == "LONG" else (p.entry - price) * p.quantity
        t = Trade(p.symbol, p.side, p.entry, price, p.quantity, pnl, "stop/take" if hit else reason,
                  p.opened_at, datetime.now(timezone.utc).isoformat())
        self.trades.append(t)
        self.risk.closed(pnl)
        del self.positions[symbol]
        return t


def load_csv(path: str) -> list[Candle]:
    with open(path, newline="", encoding="utf-8") as f:
        return [Candle(int(r["timestamp"]), float(r["open"]), float(r["high"]), float(r["low"]),
                       float(r["close"]), float(r.get("volume", 0))) for r in csv.DictReader(f)]


def backtest(candles: list[Candle], cfg: Config) -> dict[str, float]:
    if not candles or not cfg.symbols:
        return {"trades": 0, "pnl": 0.0, "win_rate_pct": 0.0, "profit_factor": 0.0}
    strategy, risk = RegimeStrategy(cfg), RiskGate(cfg)
    broker = PaperBroker(cfg, risk)
    for i in range(len(candles)):
        window = candles[:i + 1]
        c = candles[i]
        sig = strategy.decide(window)
        closed = broker.mark(cfg.symbols[0], c.close, sig)
        if closed:
            LOG.info("closed %s pnl=%.2f", closed.symbol, closed.pnl)
        ok, qty, _ = risk.approve(sig, c.close, atr(window, cfg.atr_period), len(broker.positions))
        if ok and cfg.symbols[0] not in broker.positions:
            broker.open(cfg.symbols[0], sig, c.close, qty, atr(window, cfg.atr_period) or 0)
    if broker.positions:
        broker.mark(cfg.symbols[0], candles[-1].close, Signal.SELL)
    wins = sum(t.pnl > 0 for t in broker.trades)
    gross_win = sum(t.pnl for t in broker.trades if t.pnl > 0)
    gross_loss = -sum(t.pnl for t in broker.trades if t.pnl < 0)
    return {"trades": len(broker.trades),
            "pnl": round(sum(t.pnl for t in broker.trades), 8),
            "win_rate_pct": round(wins / len(broker.trades) * 100, 2) if broker.trades else 0.0,
            "profit_factor": round(gross_win / gross_loss, 4) if gross_loss else 0.0}


# ---- durable state -------------------------------------------------------

def _atomic_write_text(path: Path, text: str) -> None:
    """Write through a temporary file in the same directory, then rename over the target.

    write_text truncates before it writes, so a crash halfway leaves a corrupt file -
    and a corrupt risk file silently resets every circuit breaker to zero.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=str(path.parent),
                                         prefix=path.name + ".", suffix=".tmp", delete=False)
    try:
        with handle as tmp:
            tmp.write(text)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(handle.name, str(path))
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def load_risk_state(path: Path, strict: bool = False) -> dict:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        if strict:
            raise RuntimeError(
                "risk state at %s is unreadable (%s). Starting fresh would reset the daily-loss, "
                "drawdown and hourly limits to zero, so the live loop refuses to start. Inspect "
                "or delete the file deliberately." % (path, exc)) from None
        LOG.warning("risk state at %s is unreadable; starting fresh", path)
        return {}


def save_risk_state(path: Path, risk: RiskGate) -> None:
    _atomic_write_text(path, json.dumps(risk.state_dict()))


class PositionLedger:
    """What the bot believes it holds right now, persisted across cycles and restarts.

    Open orders cannot stand in for this. A filled market buy leaves no open order, so a
    bot that asks "are there open orders?" concludes it holds nothing and buys again.
    """

    def __init__(self, path: Optional[Path] = None):
        self.path = path
        self.positions: dict = {}

    def load(self) -> "PositionLedger":
        if not self.path or not self.path.exists():
            return self
        try:
            raw = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            raise RuntimeError(
                "position ledger at %s is unreadable (%s). It records open positions, so "
                "starting without it could double a position. Inspect it before retrying."
                % (self.path, exc)) from None
        known = {f.name for f in dataclass_fields(LivePosition)}
        self.positions = {symbol: LivePosition(**{k: v for k, v in item.items() if k in known})
                          for symbol, item in raw.items()}
        return self

    def save(self) -> None:
        if not self.path:
            return
        _atomic_write_text(self.path, json.dumps(
            {symbol: asdict(p) for symbol, p in self.positions.items()}, indent=2))

    def holds(self, symbol: str) -> bool:
        return symbol in self.positions

    def get(self, symbol: str) -> Optional[LivePosition]:
        return self.positions.get(symbol)

    def record(self, position: LivePosition) -> None:
        self.positions[position.symbol] = position
        self.save()

    def drop(self, symbol: str) -> None:
        self.positions.pop(symbol, None)
        self.save()

    def __len__(self) -> int:
        return len(self.positions)


# ---- live helpers --------------------------------------------------------

def assert_key_is_trade_only(client: BinanceREST, cfg: Config) -> None:
    """Fail closed before the first live order.

    A key that can withdraw is a key that can empty the account, and that is a different
    category of risk from anything the strategy can do.
    """
    try:
        restrictions = client.get_api_key_permissions()
    except Exception as exc:  # noqa: BLE001 - an unverifiable key is treated as unsafe
        raise RuntimeError(
            "could not read this API key's restrictions, so live trading is refused (%s). "
            "Grant the key permission to read its own restrictions, then retry." % exc) from None
    if restrictions.get("enableWithdrawals"):
        raise RuntimeError(
            "this API key is allowed to withdraw funds; live trading is refused. "
            "Create a key with spot trading only and no withdrawal permission.")
    if cfg.require_key_ip_restriction and not restrictions.get("ipRestrict"):
        raise RuntimeError(
            "this API key has no IP allowlist, so it works from anywhere it leaks to; live "
            "trading is refused. Restrict the key to this host's address, or set "
            "REQUIRE_KEY_IP_RESTRICTION=0 if you accept that risk.")
    LOG.info("API key %s checked: withdrawals disabled, IP restricted=%s",
             cfg.masked_key, bool(restrictions.get("ipRestrict")))


def _reraise_if_fatal(exc: BaseException) -> None:
    """A bad key, a bad signature or a banned IP is not something the next cycle fixes.

    Every broad handler in the live path calls this first, so a fatal error reaches
    run_live and stops the loop instead of being retried every poll forever.
    """
    if isinstance(exc, BinanceError) and exc.is_fatal:
        raise exc


def _client_order_id(prefix: str, symbol: str) -> str:
    stamp = "%x%04x" % (int(time.time() * 1000), random.randrange(16 ** 4))
    return ("%s-%s-%s" % (prefix, symbol, stamp))[:36]


def _average_fill_price(order: dict, fallback: float) -> float:
    executed = float(order.get("executedQty", 0) or 0)
    quote = float(order.get("cummulativeQuoteQty", 0) or 0)
    return quote / executed if executed > 0 and quote > 0 else fallback


def _base_asset(symbol: str, cfg: Config) -> str:
    return symbol[:-len(cfg.quote_asset)] if symbol.endswith(cfg.quote_asset) else ""


def _net_filled_quantity(order: dict, symbol: str, cfg: Config) -> float:
    """What is actually sellable after fees.

    A commission charged in the base asset comes straight out of the coins just bought,
    so bracketing the gross quantity is rejected for insufficient balance.
    """
    executed = float(order.get("executedQty", 0) or 0)
    base = _base_asset(symbol, cfg)
    fees = sum(float(f.get("commission", 0) or 0) for f in (order.get("fills") or [])
               if base and f.get("commissionAsset") == base)
    return max(0.0, executed - fees)


def filled_quantity_for_order(client: BinanceREST, cfg: Config, symbol: str, order: dict) -> float:
    """Sellable quantity for an order, whichever way we came by it.

    A POST response carries `fills` with their commissions. A lookup by client order id
    does not, so the fills are read back from trade history - otherwise a fee charged in
    the base asset is invisible and the bracket is sized for coins that are not there.
    """
    if order.get("fills"):
        return _net_filled_quantity(order, symbol, cfg)
    executed = float(order.get("executedQty", 0) or 0)
    order_id = order.get("orderId")
    if executed <= 0 or order_id is None:
        return max(0.0, executed)
    base = _base_asset(symbol, cfg)
    try:
        fills = [t for t in client.get_my_trades(symbol) if t.get("orderId") == order_id]
    except Exception:  # noqa: BLE001 - fall back to the gross quantity rather than nothing
        LOG.warning("could not read fills for order %s on %s", order_id, symbol, exc_info=True)
        return executed
    if not fills:
        return executed
    fees = sum(float(t.get("commission", 0) or 0) for t in fills
               if base and t.get("commissionAsset") == base)
    return max(0.0, sum(float(t["qty"]) for t in fills) - fees)


def _stop_limit_price(stop: float, cfg: Config, tick_size: Optional[float]) -> float:
    """The limit leg sits below the trigger so a fast move still fills it.

    A stop-limit priced exactly at its own trigger often rests unfilled while the price
    runs past it, leaving a position that reads as protected but is not.
    """
    limit = stop * (1 - cfg.stop_limit_buffer_pct / 100)
    rounded = round_to_step(limit, tick_size)
    return rounded if rounded > 0 else limit


def realized_pnl(client: BinanceREST, cfg: Config, position: LivePosition) -> tuple[float, float, int]:
    """Realized P&L in the quote asset from the exit fills recorded after the entry.

    Returns (pnl, quantity_exited, last_exit_ms). The timestamp is what lets a partially
    exited position advance its watermark, so the same fills are not counted twice on the
    next cycle.

    Commission is deducted only where Binance charged it in the quote asset; a fee taken
    in BNB or the base asset is left out rather than guessed at.
    """
    trades = client.get_my_trades(position.symbol)
    exits = [t for t in trades
             if not t.get("isBuyer") and int(t.get("time", 0)) >= position.entry_time_ms]
    quantity = sum(float(t["qty"]) for t in exits)
    if quantity <= 0:
        return 0.0, 0.0, position.entry_time_ms
    proceeds = sum(float(t["quoteQty"]) for t in exits)
    commission = sum(float(t.get("commission", 0) or 0) for t in exits
                     if t.get("commissionAsset") == cfg.quote_asset)
    last_exit = max(int(t.get("time", 0)) for t in exits)
    return proceeds - position.entry_price * quantity - commission, quantity, last_exit


def live_equity(client: BinanceREST, cfg: Config, ledger: PositionLedger) -> float:
    """Account value in the quote asset: the free and locked quote balance plus what the
    open positions cost. Ignoring open positions would read as a drawdown the moment the
    bot puts money to work."""
    quote = 0.0
    for entry in client.account()["balances"]:
        if entry.get("asset") == cfg.quote_asset:
            quote = float(entry.get("free", 0) or 0) + float(entry.get("locked", 0) or 0)
            break
    committed = sum(p.entry_price * p.quantity for p in ledger.positions.values())
    return quote + committed


def refresh_equity(cfg: Config, client: BinanceREST, risk: RiskGate, ledger: PositionLedger) -> bool:
    try:
        equity = live_equity(client, cfg, ledger)
    except Exception as exc:  # noqa: BLE001 - without a balance there is no safe size to trade
        _reraise_if_fatal(exc)
        LOG.exception("could not read the account balance; skipping this cycle")
        return False
    if equity <= 0:
        LOG.error("account balance in %s reads as %s; skipping this cycle", cfg.quote_asset, equity)
        return False
    risk.set_equity(equity)
    return True


def flatten_position(cfg: Config, client: BinanceREST, risk: RiskGate, ledger: PositionLedger,
                     position: LivePosition, reason: str) -> dict:
    """Sell back what was just bought and forget the position.

    Losing the round-trip fee is the cheap outcome; leaving an unprotected position
    running is not.
    """
    try:
        client.cancel_all_open_orders(position.symbol)
    except Exception:  # noqa: BLE001 - best effort; the exit below is what matters
        LOG.warning("could not cancel open orders on %s while flattening", position.symbol, exc_info=True)
    exit_order = None
    if position.quantity > 0:
        try:
            filters = client.get_symbol_filters(position.symbol)
            qty = round_to_step(position.quantity, filters["step_size"])
            if qty > 0:
                exit_order = client.market_order(position.symbol, "SELL", qty,
                                                 client_order_id=_client_order_id("orcax", position.symbol))
        except Exception:  # noqa: BLE001 - keep the position on the books so the next cycle retries
            LOG.exception("could not flatten %s; it is still open and unprotected", position.symbol)
            position.protected = False
            ledger.record(position)
            return {"symbol": position.symbol, "action": "alert", "reason": "flatten-failed-position-open"}
    try:
        pnl, exited, _ = realized_pnl(client, cfg, position)
        if exited > 0:
            risk.closed(pnl)
    except Exception:  # noqa: BLE001 - the position is closed either way
        LOG.exception("flattened %s but could not book its P&L", position.symbol)
    ledger.drop(position.symbol)
    LOG.warning("flattened %s (%s)", position.symbol, reason)
    return {"symbol": position.symbol, "action": "flattened", "reason": reason, "order": exit_order}


def ensure_bracket(cfg: Config, client: BinanceREST, risk: RiskGate, ledger: PositionLedger,
                   position: LivePosition, filters: dict) -> dict:
    """Give an open position its stop-loss and take-profit, or close it.

    There is no third branch on purpose: a position whose bracket could not be placed is
    a position with no stop, and the next cycle would not even see it as open.
    """
    try:
        stop_limit = _stop_limit_price(position.stop, cfg, filters.get("tick_size"))
        bracket = client.place_oco_order(position.symbol, "SELL", position.quantity,
                                         take_profit_price=position.take,
                                         stop_price=position.stop,
                                         stop_limit_price=stop_limit)
    except Exception:  # noqa: BLE001 - any failure here means an unprotected position
        LOG.exception("bracket failed for %s; flattening the position now", position.symbol)
        return flatten_position(cfg, client, risk, ledger, position, "bracket-failed")
    position.bracket_order_list_id = bracket.get("orderListId") if isinstance(bracket, dict) else None
    position.protected = True
    ledger.record(position)
    LOG.info("opened %s qty=%s entry=%.8f stop=%.8f take=%.8f",
             position.symbol, position.quantity, position.entry_price, position.stop, position.take)
    return {"symbol": position.symbol, "action": "opened", "quantity": position.quantity,
            "entry": position.entry_price, "stop": position.stop, "take": position.take,
            "bracket": bracket}


def recover_unconfirmed_entries(cfg: Config, client: BinanceREST, risk: RiskGate,
                                ledger: PositionLedger) -> list[dict]:
    """Resolve entries whose outcome the bot never saw.

    A timeout on an order POST is genuinely ambiguous. The client order id turns that
    into a question Binance can answer, instead of a guess that either abandons a real
    position or duplicates one.
    """
    results = []
    for symbol in list(ledger.positions):
        position = ledger.get(symbol)
        if position is None or position.quantity > 0:
            continue
        try:
            order = client.get_order_by_client_id(symbol, position.entry_client_order_id)
        except BinanceError as exc:
            if exc.code == ERROR_ORDER_DOES_NOT_EXIST:
                LOG.info("entry %s never reached Binance; clearing the reservation",
                         position.entry_client_order_id)
                ledger.drop(symbol)
                results.append({"symbol": symbol, "action": "recovered", "reason": "entry-never-placed"})
            else:
                LOG.error("could not resolve entry %s: %s", position.entry_client_order_id, exc)
            continue
        except Exception as exc:  # noqa: BLE001 - leave it on the books and retry next cycle
            _reraise_if_fatal(exc)
            LOG.exception("could not resolve entry %s", position.entry_client_order_id)
            continue
        filled = filled_quantity_for_order(client, cfg, symbol, order)
        status = str(order.get("status", ""))
        if filled <= 0:
            if status in ("CANCELED", "EXPIRED", "REJECTED"):
                ledger.drop(symbol)
                results.append({"symbol": symbol, "action": "recovered",
                                "reason": "entry-%s" % status.lower()})
            else:
                results.append({"symbol": symbol, "action": "waiting", "reason": "entry-not-filled"})
            continue
        position.quantity = filled
        position.entry_price = _average_fill_price(order, position.entry_price)
        ledger.record(position)
        LOG.warning("recovered an unconfirmed entry on %s: %s filled", symbol, filled)
        results.append({"symbol": symbol, "action": "recovered", "reason": "entry-filled", "quantity": filled})
    return results


def protect_open_positions(cfg: Config, client: BinanceREST, risk: RiskGate,
                           ledger: PositionLedger) -> list[dict]:
    """Every held position must carry a bracket before anything else happens."""
    results = []
    for symbol in list(ledger.positions):
        position = ledger.get(symbol)
        if position is None or position.protected or position.quantity <= 0:
            continue
        try:
            filters = client.get_symbol_filters(symbol)
        except Exception as exc:  # noqa: BLE001
            _reraise_if_fatal(exc)
            LOG.exception("no filters for %s; cannot bracket it this cycle", symbol)
            continue
        results.append(ensure_bracket(cfg, client, risk, ledger, position, filters))
    return results


def reconcile_positions(cfg: Config, client: BinanceREST, risk: RiskGate,
                        ledger: PositionLedger) -> list[dict]:
    """Turn brackets that have resolved into realized P&L on the RiskGate.

    Until this runs, daily_pnl stays at zero, which means the daily-loss and drawdown
    limits are reading a number that never moves.
    """
    results = []
    for symbol in list(ledger.positions):
        position = ledger.get(symbol)
        if position is None or position.quantity <= 0 or not position.protected:
            continue
        try:
            if client.get_open_orders(symbol):
                continue
        except Exception as exc:  # noqa: BLE001 - leave it alone rather than act on a bad read
            _reraise_if_fatal(exc)
            LOG.exception("could not read open orders for %s; leaving it in the ledger", symbol)
            continue
        try:
            pnl, exited, last_exit_ms = realized_pnl(client, cfg, position)
        except Exception as exc:  # noqa: BLE001
            _reraise_if_fatal(exc)
            LOG.exception("could not reconcile %s; leaving it in the ledger", symbol)
            continue
        if exited <= 0:
            # No bracket and no exit fills: the position is unprotected, not closed.
            LOG.error("%s has no bracket and no exit fills; flattening it", symbol)
            results.append(flatten_position(cfg, client, risk, ledger, position, "unprotected-on-reconcile"))
            continue
        remaining = position.quantity - exited
        if remaining > 0 and remaining / position.quantity > 0.01:
            # Only part of the bracket filled. Book what closed, keep the rest on the books
            # and let this cycle bracket it again rather than treating it as flat.
            risk.closed(pnl)
            position.quantity = remaining
            position.protected = False
            position.bracket_order_list_id = None
            # Move the watermark past what was just booked, so the next cycle does not
            # count these same fills again.
            position.entry_time_ms = last_exit_ms + 1
            ledger.record(position)
            LOG.warning("%s exited %s of %s; re-protecting the remainder",
                        symbol, exited, exited + remaining)
            results.append({"symbol": symbol, "action": "partially-closed", "pnl": pnl,
                            "remaining": remaining})
            continue
        risk.closed(pnl)
        ledger.drop(symbol)
        LOG.info("closed %s pnl=%.8f equity=%.8f daily=%.8f", symbol, pnl, risk.equity, risk.daily_pnl)
        results.append({"symbol": symbol, "action": "closed", "pnl": pnl})
    return results


def run_live_cycle(cfg: Config, client: BinanceREST, risk: RiskGate, strategy: RegimeStrategy,
                   ledger: Optional[PositionLedger] = None) -> dict:
    """One real evaluate-and-act pass over every symbol.

    Binance spot has no shorting: a SELL signal only ever closes a position you already
    hold, so it is skipped here rather than sent as a broken sell-to-open.

    The order of work matters. Unconfirmed entries are resolved first, then brackets that
    have already resolved are booked as realized P&L, then anything still unprotected is
    given a bracket, and only then is a new entry considered - against the real account
    balance. Reconciling before bracketing is deliberate: a bracket placed earlier in the
    same pass has not reached the exchange's open orders yet, and would read as resolved.
    """
    ledger = ledger if ledger is not None else PositionLedger()
    results: list[dict] = []
    results.extend(recover_unconfirmed_entries(cfg, client, risk, ledger))
    results.extend(reconcile_positions(cfg, client, risk, ledger))
    results.extend(protect_open_positions(cfg, client, risk, ledger))

    if not refresh_equity(cfg, client, risk, ledger):
        return {"results": results + [{"symbol": None, "action": "skipped", "reason": "equity-unavailable"}],
                "equity": risk.equity, "open_positions": len(ledger)}

    for symbol in cfg.symbols:
        if ledger.holds(symbol):
            results.append({"symbol": symbol, "action": "skipped", "reason": "position-open"})
            continue
        if client.weight_is_critical():
            results.append({"symbol": symbol, "action": "skipped", "reason": "rate-limit-budget"})
            continue
        candles = client.klines(symbol, cfg.kline_interval)
        # The final kline is the bar still forming. Its close repaints until the interval
        # ends, so a signal read from it can appear, trade, and then vanish.
        closed_candles = candles[:-1] if len(candles) > 1 else []
        if not closed_candles:
            results.append({"symbol": symbol, "action": "skipped", "reason": "no-data"})
            continue
        decision = strategy.decide(closed_candles)
        if decision is Signal.SELL:
            results.append({"symbol": symbol, "action": "skipped", "reason": "spot-no-short"})
            continue
        a = atr(closed_candles, cfg.atr_period)
        try:
            price = client.ticker_price(symbol)
        except Exception:  # noqa: BLE001
            LOG.exception("no live price for %s; skipping it this cycle", symbol)
            results.append({"symbol": symbol, "action": "skipped", "reason": "no-price"})
            continue
        ok, qty, reason = risk.approve(decision, price, a, len(ledger))
        if not ok:
            results.append({"symbol": symbol, "action": "no-trade", "reason": reason})
            continue
        try:
            filters = client.get_symbol_filters(symbol)
        except Exception:  # noqa: BLE001
            LOG.exception("no filters for %s; skipping it this cycle", symbol)
            results.append({"symbol": symbol, "action": "skipped", "reason": "no-filters"})
            continue
        # The risk budget divided by a small ATR is a large order: the calmer the market,
        # the bigger the position a fixed risk buys. Cap it against the real balance.
        max_notional = risk.equity * cfg.max_notional_pct / 100
        if qty * price > max_notional:
            LOG.info("%s size capped by MAX_NOTIONAL_PCT: %.8f -> %.8f", symbol, qty, max_notional / price)
            qty = max_notional / price
        qty = round_to_step(qty, filters["step_size"])
        if filters["min_qty"] and qty < filters["min_qty"]:
            results.append({"symbol": symbol, "action": "no-trade", "reason": "below-min-qty"})
            continue
        if filters["min_notional"] and qty * price < filters["min_notional"]:
            results.append({"symbol": symbol, "action": "no-trade", "reason": "below-min-notional"})
            continue
        stop = round_to_step(price - a * cfg.atr_stop_mult, filters["tick_size"])
        take = round_to_step(price + a * cfg.atr_take_mult, filters["tick_size"])
        if stop <= 0 or stop >= price or take <= price:
            results.append({"symbol": symbol, "action": "no-trade", "reason": "invalid-bracket-prices"})
            continue
        client_order_id = _client_order_id("orca", symbol)
        position = LivePosition(symbol=symbol, entry_price=price, stop=stop, take=take,
                                opened_at=datetime.now(timezone.utc).isoformat(),
                                # A second of slack so a fill stamped just before the send
                                # still counts as this position's.
                                entry_time_ms=int(time.time() * 1000) - 1000,
                                entry_client_order_id=client_order_id)
        # Written before the order is sent: if the response never arrives, the next cycle
        # still knows what to ask Binance about.
        ledger.record(position)
        try:
            order = client.market_order(symbol, "BUY", qty, client_order_id=client_order_id)
        except Exception:  # noqa: BLE001 - recovery resolves it by client order id
            LOG.exception("entry order failed for %s; the next cycle will resolve it by client order id", symbol)
            results.append({"symbol": symbol, "action": "failed", "reason": "entry-order-failed"})
            continue
        filled = _net_filled_quantity(order, symbol, cfg)
        if filled <= 0:
            ledger.drop(symbol)
            results.append({"symbol": symbol, "action": "no-trade", "reason": "entry-unfilled"})
            continue
        position.quantity = round_to_step(filled, filters["step_size"])
        position.entry_price = _average_fill_price(order, price)
        ledger.record(position)
        risk.opened()
        results.append(ensure_bracket(cfg, client, risk, ledger, position, filters))
    return {"results": results, "equity": risk.equity, "open_positions": len(ledger)}


def run_live(cfg: Config, client: BinanceREST) -> None:
    """Autonomous live loop: starts only when you run this command yourself, then
    keeps evaluating and trading on its own every LIVE_POLL_SECONDS until you stop
    it (Ctrl+C / SIGTERM), a fatal error arrives, or too many cycles fail in a row."""
    if cfg.mode is not Mode.LIVE:
        raise RuntimeError("live requires TRADING_MODE=live with LIVE_TRADING_CONFIRM set")
    assert_key_is_trade_only(client, cfg)
    client.sync_time()
    risk, strategy = RiskGate(cfg), RegimeStrategy(cfg)
    risk.restore(load_risk_state(cfg.risk_state_path, strict=True))
    ledger = PositionLedger(cfg.positions_path).load()
    stop_requested = {"flag": False}

    def _handle_stop(signum, _frame):
        LOG.info("stop signal %s received; exiting after the current cycle", signum)
        stop_requested["flag"] = True

    signal.signal(signal.SIGINT, _handle_stop)
    signal.signal(signal.SIGTERM, _handle_stop)
    LOG.info("live loop started for %s, polling every %ss - started by explicit command, not scheduled",
             cfg.symbols, cfg.live_poll_seconds)
    failures = 0
    while not stop_requested["flag"]:
        try:
            run_live_cycle(cfg, client, risk, strategy, ledger)
            failures = 0
        except BinanceError as exc:
            failures += 1
            LOG.error("live cycle failed: %s", exc)
            if exc.is_fatal:
                LOG.error("this will not recover by retrying; stopping the live loop")
                break
        except Exception:  # noqa: BLE001
            failures += 1
            LOG.exception("live cycle failed; will retry next poll")
        save_risk_state(cfg.risk_state_path, risk)
        if failures >= cfg.max_consecutive_failures:
            LOG.error("%d cycles failed in a row; stopping rather than running unattended in a broken state",
                      failures)
            break
        for _ in range(cfg.live_poll_seconds):
            if stop_requested["flag"]:
                break
            time.sleep(1)
    save_risk_state(cfg.risk_state_path, risk)
    ledger.save()
    LOG.info("live loop stopped cleanly")


def main() -> int:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["backtest", "fetch", "config-check", "live"])
    parser.add_argument("--csv", help="CSV columns: timestamp,open,high,low,close,volume")
    args = parser.parse_args()
    try:
        cfg = Config()
        cfg.validate()
    except ValueError as exc:
        # ConfigError included: name the setting instead of dying in a library traceback.
        LOG.error("configuration error: %s", exc)
        return 2
    if args.command == "config-check":
        print(json.dumps({"mode": cfg.mode.value, "symbols": cfg.symbols,
                          "live_enabled": cfg.mode is Mode.LIVE, "api_key": cfg.masked_key}, indent=2))
        return 0
    if args.command == "backtest":
        if not args.csv:
            parser.error("--csv is required for backtest")
        print(json.dumps(backtest(load_csv(args.csv), cfg), indent=2))
        return 0
    client = BinanceREST(cfg)
    if not cfg.symbols:
        raise SystemExit("SYMBOLS is empty")
    if args.command == "live":
        run_live(cfg, client)
        return 0
    print(json.dumps([c.__dict__ for c in client.klines(cfg.symbols[0], cfg.kline_interval)[-5:]], indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
