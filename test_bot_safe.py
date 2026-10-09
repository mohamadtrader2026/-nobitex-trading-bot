import os
import tempfile
import importlib.util
import requests
from unittest.mock import patch
from pathlib import Path

spec = importlib.util.spec_from_file_location("nobitex_bot_test_module", Path("nobitex_bot.py"))
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)

assert bot.TEST_MODE is True
assert bot.CHECK_SECONDS <= 2.0
assert bot.ORDER_POLL_SECONDS <= 0.5
assert bot.CANDIDATE_SYMBOLS
assert abs(bot.MAX_SPREAD_PCT - 0.25) < 1e-9
assert abs(bot.MIN_BOOK_IMBALANCE - 0.40) < 1e-9
assert abs(bot.FEE_PCT - 0.1) < 1e-9
assert abs(bot.MAX_VOLATILITY_PCT - 0.25) < 1e-9
assert abs(bot.MIN_SCORE - 0.18) < 1e-9
assert abs(bot.TAKE_PROFIT - 0.05) < 1e-9
assert abs(bot.STOP_LOSS - 0.01) < 1e-9
assert abs(bot.DAILY_LOSS_LIMIT_RLS - 200_000) < 1e-9
assert abs(bot.daily_loss_limit_rials() - 200_000) < 1e-9
unknown_order = bot.OrderStatusUnknown("12345", "buy", "BTCIRT")
assert unknown_order.order_id == "12345"
assert unknown_order.order_type == "buy"
assert unknown_order.symbol == "BTCIRT"

# Simulate an accepted order whose status request times out. This must fail closed
# and must not submit a second order. No network call is made.
class FakeResponse:
    def raise_for_status(self):
        pass
    def json(self):
        return {"status": "ok", "order": {"id": 12345}}

old_test_mode, old_token = bot.TEST_MODE, bot.API_TOKEN
try:
    bot.TEST_MODE = False
    bot.API_TOKEN = "test-token"
    with patch.object(bot.SESSION, "post", side_effect=[FakeResponse(), requests.Timeout("simulated timeout")]) as mocked_post:
        try:
            bot.place_order("buy", 0.01, 100_000_000, "BTCIRT")
            raise AssertionError("OrderStatusUnknown was expected")
        except bot.OrderStatusUnknown as exc:
            assert exc.order_id == "12345"
            assert exc.order_type == "buy"
            assert exc.symbol == "BTCIRT"
        assert mocked_post.call_count == 2  # one order submission + one status poll
finally:
    bot.TEST_MODE, bot.API_TOKEN = old_test_mode, old_token

with tempfile.TemporaryDirectory() as d:
    old = bot.STATE_FILE
    bot.STATE_FILE = os.path.join(d, "bot_state.json")
    try:
        entry = 100_000_000.0
        amount = bot.TRADE_AMOUNT_RLS / entry
        buy = bot.place_order("buy", amount, entry, "BTCIRT")
        assert buy["filled"] is True
        state = {"in_position": True, "symbol": "BTCIRT", "entry_price": buy["average_price"], "amount": buy["amount"], "history": {}}
        bot.save_state(state)
        loaded = bot.load_state()
        assert loaded["symbol"] == "BTCIRT"
        assert loaded["in_position"] is True
        assert abs(entry * (1 - bot.STOP_LOSS) - 99_000_000) < 1
        assert abs(entry * (1 + bot.TAKE_PROFIT) - 105_000_000) < 1
        assert bot.place_order("sell", amount, 99_000_000, "BTCIRT")["filled"] is True
        loss_state = {"in_position": True, "entry_price": 100_000_000, "amount": 0.01, "last_exit_at": 0, "daily_loss_date": bot.datetime.now(bot.timezone.utc).date().isoformat(), "daily_loss_rials": 0}
        bot.finalize_exit(loss_state, 97_000_000)
        assert abs(loss_state["daily_loss_rials"] - 31_970) < 1
        assert loss_state["in_position"] is False
        assert loss_state["daily_loss_rials"] < bot.daily_loss_limit_rials()
        history = {"BTCIRT": [100+i for i in range(21)], "ETHIRT": [100]*21}
        snapshots = {"BTCIRT": {"price": 122, "bid": 121, "ask": 123, "spread_pct": .10, "depth": 20_000_000}, "ETHIRT": {"price": 100, "bid": 99, "ask": 101, "spread_pct": .10, "depth": 20_000_000}}
        best, scored = bot.select_best_market(snapshots, history)
        assert best is not None and best[1] == "BTCIRT" and best[8] is True

        # A tighter spread market should beat a high-spread market.
        good_history = {"BTCIRT": [100 + i for i in range(21)], "ETHIRT": [100 + i for i in range(21)]}
        good_snapshots = {
            "BTCIRT": {"price": 122, "bid": 121, "ask": 123, "spread_pct": .90, "depth": 20_000_000},
            "ETHIRT": {"price": 122, "bid": 121.8, "ask": 122.2, "spread_pct": .10, "depth": 20_000_000},
        }
        good_best, _ = bot.select_best_market(good_snapshots, good_history)
        assert good_best is not None and good_best[1] == "ETHIRT"

        # Ask-heavy order books are rejected even when price history trends upward.
        ask_heavy_history = {"BTCIRT": [100 + i for i in range(21)]}
        ask_heavy = {"BTCIRT": {"price": 122, "bid": 121.8, "ask": 122.2, "spread_pct": .10, "depth": 20_000_000, "book_imbalance": .20}}
        no_ask_heavy, _ = bot.select_best_market(ask_heavy, ask_heavy_history)
        assert no_ask_heavy is None

        # Flat markets must not trigger an entry.
        flat_history = {"BTCIRT": [100] * 21}
        flat_snapshots = {"BTCIRT": {"price": 100, "bid": 99.9, "ask": 100.1, "spread_pct": .20, "depth": 20_000_000}}
        no_trade, _ = bot.select_best_market(flat_snapshots, flat_history)
        assert no_trade is None
        print("FINAL SAFE TEST: PASS")
        print("FAST CHECK/POLL: PASS")
        print("SMART SELECTOR: PASS")
        print("SPREAD / MOMENTUM / CONSISTENCY / VOLATILITY FILTERS: PASS")
        print("TAKE PROFIT 5% / STOP LOSS 1%: PASS")
        print("DAILY LOSS CAP 200,000 RIALS/DAY: PASS")
        print("TEST_MODE: PASS")
        print("NO LIVE ORDER: PASS")
    finally:
        bot.STATE_FILE = old
