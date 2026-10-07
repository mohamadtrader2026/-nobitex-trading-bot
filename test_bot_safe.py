import os
import tempfile
import nobitex_bot as bot

assert bot.TEST_MODE is True, "TEST_MODE must remain enabled"
assert bot.CANDIDATE_SYMBOLS, "candidate market list is empty"

with tempfile.TemporaryDirectory() as d:
    old = bot.STATE_FILE
    bot.STATE_FILE = os.path.join(d, "bot_state.json")
    try:
        entry = 100_000_000.0
        amount = bot.TRADE_AMOUNT_RLS / entry
        buy = bot.place_order("buy", amount, entry, "BTCIRT")
        assert buy.get("filled") is True
        state = {
            "in_position": True, "symbol": "BTCIRT", "src_currency": "btc",
            "dst_currency": "rls", "entry_price": buy["average_price"],
            "amount": buy["amount"], "history": {}
        }
        bot.save_state(state)
        loaded = bot.load_state()
        assert loaded["in_position"] is True
        assert loaded["symbol"] == "BTCIRT"
        assert loaded["amount"] > 0

        assert abs(entry * (1 - bot.STOP_LOSS) - 98_000_000.0) < 0.01
        assert abs(entry * (1 + bot.TAKE_PROFIT) - 104_000_000.0) < 0.01
        assert bot.place_order("sell", amount, 98_000_000.0, "BTCIRT").get("filled") is True
        assert bot.place_order("sell", amount, 104_000_000.0, "BTCIRT").get("filled") is True

        history = {
            "BTCIRT": [100 + i for i in range(21)],
            "ETHIRT": [100] * 21,
        }
        snapshots = {
            "BTCIRT": {"price": 122, "spread_pct": 0.10, "depth": 20_000_000},
            "ETHIRT": {"price": 100, "spread_pct": 0.10, "depth": 20_000_000},
        }
        best, scored = bot.select_best_market(snapshots, history)
        assert scored, "selector returned no scored markets"
        assert best is not None, "selector correctly refused the weak/flat setup unexpectedly"
        assert best[1] == "BTCIRT", f"unexpected selected market: {best[1]}"

        print("FINAL SAFE TEST: PASS")
        print("TEST_MODE: PASS")
        print("MULTI-MARKET SELECTOR: PASS")
        print("STATE persistence: PASS")
        print("STOP_LOSS/TAKE_PROFIT: PASS")
        print("No live order endpoint was called.")
    finally:
        bot.STATE_FILE = old
