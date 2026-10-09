import csv
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import backtest


class BacktestTests(unittest.TestCase):
    def test_load_snapshots_and_compute_spread(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=["timestamp", "symbol", "price", "bid", "ask", "depth"])
                writer.writeheader()
                writer.writerow({"timestamp": "2026-01-01T00:00:00Z", "symbol": "BTCIRT", "price": 100, "bid": 99, "ask": 101, "depth": 1000})
            rows = backtest.load_snapshots(path)
            self.assertEqual(len(rows), 1)
            self.assertAlmostEqual(rows[0][1]["BTCIRT"]["spread_pct"], 2.0)

    def test_take_profit_trade_subtracts_fees(self):
        rows = [
            ("2026-01-01T00:00:00Z", {"BTCIRT": {"price": 100, "bid": 99.5, "ask": 101, "depth": 10000, "spread_pct": 0.1}}),
            ("2026-01-01T00:01:00Z", {"BTCIRT": {"price": 110, "bid": 109, "ask": 111, "depth": 10000, "spread_pct": 0.1}}),
        ]
        candidate = (1.0, "BTCIRT", rows[0][1]["BTCIRT"], 0.01, 0.02, 1.0, 0.75, 0.1, True)
        with patch.object(backtest.bot, "select_best_market", return_value=(candidate, [candidate])):
            report = backtest.run_backtest(rows, fee_pct=0.1, trade_amount=1000)
        self.assertEqual(report["closed_trades"], 1)
        self.assertEqual(report["wins"], 1)
        self.assertEqual(report["trades"][0]["exit_reason"], "take_profit")
        self.assertGreater(report["fees_rials"], 0)
        self.assertAlmostEqual(
            report["net_pnl_rials"],
            report["gross_pnl_rials"] - report["fees_rials"],
            places=2,
        )

    def test_invalid_csv_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.csv"
            path.write_text("timestamp,symbol,price\n2026-01-01T00:00:00Z,BTCIRT,100\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                backtest.load_snapshots(path)


if __name__ == "__main__":
    unittest.main()
