import os
import tempfile
import nobitex_bot as bot

# Never touches Nobitex order endpoints.
assert bot.TEST_MODE is True, "TEST_MODE must remain enabled for the final safe test"

with tempfile.TemporaryDirectory() as d:
    old = bot.STATE_FILE
    bot.STATE_FILE = os.path.join(d, "bot_state.json")
    try:
        state = {"in_position": False, "entry_price": 0, "amount": 0}

        # Simulate a test buy.
        entry = 100_000_000.0
        amount = bot.TRADE_AMOUNT_RLS / entry
        result = bot.place_order("buy", amount, entry)
        assert result["filled"] is True
        state = {
            "in_position": True,
            "entry_price": result["average_price"],
            "amount": result["amount"],
        }
        bot.save_state(state)

        # Verify persistence.
        loaded = bot.load_state()
        assert loaded == state

        # Verify stop-loss threshold.
        assert entry * (1 - bot.STOP_LOSS) == 98_000_000.0

        # Verify take-profit threshold.
        assert entry * (1 + bot.TAKE_PROFIT) == 104_000_000.0

        # Simulate test sells at both thresholds.
        stop = bot.place_order("sell", amount, 98_000_000.0)
        assert stop["filled"] is True

        target = bot.place_order("sell", amount, 104_000_000.0)
        assert target["filled"] is True

        print("FINAL SAFE TEST: PASS")
        print("TEST_MODE: PASS")
        print("BUY simulation: PASS")
        print("STATE persistence: PASS")
        print("STOP_LOSS 2%: PASS")
        print("TAKE_PROFIT 4%: PASS")
        print("No live order endpoint was called.")
    finally:
        bot.STATE_FILE = old
