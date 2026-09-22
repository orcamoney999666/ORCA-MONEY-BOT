# ORCA-MONEY-BOT

نواة احترافية قابلة للتطوير لبوت تداول Binance، مع فصل واضح بين البيانات والاستراتيجية ومحرك المخاطر والتنفيذ. هذه النسخة **لا تنفذ تداولًا حقيقيًا افتراضيًا**؛ الوضع الافتراضي هو `paper`.

## ما تم إصلاحه

- إزالة الاعتماد الإجباري على مكتبات غير مثبتة مثل `ta`, `python-binance`, Redis وPostgreSQL.
- منع التداول الحي افتراضيًا؛ يلزم تصريح صريح `LIVE_TRADING_CONFIRM=I_UNDERSTAND_RISK`.
- إضافة طبقة REST صغيرة موقعة لـ Binance دون تخزين المفاتيح في الكود.
- إضافة استراتيجية هجينة محافظة تعتمد الاتجاه، متوسطات متحركة، ATR، ومرشح تقلب.
- إضافة بوابة مخاطر تشمل نسبة المخاطرة لكل صفقة، حد الخسارة اليومية، أقصى تراجع، وعدد المراكز.
- إضافة Paper Broker واختبار تاريخي CSV ومقاييس أولية.
- جعل الاتصال بـ Binance اختياريًا؛ لا حاجة إلى Redis/PostgreSQL لتشغيل الاختبارات.

## Phase 2 safety hardening

The live path now reads the real quote-asset balance before sizing, caps order notional
with `MAX_NOTIONAL_PCT`, records entries for the hourly limit, persists a position ledger,
and reconciles observed closing fills before removing a position. If the protective OCO
fails after an entry, the bot attempts to cancel remaining orders and flatten the entry
instead of silently carrying an unprotected position. Binance HTTP errors retain their
exchange code/message, credentials are excluded from `Config` repr, risk/position files
are replaced atomically, and symbol filters are cached for one hour.

This does **not** complete Oracle or authorize live trading. The position reconciliation
still depends on observable Binance trade history and must be validated on Testnet before
any real account is considered. The live loop stops after a fatal Binance error or five
consecutive failures rather than retrying indefinitely.

## Phase 3: market-data Oracle

`oracle.py` is a decision-independent validation layer. It rejects empty, stale, future,
non-monotonic, gapped, NaN, negative, or internally inconsistent OHLCV data. The live loop
and guarded bridge use it before strategy evaluation; an Oracle rejection is always a
no-trade decision and is recorded in the audit stream.

## Phase 4: observability and trial operation

`monitoring.py` provides dependency-free JSONL audit events for health and per-symbol
decisions. The event path is `EVENT_LOG_PATH` and defaults to `data/events.jsonl`, which is
ignored by Git. Monitoring records decisions and safety stops without changing strategy
behavior or enabling live trading. External notification providers and WebSocket transport
remain adapters to add only after their contracts are defined and tested; the current
polling path is deliberately retained as the safe baseline.

## Real order execution (added this session)

`BinanceREST` now supports genuine live order management, not just market data:

- `market_order(symbol, side, quantity)` — real market buy/sell (fixed a bug: it was silently sending `GET` instead of `POST`, so it would have failed against the real API).
- `place_oco_order(symbol, side, quantity, take_profit_price, stop_price, stop_limit_price)` — real stop-loss + take-profit as one Binance OCO bracket order.
- `cancel_order` / `cancel_oco_order` — cancel a single order or an OCO pair.
- `get_open_orders` / `get_order` — check live order state.

All mutating calls (`market_order`, `place_oco_order`, `cancel_order`, `cancel_oco_order`) stay hard-gated to `Mode.LIVE`, exactly like before — paper mode never touches the real API. Covered by mocked unit tests in `test_bot.py` (no network calls or credentials needed to run the suite).

## Autonomous live loop: `live` command (added this session)

`python3 binance_trading_bot.py live` starts a real trading loop. It only ever starts when you
run it yourself — nothing in this repo schedules or auto-starts it — but once started it keeps
running and trading on its own, evaluating every symbol every `LIVE_POLL_SECONDS` (default 60),
until you stop it with Ctrl+C or `SIGTERM`.

