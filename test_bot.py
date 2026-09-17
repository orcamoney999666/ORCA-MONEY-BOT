import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("TRADING_MODE", "paper")
from binance_trading_bot import Candle, Config, RegimeStrategy, RiskGate, Signal, atr, backtest

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

if __name__ == "__main__": unittest.main()
