import tempfile
import unittest
from unittest.mock import Mock
from pathlib import Path

import paper_long_short as bot


class PaperLongShortTests(unittest.TestCase):
    def test_signal_long_short_and_flat(self):
        rising = [float(i) for i in range(1, 31)]
        falling = [float(i) for i in range(30, 0, -1)]
        flat = [100.0] * 30
        self.assertEqual(bot.signal(rising, fast=5, slow=20), "LONG")
        self.assertEqual(bot.signal(falling, fast=5, slow=20), "SHORT")
        self.assertEqual(bot.signal(flat, fast=5, slow=20), "FLAT")
        self.assertEqual(bot.signal([1.0, 2.0], fast=5, slow=20), "FLAT")

    def test_directional_pnl_is_symmetric(self):
        long_pos = {"side": "LONG", "entry": 100.0}
        short_pos = {"side": "SHORT", "entry": 100.0}
        self.assertAlmostEqual(bot.pnl_pct(long_pos, 110.0), 0.10)
        self.assertAlmostEqual(bot.pnl_pct(long_pos, 90.0), -0.10)
        self.assertAlmostEqual(bot.pnl_pct(short_pos, 90.0), 0.10)
        self.assertAlmostEqual(bot.pnl_pct(short_pos, 110.0), -0.10)

    def test_bad_price_is_rejected(self):
        with self.assertRaises(ValueError):
            bot.pnl_pct({"side": "LONG", "entry": 100.0}, 0)

    def test_close_position_accounts_for_fees(self):
        state = {
            "position": {"symbol": "BTCIRT", "side": "LONG", "entry": 100.0, "notional": 1000.0},
            "realized_pnl": 0.0,
            "trades": 0,
        }
        net = bot.close_position(state, 110.0, "test", fee_rate=0.001)
        self.assertAlmostEqual(net, 100.0 - 1.0 - 1.1)
        self.assertIsNone(state["position"])
        self.assertEqual(state["trades"], 1)

    def test_fetch_prices_skips_wide_spreads(self):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "status": "ok",
            "BTCIRT": {
                "lastTradePrice": "100",
                "bids": [["99", "10"]],
                "asks": [["101", "10"]],
            },
            "ETHIRT": {
                "lastTradePrice": "100",
                "bids": [["99.95", "10"]],
                "asks": [["100.05", "10"]],
            },
        }
        session = Mock()
        session.get.return_value = response

        prices = bot.fetch_prices(session=session)

        self.assertNotIn("BTCIRT", prices)
        self.assertIn("ETHIRT", prices)
        session.get.assert_called_once_with(bot.API_URL, timeout=bot.HTTP_TIMEOUT_SECONDS)

    def test_invalid_quote_does_not_open_position(self):
        state = {"history": {}, "position": None, "realized_pnl": 0.0, "trades": 0}
        bot.process_snapshot(state, {"BTCIRT": {"bid": 101.0, "ask": 100.0, "mid": 100.5}})
        self.assertIsNone(state["position"])

    def test_state_round_trip_and_corrupt_state_recovery(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            original = {
                "history": {"BTCIRT": [100, 101, "bad"]},
                "position": {"symbol": "BTCIRT", "side": "LONG", "entry": 100, "notional": 1000},
                "realized_pnl": 12.5,
                "trades": 3,
            }
            bot.save_state(original, path)
            loaded = bot.load_state(path)
            self.assertEqual(loaded["history"]["BTCIRT"], [100.0, 101.0])
            self.assertEqual(loaded["trades"], 3)
            path.write_text("{broken", encoding="utf-8")
            recovered = bot.load_state(path)
            self.assertIsNone(recovered["position"])
            self.assertEqual(recovered["history"], {})


if __name__ == "__main__":
    unittest.main()