Each cycle, per symbol:
1. Skip if there is already an open order on that symbol (no duplicate entries).
2. Fetch recent klines and ask `RegimeStrategy` for a signal.
3. A `SELL` signal is skipped, not sent as a real order. **Binance spot cannot short** — a
   `SELL` only makes sense to close a position you already hold, so this bot only ever
   opens long via `BUY` and exits via the OCO bracket below.
4. On an approved `BUY`, it places a real market buy, then immediately places a real OCO
   order (`place_oco_order`) as the stop-loss + take-profit bracket, using the same ATR
   multipliers as backtest/paper mode.

`RiskGate` state (equity, daily P&L, day rollover, trade count/timestamps) is persisted to
`RISK_STATE_PATH` (default `data/risk_state.json`) and reloaded on start, so the daily-loss,
drawdown, and hourly-trade limits stay meaningful across separate runs instead of resetting
every time you start the loop.

**Known gap:** this cycle only limits how many *new* positions can be opened
(`MAX_OPEN_POSITIONS`). It does not yet poll filled orders back to credit/debit
`risk.equity` with the real realized P&L once a bracket fills — that reconciliation is a
separate next step before relying on the daily-loss/drawdown limits for capital already at risk.

```bash
export TRADING_MODE=live
export BINANCE_API_KEY='...'
export BINANCE_API_SECRET='...'
export LIVE_TRADING_CONFIRM=I_UNDERSTAND_RISK
python3 binance_trading_bot.py live
```

## Direct asset conversion (added this session)

Verified against Binance's official Convert API docs, not memory. `BinanceREST` now also
supports converting one asset straight into another — the same "Convert" swap a user does
in the Binance app, no order book involved:

- `get_convert_quote(from_asset, to_asset, from_amount)` — get a firm, time-limited quote.
  Read-only: works in any mode with valid API keys, quoting does not move funds.
- `accept_convert_quote(quote_id)` — execute a previously fetched quote. Gated to `Mode.LIVE`.
- `get_convert_order_status(order_id=..., quote_id=...)` — check a conversion's status.
- `convert(from_asset, to_asset, from_amount)` — quote + accept in one call, gated to `Mode.LIVE`.

This is exposed as a primitive on `BinanceREST`, callable directly (e.g. from a script or a
REPL) exactly like a manual trade. It is **not** wired into the `live` loop's automatic
decisions — `RegimeStrategy` has no logic yet for deciding when to convert between assets,
and inventing that trigger without a stated strategy would be guessing, not engineering.
Wiring it into the autonomous loop is a future phase if/when that strategy is defined.

## Full order/account parity (added this session)

Scope for this pass: everything a spot-trading user can do **except withdrawing or
transferring funds off the account** — those are a fundamentally different risk category
(irreversible, moves money outside the exchange) and are intentionally not implemented.

New on `BinanceREST`, verified against Binance's official docs:

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

**Wired into `live`:** each cycle now fetches the symbol's filters and rounds the order
quantity to the lot-size step before sending it, and rejects (`no-trade`, reason
`below-min-qty` / `below-min-notional`) rather than firing an order Binance would reject
anyway. Before this, a computed quantity that violated `LOT_SIZE`/`NOTIONAL` would have
been sent as-is and failed at Binance's end.

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

**التداول الحي غير موصى به قبل اختبار طويل ومراجعة مستقلة.** لا تستخدم مفاتيح بصلاحية السحب، وفَعّل IP allowlist، وابدأ بصلاحية قراءة/تداول فقط. لا يوجد في هذه النسخة نظام أخبار أو تحليل سياسي/اجتماعي آلي؛ إدخال هذه المصادر يحتاج مزود بيانات موثوق، تعريفًا زمنيًا واضحًا، واختبارات تمنع تحويل الأخبار إلى قرارات غير قابلة للتدقيق.

## بنية التطوير التالية

يمكن لاحقًا إضافة موصل WebSocket، مخزن SQLite/PostgreSQL، لوحة مراقبة، وإشعارات Telegram، مع إبقاء `RiskGate` كحاجز نهائي لا يمكن للاستراتيجية تجاوزه. لا تضع أسرار API في Git أو ملفات عامة.
