import os
import time
import uuid
import requests
from statistics import mean
from datetime import datetime, timezone

API_ENV = os.getenv("NOBITEX_API_ENV", "testnet").lower()

if API_ENV == "testnet":
    PUBLIC_API_BASE = "https://testnetapi.nobitex.ir"
    TRADE_API_BASE = "https://testnetapi.nobitex.ir"
elif API_ENV == "mainnet":
    PUBLIC_API_BASE = "https://apiv2.nobitex.ir"
    TRADE_API_BASE = "https://api.nobitex.ir"
else:
    raise RuntimeError("NOBITEX_API_ENV باید testnet یا mainnet باشد.")

# Smart selector v2: scan liquid IRR markets on every cycle.
# The selector scans liquid IRR markets. The list is only a preference list;
# markets missing from Nobitex's public orderbook are skipped automatically.
CANDIDATE_SYMBOLS = [
    "BTCIRT", "ETHIRT", "USDTIRT", "DOGEIRT", "XRPIRT", "LTCIRT",
    "ADAIRT", "SOLIRT", "TRXIRT", "LINKIRT", "XLMIRT",
]
DEFAULT_SYMBOL = "BTCIRT"
QUOTE_CURRENCY = "rls"

SHORT_WINDOW = 5
LONG_WINDOW = 20
HISTORY_SIZE = LONG_WINDOW + 1

TRADE_AMOUNT_RLS = 1_000_000
STOP_LOSS = 0.02
TAKE_PROFIT = 0.04
CHECK_SECONDS = 10
ORDER_POLL_SECONDS = 2
ORDER_POLL_ATTEMPTS = 15
MAX_RUNTIME_SECONDS = int(os.getenv("NOBITEX_MAX_RUNTIME_SECONDS", "0"))
MIN_SCORE = float(os.getenv("NOBITEX_MIN_SCORE", "0.15"))
MAX_SPREAD_PCT = float(os.getenv("NOBITEX_MAX_SPREAD_PCT", "0.80"))

TEST_MODE = os.getenv("NOBITEX_TEST_MODE", "true").lower() == "true"
API_TOKEN = os.getenv("NOBITEX_API_TOKEN")
STATE_FILE = "bot_state.json"

ALLOW_PUBLIC_MARKET_FALLBACK = (
    API_ENV == "testnet"
    and TEST_MODE
    and os.getenv("NOBITEX_PUBLIC_MARKET_FALLBACK", "true").lower() == "true"
)
PUBLIC_MARKET_FALLBACK_BASE = "https://apiv2.nobitex.ir"


def log(message):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{now}] {message}", flush=True)


def fetch_market_snapshot():
    """Fetch all public orderbooks once and return candidate market snapshots."""
    urls = [f"{PUBLIC_API_BASE}/v3/orderbook/all"]
    if ALLOW_PUBLIC_MARKET_FALLBACK:
        urls.append(f"{PUBLIC_MARKET_FALLBACK_BASE}/v3/orderbook/all")

    last_error = None
    for index, url in enumerate(urls):
        try:
            response = requests.get(url, timeout=15)
            response.raise_for_status()
            data = response.json()
            if data.get("status") != "ok":
                raise RuntimeError(f"پاسخ بازار نامعتبر است: {data}")

            snapshots = {}
            for symbol in CANDIDATE_SYMBOLS:
                book = data.get(symbol)
                if not isinstance(book, dict) or not book.get("lastTradePrice"):
                    continue
                try:
                    price = float(book["lastTradePrice"])
                    bids = book.get("bids") or []
                    asks = book.get("asks") or []
                    if not bids or not asks:
                        continue
                    best_bid = float(bids[0][0])
                    best_ask = float(asks[0][0])
                    spread_pct = max(0.0, (best_ask - best_bid) / price * 100)
                    depth = sum(float(x[0]) * float(x[1]) for x in bids[:5]) + sum(
                        float(x[0]) * float(x[1]) for x in asks[:5]
                    )
                    snapshots[symbol] = {
                        "price": price,
                        "spread_pct": spread_pct,
                        "depth": depth,
                    }
                except (TypeError, ValueError, IndexError):
                    continue

            if index == 1:
                log("⚠️ Testnet در دسترس نیست؛ داده عمومی بازار اصلی فقط برای TEST_MODE استفاده شد.")
            return snapshots
        except Exception as exc:
            last_error = exc

    raise RuntimeError(f"دریافت داده بازار ناموفق بود: {last_error}")


