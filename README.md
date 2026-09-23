# ORCA-MONEY-BOT

نواة احترافية قابلة للتطوير لبوت تداول Binance، مع فصل واضح بين البيانات والاستراتيجية ومحرك المخاطر والتنفيذ. هذه النسخة **لا تنفذ تداولًا حقيقيًا افتراضيًا**؛ الوضع الافتراضي هو `paper`.

## التشغيل

```bash
python3 binance_trading_bot.py config-check
python3 -m unittest -v
python3 binance_trading_bot.py backtest --csv data/sample.csv
```

صيغة CSV: `timestamp,open,high,low,close,volume`.

لجلب شموع عامة من Binance:

```bash
export TRADING_MODE=paper
export SYMBOLS=BTCUSDT
python3 binance_trading_bot.py fetch
```

لـ Testnet فقط، وبعد إنشاء مفاتيح مخصصة له:

```bash
export TRADING_MODE=testnet
export BINANCE_API_KEY='...'
export BINANCE_API_SECRET='...'
python3 binance_trading_bot.py fetch
```

**التداول الحي غير موصى به قبل اختبار طويل ومراجعة مستقلة.** البوت نفسه يرفض الآن التشغيل الحي بمفتاح يملك صلاحية السحب أو بلا IP allowlist، لكن ذلك لا يغني عن إنشاء المفتاح بصلاحية تداول فقط من البداية. لا تستخدم مفاتيح بصلاحية السحب، وفَعّل IP allowlist، وابدأ بصلاحية قراءة/تداول فقط. لا يوجد في هذه النسخة نظام أخبار أو تحليل سياسي/اجتماعي آلي؛ إدخال هذه المصادر يحتاج مزود بيانات موثوق، تعريفًا زمنيًا واضحًا، واختبارات تمنع تحويل الأخبار إلى قرارات غير قابلة للتدقيق.

## Safety settings

All optional, with the defaults shown. They only matter in live mode. `.env.example` lists
every setting with a comment.

| Variable | Default | What it does |
| --- | --- | --- |
| `MAX_NOTIONAL_PCT` | `20` | Caps one order's notional as a share of the account. The risk budget divided by a small ATR is a large order, so the calmer the market the bigger the position a fixed risk buys. |
| `STOP_LIMIT_BUFFER_PCT` | `0.2` | How far the OCO stop-limit leg sits below its trigger. A stop-limit priced at its own trigger often rests unfilled while price runs past it. |
| `POSITIONS_PATH` | `data/positions.json` | Where the position ledger is kept. |
| `REQUIRE_KEY_IP_RESTRICTION` | `1` | Refuse to trade with an API key that has no IP allowlist. |
| `MAX_CONSECUTIVE_FAILURES` | `5` | Stop the live loop after this many failed cycles in a row. |
| `RECV_WINDOW_MS` | `5000` | Binance `recvWindow` for signed requests. |
| `FILTERS_CACHE_SECONDS` | `3600` | How long a symbol's exchange filters are cached, instead of re-fetching `exchangeInfo` per order. |
| `MAX_WEIGHT_PER_MINUTE` | `1200` | Request-weight budget; the cycle pauses new entries at 80% of it. |
| `KLINE_INTERVAL` | `1h` | Candle interval the strategy decides on. |
| `MAX_TRADES_PER_HOUR` | `6` | Entries allowed per rolling hour. |
| `CANDLE_MAX_DELAY_SECONDS` | `300` | How late the newest closed candle may be, beyond one interval, before the data is treated as stale. |
| `EVENT_LOG_PATH` | `data/events.jsonl` | Append-only audit log of every live cycle. |

`MAX_DAILY_LOSS_PCT`, `MAX_DRAWDOWN_PCT` and `MAX_NOTIONAL_PCT` must each be greater than 0
and at most 100; a value outside that is rejected at startup rather than silently disabling
the breaker.

## The `live` command

`python3 binance_trading_bot.py live` starts a real trading loop. It only ever starts when you
run it yourself — nothing in this repo schedules or auto-starts it — but once started it keeps
running and trading on its own, evaluating every symbol every `LIVE_POLL_SECONDS` (default 60),
until you stop it with Ctrl+C or `SIGTERM`.

Each cycle, in this order:

