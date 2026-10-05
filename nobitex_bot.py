import os
import time
import requests
from statistics import mean

# =========================
# تنظیمات ربات
# =========================

API_BASE = "https://api.nobitex.ir"

# بازار: BTC به ریال
SRC_CURRENCY = "btc"
DST_CURRENCY = "rls"
SYMBOL = "BTCIRT"

# تعداد قیمت برای میانگین متحرک
SHORT_WINDOW = 5
LONG_WINDOW = 20

# مدیریت سرمایه
TRADE_AMOUNT_RLS = 1_000_000

# حد ضرر و حد سود
STOP_LOSS = 0.02       # 2 درصد
TAKE_PROFIT = 0.04     # 4 درصد

# فاصله بررسی بازار
CHECK_SECONDS = 10

# خیلی مهم:
# True = فقط آزمایشی، هیچ سفارشی ثبت نمی‌شود
# False = امکان ثبت سفارش واقعی
TEST_MODE = True

# توکن از متغیر محیطی خوانده می‌شود
API_TOKEN = os.getenv("NOBITEX_API_TOKEN")


# =========================
# دریافت قیمت
# =========================

def get_latest_price():
    url = f"{API_BASE}/v2/trades/{SYMBOL}"

    response = requests.get(url, timeout=15)
    response.raise_for_status()

    data = response.json()

    if data.get("status") != "ok":
        raise RuntimeError(f"خطا در دریافت قیمت: {data}")

    trades = data.get("trades", [])

    if not trades:
        raise RuntimeError("قیمت دریافت نشد.")

    return float(trades[0]["price"])


# =========================
# ثبت سفارش
# =========================

def place_order(order_type, amount, price):
    if TEST_MODE:
        print(
            f"[TEST MODE] سفارش آزمایشی: "
            f"{order_type.upper()} | مقدار={amount} | قیمت={price}"
        )
        return {"status": "test"}

    if not API_TOKEN:
        raise RuntimeError(
            "NOBITEX_API_TOKEN تنظیم نشده است."
        )

    url = f"{API_BASE}/market/orders/add"

    headers = {
        "Authorization": f"Token {API_TOKEN}",
        "Content-Type": "application/json",
    }

    payload = {
        "type": order_type,
        "srcCurrency": SRC_CURRENCY,
        "dstCurrency": DST_CURRENCY,
        "amount": str(amount),
        "price": price,
    }

    response = requests.post(
        url,
        headers=headers,
        json=payload,
        timeout=15
    )

    print("پاسخ نوبیتکس:", response.text)

    response.raise_for_status()

    return response.json()


# =========================
# ربات
# =========================

def run_bot():

    prices = []

    in_position = False
    entry_price = 0

    print("================================")
    print("   NOBITEX TRADING BOT")
    print("================================")

    if TEST_MODE:
        print("حالت فعلی: آزمایشی")
        print("هیچ خرید یا فروش واقعی انجام نمی‌شود.")
    else:
        print("هشدار: حالت واقعی فعال است!")

    print("--------------------------------")

    while True:

        try:

            price = get_latest_price()

            prices.append(price)

            if len(prices) > LONG_WINDOW:
                prices.pop(0)

            print(
                f"قیمت فعلی: {price:,.0f} | "
                f"تعداد داده: {len(prices)}"
            )

            # هنوز داده کافی نداریم
            if len(prices) < LONG_WINDOW:
                time.sleep(CHECK_SECONDS)
                continue

            short_ma = mean(prices[-SHORT_WINDOW:])
            long_ma = mean(prices[-LONG_WINDOW:])

            print(
                f"MA کوتاه: {short_ma:,.0f} | "
                f"MA بلند: {long_ma:,.0f}"
            )

            # =========================
            # سیگنال خرید
            # =========================

            if not in_position and short_ma > long_ma:

                amount = TRADE_AMOUNT_RLS / price

                print("🟢 سیگنال خرید")

                result = place_order(
                    "buy",
                    amount,
                    price
                )

                if result.get("status") in ["ok", "test"]:
                    in_position = True
                    entry_price = price

                    print(
                        f"ورود در قیمت: "
                        f"{entry_price:,.0f}"
                    )

            # =========================
            # مدیریت معامله
            # =========================

            elif in_position:

                stop_price = entry_price * (1 - STOP_LOSS)
                target_price = entry_price * (1 + TAKE_PROFIT)

                print(
                    f"حد ضرر: {stop_price:,.0f} | "
                    f"حد سود: {target_price:,.0f}"
                )

                # حد ضرر
                if price <= stop_price:

                    print("🔴 حد ضرر فعال شد")

                    amount = TRADE_AMOUNT_RLS / entry_price

                    result = place_order(
                        "sell",
                        amount,
                        price
                    )

                    if result.get("status") in ["ok", "test"]:
                        in_position = False
                        entry_price = 0

                # حد سود
                elif price >= target_price:

                    print("🟢 حد سود فعال شد")

                    amount = TRADE_AMOUNT_RLS / entry_price

                    result = place_order(
                        "sell",
                        amount,
                        price
                    )

                    if result.get("status") in ["ok", "test"]:
                        in_position = False
                        entry_price = 0

            time.sleep(CHECK_SECONDS)

        except KeyboardInterrupt:

            print("ربات متوقف شد.")
            break

        except Exception as e:

            print("⚠️ خطا:", e)
            time.sleep(CHECK_SECONDS)


if __name__ == "__main__":
    run_bot()