def select_best_market(snapshots, history):
    """Score candidates using trend, momentum, liquidity and spread."""
    scored = []
    for symbol, snap in snapshots.items():
        prices = history.setdefault(symbol, [])
        prices.append(snap["price"])
        if len(prices) > HISTORY_SIZE:
            del prices[:-HISTORY_SIZE]

        if len(prices) < LONG_WINDOW + 1:
            continue

        short_ma = mean(prices[-SHORT_WINDOW:])
        long_ma = mean(prices[-LONG_WINDOW:])
        momentum = (prices[-1] / prices[-SHORT_WINDOW] - 1) if prices[-SHORT_WINDOW] else 0
        trend = (short_ma / long_ma - 1) if long_ma else 0
        spread_penalty = snap["spread_pct"] / MAX_SPREAD_PCT if MAX_SPREAD_PCT else 1
        liquidity = min(1.0, snap["depth"] / max(TRADE_AMOUNT_RLS * 5, 1))

        # Positive trend/momentum are rewarded; wide spread is penalized.
        score = (trend * 4.0) + (momentum * 3.0) + (liquidity * 0.25) - (spread_penalty * 0.25)
        scored.append((score, symbol, snap, trend, momentum, liquidity))

    scored.sort(reverse=True, key=lambda x: x[0])
    if not scored:
        return None, scored

    best = scored[0]
    if best[0] < MIN_SCORE or best[3] <= 0 or best[4] <= 0 or best[2]["spread_pct"] > MAX_SPREAD_PCT:
        return None, scored
    return best, scored


def load_state():
    try:
        import json
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
            return {
                "in_position": bool(state.get("in_position", False)),
                "symbol": str(state.get("symbol", DEFAULT_SYMBOL)),
                "src_currency": str(state.get("src_currency", DEFAULT_SYMBOL[:-3].lower())),
                "dst_currency": str(state.get("dst_currency", QUOTE_CURRENCY)),
                "entry_price": float(state.get("entry_price", 0)),
                "amount": float(state.get("amount", 0)),
                "history": state.get("history", {}),
            }
    except (FileNotFoundError, ValueError, TypeError):
        return {"in_position": False, "symbol": DEFAULT_SYMBOL, "src_currency": "btc", "dst_currency": QUOTE_CURRENCY, "entry_price": 0, "amount": 0, "history": {}}


def save_state(state):
    import json
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def get_order_status(order_id):
    url = f"{TRADE_API_BASE}/market/orders/status"
    headers = {"Authorization": f"Token {API_TOKEN}"}
    response = requests.post(url, headers=headers, json={"id": order_id}, timeout=15)
    response.raise_for_status()
    return response.json()


def place_order(order_type, amount, price, symbol=DEFAULT_SYMBOL):
    src_currency = symbol[:-3].lower() if symbol.endswith("IRT") else symbol[:-4].lower()
    dst_currency = "rls" if symbol.endswith("IRT") else "usdt"
    if TEST_MODE:
        log(f"🧪 [TEST MODE] {order_type.upper()} {symbol} | مقدار={amount:.8f} | قیمت={price:,.0f}")
        return {"status": "test", "filled": True, "amount": amount, "average_price": price}

    if not API_TOKEN:
        raise RuntimeError("NOBITEX_API_TOKEN تنظیم نشده است.")

    url = f"{TRADE_API_BASE}/market/orders/add"
    headers = {"Authorization": f"Token {API_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "type": order_type, "execution": "market", "srcCurrency": src_currency,
        "dstCurrency": dst_currency, "amount": str(amount),
        "clientOrderId": f"bot-{uuid.uuid4().hex[:24]}",
    }
    response = requests.post(url, headers=headers, json=payload, timeout=15)
    response.raise_for_status()
    result = response.json()
    log(f"پاسخ نوبیتکس: {result}")
    if result.get("status") != "ok":
        raise RuntimeError(f"ثبت سفارش ناموفق بود: {result}")
    order_id = result.get("order", {}).get("id")
    if not order_id:
        raise RuntimeError(f"شناسه سفارش در پاسخ پیدا نشد: {result}")

    for _ in range(ORDER_POLL_ATTEMPTS):
        status = get_order_status(order_id)
        order_data = status.get("order", {})
        log(f"وضعیت سفارش: {status}")
        if order_data.get("status") == "Done":
            return {"status": "ok", "filled": True,
                    "amount": float(order_data.get("matchedAmount") or order_data.get("amount") or amount),
                    "average_price": float(order_data.get("averagePrice") or price)}
        if order_data.get("status") in {"Canceled", "Rejected"}:
            raise RuntimeError(f"سفارش اجرا نشد: {status}")
        time.sleep(ORDER_POLL_SECONDS)
    raise RuntimeError("وضعیت سفارش در زمان مجاز مشخص نشد؛ معامله را انجام‌شده فرض نمی‌کنیم.")