1. **Resolve unconfirmed entries.** An order whose response was never seen (a timeout, a
   crash) is looked up by the client order id chosen before it was sent, so the bot learns
   what actually happened instead of guessing.
2. **Book resolved brackets.** A bracket that has filled becomes realized P&L on the
   `RiskGate`, which is what makes the daily-loss and drawdown limits move at all.
3. **Protect anything unprotected.** Every held position must carry an OCO bracket. If one
   cannot be placed, the position is closed at market rather than left without a stop.
4. **Read the real balance.** Sizing and the loss limits are percentages of the account, so
   the account is what they are measured against — never `PAPER_START_BALANCE`.
5. **Consider a new entry, per symbol.** Skip symbols the ledger already holds. Decide on
   closed candles only, and only once they pass the market-data checks below; price from
   the live ticker, size capped by `MAX_NOTIONAL_PCT`,
   quantity rounded to the exchange's lot step, then a market buy followed immediately by
   its OCO bracket.

A `SELL` signal is skipped, never sent. **Binance spot cannot short** — a `SELL` only makes
sense to close a position you already hold, so this bot only ever opens long via `BUY` and
exits via the OCO bracket.

### Market-data checks

Before the strategy sees a candle series, `validate_candles` refuses it — as a `no-trade`
with reason `bad-market-data` — when:

- a price is zero, negative, NaN or infinite, or a volume is negative;
- a bar's high and low do not contain its open and close;
- timestamps do not strictly increase, or a bar is off the `KLINE_INTERVAL` grid;
- a bar is missing among the 60 most recent (an older gap, such as exchange maintenance,
  is tolerated, since rejecting it would stop trading for as long as it stays in view);
- the newest closed bar ended more than one interval plus `CANDLE_MAX_DELAY_SECONDS` ago.
  A feed that stopped updating keeps returning the same old bars, and a signal read from
  them is a signal about the past.

A symbol whose candles cannot be fetched at all is skipped for that cycle without stopping
the others; a fatal error (bad key, banned IP) still stops the loop. `backtest` applies the
per-bar and ordering checks to CSV input too, and exits with an error on a broken file.

### Audit log

Every run appends JSON lines to `EVENT_LOG_PATH` (default `data/events.jsonl`):
`live-started`, one `cycle` per pass with each symbol's action and reason, `cycle-failed`
with the error, and `live-stopped` with why (`stop-signal`, `fatal-error` or
`too-many-failures`). The API key appears only masked. A failed write is logged, never
raised, so the audit log can never be the reason a position is left unmanaged.

### What the bot believes it holds

Open positions live in a persisted **position ledger** (`POSITIONS_PATH`, default
`data/positions.json`), not in the exchange's open orders. A filled market buy leaves no
open order behind, so a bot that asks "are there open orders?" concludes it holds nothing —
and buys the same symbol again on the next cycle. The ledger is the answer to that, and it
survives restarts.

`RiskGate` state (equity, daily P&L, day rollover, trade count and timestamps) is persisted
to `RISK_STATE_PATH` (default `data/risk_state.json`), written atomically, and reloaded on
start. If either file is unreadable the live loop **refuses to start** rather than resuming
with every circuit breaker silently reset to zero.

### Before the first order

`run_live` reads the API key's own restrictions and refuses to trade if the key can
withdraw, or if it has no IP allowlist (`REQUIRE_KEY_IP_RESTRICTION=0` waives the second
knowingly). It then syncs the clock against Binance, because a drifting host clock makes
every signed request fail.

The loop stops rather than grinding on: a fatal error (bad key, bad signature, banned IP)
ends it immediately, and `MAX_CONSECUTIVE_FAILURES` cycles failing in a row ends it too.

```bash
export TRADING_MODE=live
export BINANCE_API_KEY='...'
export BINANCE_API_SECRET='...'
export LIVE_TRADING_CONFIRM=I_UNDERSTAND_RISK
python3 binance_trading_bot.py live
```

## Local bridge for companion projects

`orca_bridge.py` is a dependency-free newline-delimited JSON bridge: read-only commands by
default, and a `live_cycle` that carries the same locks, risk state, ledger, data checks and
audit log as the `live` command. See [integrations/README.md](integrations/README.md).

## BinanceREST: orders and account

`BinanceREST` supports genuine live order management, not just market data:

