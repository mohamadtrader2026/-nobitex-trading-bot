import os
import tempfile
import nobitex_bot as bot

assert bot.TEST_MODE is True, "TEST_MODE must remain enabled for the final safe test"
assert "BTCIRT" in bot.CANDIDATE_SYMBOLS

with tempfile.TemporaryDirectory() as d:
    old = bot.STATE_FILE
    bot.STATE_FILE = os.path.join(d, "bot_state.json")
    try:
        state = {"in_position": False, "symbol": "BTCIRT", "src_currency": "btc", "dst_currency": "rls", "entry_price": 0, "amount": 0, "history": {}}
        entry = 100_000_000.0
        amount = bot.TRADE_AMOUNT_RLS / entry

        result = bot.place_order("buy", amount, entry, "BTCIRT")
        assert result["filled"] is True
        state.update({"in_position": True, "entry_price": result["average_price"], "amount": result["amount"]})
        bot.save_state(state)
        assert bot.load_state() == state

        assert entry * (1 - bot.STOP_LOSS) == 98_000_000.0
        assert entry * (1 + bot.TAKE_PROFIT) == 104_000_000.0
        assert bot.place_order("sell", amount, 98_000_000.0, "BTCIRT")["filled"] is True
        assert bot.place_order("sell", amount, 104_000_000.0, "BTCIRT")["filled"] is True

        # Smart selector: positive trend/momentum should beat a flat candidate.
        history = {
            "BTCIRT": [100, 100.5, 101, 101.5, 102, 103, 104, 105, 106, 107, 108, 109, 110, 111, 112, 113, 114, 115, 116, 117, 118],
            "ETHIRT": [100] * 21,
        }
        snapshots = {
            "BTCIRT": {"price": 119, "spread_pct": 0.10, "depth": 20_000_000},
            "ETHIRT": {"price": 100, "spread_pct": 0.10, "depth": 20_000_000},
        }
        best, scored = bot.select_best_market(snapshots, history)
        assert best is not None
        assert best[1] == "BTCIRT"

        print("FINAL SAFE TEST: PASS")
        print("TEST_MODE: PASS")
        print("MULTI-MARKET SELECTOR: PASS")
        print("BUY/SELL simulation: PASS")
        print("STATE persistence: PASS")
        print("STOP_LOSS 2%: PASS")
        print("TAKE_PROFIT 4%: PASS")
        print("No live order endpoint was called.")
    finally:
        bot.STATE_FILE = old