def run_bot():
    state = load_state()
    started_at = time.monotonic()
    log("================================")
    log("   NOBITEX SMART MARKET BOT")
    log("================================")
    log(f"محیط API: {API_ENV} | حالت: {'آزمایشی' if TEST_MODE else 'واقعی'}")
    log(f"بازارهای قابل بررسی: {', '.join(CANDIDATE_SYMBOLS)}")
    log("🛡️ در TEST_MODE هیچ سفارش واقعی ثبت نمی‌شود.")

    while True:
        if MAX_RUNTIME_SECONDS > 0 and time.monotonic() - started_at >= MAX_RUNTIME_SECONDS:
            save_state(state)
            log("⏹️ زمان این نوبت تمام شد؛ وضعیت ذخیره شد.")
            break
        try:
            snapshots = fetch_market_snapshot()
            if not snapshots:
                log("⚠️ هیچ بازار قابل بررسی پیدا نشد.")
                time.sleep(CHECK_SECONDS)
                continue

            if state["in_position"]:
                symbol = state["symbol"]
                snap = snapshots.get(symbol)
                if not snap:
                    log(f"⚠️ بازار موقعیت {symbol} فعلاً داده ندارد؛ خروج اجباری انجام نمی‌شود.")
                    time.sleep(CHECK_SECONDS)
                    continue
                price = snap["price"]
                entry_price = state["entry_price"]
                amount = state["amount"]
                stop_price = entry_price * (1 - STOP_LOSS)
                target_price = entry_price * (1 + TAKE_PROFIT)
                log(f"📌 موقعیت {symbol} | قیمت={price:,.0f} | ورود={entry_price:,.0f} | SL={stop_price:,.0f} | TP={target_price:,.0f}")
                if price <= stop_price:
                    log("🔴 حد ضرر فعال شد.")
                    result = place_order("sell", amount, price, symbol)
                    if result.get("filled"):
                        state.update({"in_position": False, "entry_price": 0, "amount": 0})
                        save_state(state)
                elif price >= target_price:
                    log("🟢 حد سود فعال شد.")
                    result = place_order("sell", amount, price, symbol)
                    if result.get("filled"):
                        state.update({"in_position": False, "entry_price": 0, "amount": 0})
                        save_state(state)
            else:
                best, scored = select_best_market(snapshots, state["history"])
                for item in scored[:5]:
                    log(f"🔎 {item[1]} score={item[0]:.4f} trend={item[3]*100:.2f}% momentum={item[4]*100:.2f}% spread={item[2]['spread_pct']:.2f}%")
                if best:
                    score, symbol, snap, trend, momentum, liquidity = best
                    amount = TRADE_AMOUNT_RLS / snap["price"]
                    log(f"🏆 بهترین بازار فعلی: {symbol} | score={score:.4f} | روند={trend*100:.2f}% | مومنتوم={momentum*100:.2f}%")
                    result = place_order("buy", amount, snap["price"], symbol)
                    if result.get("filled"):
                        state.update({"in_position": True, "symbol": symbol, "src_currency": symbol[:-3].lower(), "dst_currency": "rls", "entry_price": result["average_price"], "amount": result["amount"]})
                        save_state(state)
                        log(f"🟢 ورود آزمایشی به {symbol} ثبت شد.")
                else:
                    log("⏳ فعلاً هیچ بازار امتیاز کافی ندارد؛ خرید نمی‌کنیم.")
            save_state(state)
            time.sleep(CHECK_SECONDS)
        except KeyboardInterrupt:
            save_state(state)
            log("⏹️ ربات متوقف شد.")
            break
        except Exception as e:
            log(f"⚠️ خطا: {e}")
            time.sleep(CHECK_SECONDS)