- `market_order(symbol, side, quantity, client_order_id=None)` — real market buy/sell.
- `place_oco_order(symbol, side, quantity, take_profit_price, stop_price, stop_limit_price)` — real stop-loss + take-profit as one Binance OCO bracket order.
- `cancel_order` / `cancel_oco_order` — cancel a single order or an OCO pair.
- `get_open_orders` / `get_order` / `get_order_by_client_id` — check live order state.

Every call that places, cancels or converts is hard-gated to `Mode.LIVE` — paper mode never touches the real API. Covered by mocked unit tests in `test_bot.py` (no network calls or credentials needed to run the suite).

### Full spot order and account coverage

Scope: everything a spot-trading user can do **except withdrawing or
transferring funds off the account** — those are a fundamentally different risk category
(irreversible, moves money outside the exchange) and are intentionally not implemented.

Also on `BinanceREST`, verified against Binance's official docs:

- `place_limit_order`, `place_stop_loss_limit_order`, `place_take_profit_limit_order` —
  standalone order types alongside the existing `market_order` and OCO bracket
  (`place_oco_order`). All gated to `Mode.LIVE`.
- `cancel_all_open_orders(symbol)` — flatten every open order on a symbol in one call.
- `get_all_orders(symbol)` / `get_my_trades(symbol)` — full order history and actual
  fills/executions (what a user sees under Order History / Trade History).
- `get_exchange_info(symbol)` / `get_symbol_filters(symbol)` — public, unsigned; a
  symbol's real trading rules (lot size step, price tick size, minimum notional).
- `round_to_step(value, step)` — rounds a computed quantity/price down to the exchange's
  actual precision.

The `live` cycle fetches each symbol's filters and rounds the order quantity to the
lot-size step before sending it, and records `no-trade` (reason `below-min-qty` /
`below-min-notional`) rather than firing an order Binance would reject anyway.

### Direct asset conversion

Verified against Binance's official Convert API docs, not memory. `BinanceREST` also
supports converting one asset straight into another — the same "Convert" swap a user does
in the Binance app, no order book involved:

- `get_convert_quote(from_asset, to_asset, from_amount)` — get a firm, time-limited quote.
  Read-only: works in any mode with valid API keys, quoting does not move funds.
- `accept_convert_quote(quote_id)` — execute a previously fetched quote. Gated to `Mode.LIVE`.
- `get_convert_order_status(order_id=..., quote_id=...)` — check a conversion's status.
- `convert(from_asset, to_asset, from_amount, min_to_amount=None)` — quote + accept in one
  call, gated to `Mode.LIVE`. Pass `min_to_amount` for anything unattended: without a floor
  this accepts whatever rate comes back.

This is exposed as a primitive on `BinanceREST`, callable directly (e.g. from a script or a
REPL) exactly like a manual trade. It is **not** wired into the `live` loop's automatic
decisions — `RegimeStrategy` has no logic yet for deciding when to convert between assets,
and inventing that trigger without a stated strategy would be guessing, not engineering.
Wiring it into the autonomous loop is a future phase if/when that strategy is defined.

## سجل الإصلاحات الأولى

- إزالة الاعتماد الإجباري على مكتبات غير مثبتة مثل `ta`, `python-binance`, Redis وPostgreSQL.
- منع التداول الحي افتراضيًا؛ يلزم تصريح صريح `LIVE_TRADING_CONFIRM=I_UNDERSTAND_RISK`.
- إضافة طبقة REST صغيرة موقعة لـ Binance دون تخزين المفاتيح في الكود.
- إضافة استراتيجية هجينة محافظة تعتمد الاتجاه، متوسطات متحركة، ATR، ومرشح تقلب.
- إضافة بوابة مخاطر تشمل نسبة المخاطرة لكل صفقة، حد الخسارة اليومية، أقصى تراجع، وعدد المراكز.
- إضافة Paper Broker واختبار تاريخي CSV ومقاييس أولية.
- جعل الاتصال بـ Binance اختياريًا؛ لا حاجة إلى Redis/PostgreSQL لتشغيل الاختبارات.

## بنية التطوير التالية

يمكن لاحقًا إضافة موصل WebSocket، مخزن SQLite/PostgreSQL، لوحة مراقبة، وإشعارات Telegram، مع إبقاء `RiskGate` كحاجز نهائي لا يمكن للاستراتيجية تجاوزه. لا تضع أسرار API في Git أو ملفات عامة.
