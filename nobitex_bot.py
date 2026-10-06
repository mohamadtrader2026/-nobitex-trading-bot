import os
import time
import uuid
import requests
from statistics import mean

PUBLIC_API_BASE = "https://apiv2.nobitex.ir"
TRADE_API_BASE = "https://api.nobitex.ir"

SRC_CURRENCY = "btc"
DST_CURRENCY = "rls"
SYMBOL = "BTCIRT"

SHORT_WINDOW = 5
LONG_WINDOW = 20

TRADE_AMOUNT_RLS = 1_000_000
STOP_LOSS = 0.02
TAKE_PROFIT = 0.04
CHECK_SECONDS = 10
ORDER_POLL_SECONDS = 2
ORDER_POLL_ATTEMPTS = 15

# Safe by default. Set NOBITEX_TEST_MODE=false only when you explicitly want live trading.
TEST_MODE = os.getenv("NOBITEX_TEST_MODE", "true").lower() == "true"
API_TOKEN = os.getenv("NOBITEX_API_TOKEN")
STATE_FILE = "bot_state.json"


def get_latest_price():
    url = f"{PUBLIC_API_BASE}/v2/trades/{SYMBOL}"
    response = requests.get(url, timeout=15)
    response.raise_for_status()
    data = response.json()

    if data.get("status") != "ok":
        raise RuntimeError(f"خطا در دریافت قیمت: {data}")

    trades = data.get("trades", [])
    if not trades:
        raise RuntimeError("قیمت دریافت نشد.")

    return float(trades[0]["price"])


def load_state():
    try:
        import json
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
            return {
                "in_position": bool(state.get("in_position", False)),
                "entry_price": float(state.get("entry_price", 0)),
                "amount": float(state.get("amount", 0)),
            }
    except (FileNotFoundError, ValueError, TypeError):
        return {"in_position": False, "entry_price": 0, "amount": 0}


def save_state(state):
    import json
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def get_order_status(order_id):
    url = f"{TRADE_API_BASE}/market/orders/status"
    headers = {"Authorization": f"Token {API_TOKEN}"}
    response = requests.post(
        url,
        headers=headers,
        json={"id": order_id},
        timeout=15,
    )
    response.raise_for_status()
    return response.json()


def place_order(order_type, amount, price):
    if TEST_MODE:
        print(
            f"[TEST MODE] سفارش آزمایشی: "
            f"{order_type.upper()} | مقدار={amount:.8f} | قیمت={price:,.0f}"
        )
        return {
            "status": "test",
            "filled": True,
            "amount": amount,
            "average_price": price,
        }

    if not API_TOKEN:
        raise RuntimeError("NOBITEX_API_TOKEN تنظیم نشده است.")

    url = f"{TRADE_API_BASE}/market/orders/add"
    headers = {
        "Authorization": f"Token {API_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "type": order_type,
        "execution": "market",
        "srcCurrency": SRC_CURRENCY,
        "dstCurrency": DST_CURRENCY,
        "amount": str(amount),
        "clientOrderId": f"bot-{uuid.uuid4().hex[:24]}",
    }

    response = requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=15,
    )
    response.raise_for_status()
    result = response.json()
    print("پاسخ نوبیتکس:", result)

    if result.get("status") != "ok":
        raise RuntimeError(f"ثبت سفارش ناموفق بود: {result}")

    order = result.get("order", {})
    order_id = order.get("id")
    if not order_id:
        raise RuntimeError(f"شناسه سفارش در پاسخ پیدا نشد: {result}")

    for _ in range(ORDER_POLL_ATTEMPTS):
        status = get_order_status(order_id)
        print("وضعیت سفارش:", status)

        order_data = status.get("order", {})
        if order_data.get("status") == "Done":
            filled_amount = float(
                order_data.get("matchedAmount")
                or order_data.get("amount")
                or amount
            )
            average_price = float(
                order_data.get("averagePrice")
                or price
            )
            return {
                "status": "ok",
                "filled": True,
                "amount": filled_amount,
                "average_price": average_price,
            }

        if order_data.get("status") in {"Canceled", "Rejected"}:
            raise RuntimeError(f"سفارش اجرا نشد: {status}")

        time.sleep(ORDER_POLL_SECONDS)

    raise RuntimeError("وضعیت سفارش در زمان مجاز مشخص نشد؛ معامله را انجام‌شده فرض نمی‌کنیم.")


def run_bot():
    prices = []
    state = load_state()

    print("================================")
    print("   NOBITEX TRADING BOT")
    print("================================")
    print("حالت فعلی:", "آزمایشی" if TEST_MODE else "واقعی")
    if TEST_MODE:
        print("هیچ خرید یا فروش واقعی انجام نمی‌شود.")
    else:
        print("هشدار: حالت واقعی فعال است!")
    print("--------------------------------")

    while True:
        try:
            price = get_latest_price()
            prices.append(price)

            if len(prices) > LONG_WINDOW + 1:
                prices.pop(0)

            print(f"قیمت فعلی: {price:,.0f} | تعداد داده: {len(prices)}")

            if len(prices) < LONG_WINDOW + 1:
                time.sleep(CHECK_SECONDS)
                continue

            previous_short = mean(prices[-SHORT_WINDOW - 1:-1])
            previous_long = mean(prices[-LONG_WINDOW - 1:-1])
            short_ma = mean(prices[-SHORT_WINDOW:])
            long_ma = mean(prices[-LONG_WINDOW:])

            crossed_up = previous_short <= previous_long and short_ma > long_ma
            print(f"MA کوتاه: {short_ma:,.0f} | MA بلند: {long_ma:,.0f}")

            if not state["in_position"] and crossed_up:
                amount = TRADE_AMOUNT_RLS / price
                print("🟢 سیگنال خرید")

                result = place_order("buy", amount, price)
                if result.get("filled"):
                    state = {
                        "in_position": True,
                        "entry_price": result["average_price"],
                        "amount": result["amount"],
                    }
                    save_state(state)
                    print(f"ورود در قیمت: {state['entry_price']:,.0f}")

            elif state["in_position"]:
                entry_price = state["entry_price"]
                amount = state["amount"]
                stop_price = entry_price * (1 - STOP_LOSS)
                target_price = entry_price * (1 + TAKE_PROFIT)

                print(
                    f"حد ضرر: {stop_price:,.0f} | "
                    f"حد سود: {target_price:,.0f}"
                )

                if price <= stop_price:
                    print("🔴 حد ضرر فعال شد")
                    result = place_order("sell", amount, price)
                    if result.get("filled"):
                        state = {"in_position": False, "entry_price": 0, "amount": 0}
                        save_state(state)

                elif price >= target_price:
                    print("🟢 حد سود فعال شد")
                    result = place_order("sell", amount, price)
                    if result.get("filled"):
                        state = {"in_position": False, "entry_price": 0, "amount": 0}
                        save_state(state)

            time.sleep(CHECK_SECONDS)

        except KeyboardInterrupt:
            print("ربات متوقف شد.")
            break
        except Exception as e:
            print("⚠️ خطا:", e)
            time.sleep(CHECK_SECONDS)


if __name__ == "__main__":
    run_bot()