if __name__ == "__main__":
    run_bot()


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
        log(
            f"🧪 [TEST MODE] سفارش فقط شبیه‌سازی شد: "
            f"{order_type.upper()} | مقدار={amount:.8f} BTC | قیمت={price:,.0f} RLS"
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
    log(f"پاسخ نوبیتکس: {result}")

    if result.get("status") != "ok":
        raise RuntimeError(f"ثبت سفارش ناموفق بود: {result}")

    order = result.get("order", {})
    order_id = order.get("id")
    if not order_id:
        raise RuntimeError(f"شناسه سفارش در پاسخ پیدا نشد: {result}")

    for _ in range(ORDER_POLL_ATTEMPTS):
        status = get_order_status(order_id)
        log(f"وضعیت سفارش: {status}")

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
    started_at = time.monotonic()

    log("================================")
    log("   NOBITEX TRADING BOT")
    log("================================")
    log(f"محیط API: {API_ENV}")
    log(f"حالت فعلی: {'آزمایشی' if TEST_MODE else 'واقعی'}")
    if TEST_MODE:
        log("🛡️ هیچ خرید یا فروش واقعی انجام نمی‌شود؛ سفارش‌ها فقط شبیه‌سازی هستند.")
        if ALLOW_PUBLIC_MARKET_FALLBACK:
            log("🔁 fallback عمومی بازار در صورت قطعی Testnet فعال است؛ فقط برای TEST_MODE.")
    else:
        log("⚠️ هشدار: حالت واقعی فعال است!")
    log(f"نماد: {SYMBOL} | MA کوتاه={SHORT_WINDOW} | MA بلند={LONG_WINDOW}")
    log(f"حد ضرر={STOP_LOSS * 100:.1f}% | حد سود={TAKE_PROFIT * 100:.1f}%")
    log("--------------------------------")

    while True:
        if MAX_RUNTIME_SECONDS > 0 and time.monotonic() - started_at >= MAX_RUNTIME_SECONDS:
            save_state(state)
            log("⏹️ زمان این نوبت تمام شد؛ وضعیت ذخیره شد.")
            break

        try:
            price = get_latest_price()
            prices.append(price)

            if len(prices) > LONG_WINDOW + 1:
                prices.pop(0)

            log(f"📊 داده قیمت #{len(prices)} | BTCIRT={price:,.0f}")

            if len(prices) < LONG_WINDOW + 1:
                log(f"⏳ برای محاسبه سیگنال {LONG_WINDOW + 1 - len(prices)} داده دیگر لازم است.")
                time.sleep(CHECK_SECONDS)
                continue

            previous_short = mean(prices[-SHORT_WINDOW - 1:-1])
            previous_long = mean(prices[-LONG_WINDOW - 1:-1])
            short_ma = mean(prices[-SHORT_WINDOW:])
            long_ma = mean(prices[-LONG_WINDOW:])

            crossed_up = previous_short <= previous_long and short_ma > long_ma
            log(
                f"📈 MA کوتاه={short_ma:,.0f} | MA بلند={long_ma:,.0f} | "
                f"سیگنال صعودی={'بله' if crossed_up else 'خیر'}"
            )

            if not state["in_position"] and crossed_up:
                amount = TRADE_AMOUNT_RLS / price
                log("🟢 سیگنال خرید صادر شد.")
                result = place_order("buy", amount, price)
                if result.get("filled"):
                    state = {
                        "in_position": True,
                        "entry_price": result["average_price"],
                        "amount": result["amount"],
                    }
                    save_state(state)
                    log(f"🧪 ورود آزمایشی در قیمت {state['entry_price']:,.0f}")

            elif state["in_position"]:
                entry_price = state["entry_price"]
                amount = state["amount"]
                stop_price = entry_price * (1 - STOP_LOSS)
                target_price = entry_price * (1 + TAKE_PROFIT)

                log(
                    f"🎯 موقعیت باز | ورود={entry_price:,.0f} | "
                    f"حد ضرر={stop_price:,.0f} | حد سود={target_price:,.0f}"
                )

                if price <= stop_price:
                    log("🔴 حد ضرر فعال شد.")
                    result = place_order("sell", amount, price)
                    if result.get("filled"):
                        state = {"in_position": False, "entry_price": 0, "amount": 0}
                        save_state(state)
                        log("🧪 خروج آزمایشی با حد ضرر انجام شد.")

                elif price >= target_price:
                    log("🟢 حد سود فعال شد.")
                    result = place_order("sell", amount, price)
                    if result.get("filled"):
                        state = {"in_position": False, "entry_price": 0, "amount": 0}
                        save_state(state)
                        log("🧪 خروج آزمایشی با حد سود انجام شد.")

            time.sleep(CHECK_SECONDS)

        except KeyboardInterrupt:
            save_state(state)
            log("⏹️ ربات متوقف شد.")
            break
        except Exception as e:
            log(f"⚠️ خطا: {e}")
            time.sleep(CHECK_SECONDS)


if __name__ == "__main__":
    run_bot()
