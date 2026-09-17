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
