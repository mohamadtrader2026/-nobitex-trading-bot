import os
import tempfile
import importlib.util
from pathlib import Path

spec = importlib.util.spec_from_file_location("nobitex_bot_test_module", Path("nobitex_bot.py"))
bot = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bot)

assert bot.TEST_MODE is True
assert bot.CHECK_SECONDS <= 2.0
assert bot.ORDER_POLL_SECONDS <= 0.5
assert bot.CANDIDATE_SYMBOLS

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
        assert abs(entry * (1 - bot.STOP_LOSS) - 98_000_000) < 1
        assert abs(entry * (1 + bot.TAKE_PROFIT) - 104_000_000) < 1
        assert bot.place_order("sell", amount, 98_000_000, "BTCIRT")["filled"] is True
        history = {"BTCIRT": [100+i for i in range(21)], "ETHIRT": [100]*21}
        snapshots = {"BTCIRT": {"price": 122, "bid": 121, "ask": 123, "spread_pct": .10, "depth": 20_000_000}, "ETHIRT": {"price": 100, "bid": 99, "ask": 101, "spread_pct": .10, "depth": 20_000_000}}
        best, scored = bot.select_best_market(snapshots, history)
        assert best is not None and best[1] == "BTCIRT"
        print("FINAL SAFE TEST: PASS")
        print("FAST CHECK/POLL: PASS")
        print("SMART SELECTOR: PASS")
        print("TEST_MODE: PASS")
        print("NO LIVE ORDER: PASS")
    finally:
        bot.STATE_FILE = old
