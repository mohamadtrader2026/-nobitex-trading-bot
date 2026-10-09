import os
import time
import uuid
import json
import requests
from statistics import mean
from datetime import datetime, timezone

API_ENV = os.getenv("NOBITEX_API_ENV", "testnet").lower()
if API_ENV == "testnet":
    PUBLIC_API_BASE = TRADE_API_BASE = "https://testnetapi.nobitex.ir"
elif API_ENV == "mainnet":
    PUBLIC_API_BASE = "https://apiv2.nobitex.ir"
    TRADE_API_BASE = "https://api.nobitex.ir"
else:
    raise RuntimeError("NOBITEX_API_ENV باید testnet یا mainnet باشد.")

CANDIDATE_SYMBOLS = ["BTCIRT","ETHIRT","USDTIRT","DOGEIRT","SHIBIRT","XRPIRT","LTCIRT","ADAIRT","SOLIRT","TRXIRT","LINKIRT","XLMIRT"]
DEFAULT_SYMBOL = "BTCIRT"
SHORT_WINDOW = 5
LONG_WINDOW = 20
HISTORY_SIZE = LONG_WINDOW + 1
TRADE_AMOUNT_RLS = 1_000_000
STOP_LOSS = 0.02
TAKE_PROFIT = 0.04

# سرعت بالاتر، بدون بی‌دقتی در ارسال سفارش
CHECK_SECONDS = float(os.getenv("NOBITEX_CHECK_SECONDS", "1"))
ORDER_POLL_SECONDS = float(os.getenv("NOBITEX_ORDER_POLL_SECONDS", "0.25"))
ORDER_POLL_ATTEMPTS = 20
REQUEST_TIMEOUT = 5
MAX_RUNTIME_SECONDS = int(os.getenv("NOBITEX_MAX_RUNTIME_SECONDS", "0"))
MIN_SCORE = float(os.getenv("NOBITEX_MIN_SCORE", "0.15"))
MAX_SPREAD_PCT = float(os.getenv("NOBITEX_MAX_SPREAD_PCT", "0.80"))
REENTRY_COOLDOWN_SECONDS = float(os.getenv("NOBITEX_REENTRY_COOLDOWN_SECONDS", "8"))

TEST_MODE = os.getenv("NOBITEX_TEST_MODE", "true").lower() == "true"
API_TOKEN = os.getenv("NOBITEX_API_TOKEN")
STATE_FILE = "bot_state.json"
ALLOW_PUBLIC_MARKET_FALLBACK = API_ENV == "testnet" and TEST_MODE and os.getenv("NOBITEX_PUBLIC_MARKET_FALLBACK","true").lower() == "true"
PUBLIC_MARKET_FALLBACK_BASE = "https://apiv2.nobitex.ir"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "Nobitex-Smart-Bot/1.0", "Connection": "keep-alive"})


def log(message):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{now}] {message}", flush=True)


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
        return {
            "in_position": bool(s.get("in_position", False)),
            "symbol": str(s.get("symbol", DEFAULT_SYMBOL)),
            "entry_price": float(s.get("entry_price", 0)),
            "amount": float(s.get("amount", 0)),
            "history": s.get("history", {}),
            "last_exit_at": float(s.get("last_exit_at", 0)),
            "halted": bool(s.get("halted", False)),
            "pending_order_id": str(s.get("pending_order_id", "")),
            "pending_order_type": str(s.get("pending_order_type", "")),
            "pending_symbol": str(s.get("pending_symbol", "")),
        }
    except (FileNotFoundError, ValueError, TypeError):
        return {"in_position": False, "symbol": DEFAULT_SYMBOL, "entry_price": 0, "amount": 0, "history": {}, "last_exit_at": 0, "halted": False, "pending_order_id": "", "pending_order_type": "", "pending_symbol": ""}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def fetch_market_snapshot():
    urls = [f"{PUBLIC_API_BASE}/v3/orderbook/all"]
    if ALLOW_PUBLIC_MARKET_FALLBACK:
        urls.append(f"{PUBLIC_MARKET_FALLBACK_BASE}/v3/orderbook/all")

    last_error = None
    for idx, url in enumerate(urls):
        try:
            r = SESSION.get(url, timeout=REQUEST_TIMEOUT)
            r.raise_for_status()
            data = r.json()
            if data.get("status") != "ok":
                raise RuntimeError("پاسخ بازار نامعتبر است")
            snapshots = {}
            for symbol in CANDIDATE_SYMBOLS:
                book = data.get(symbol)
                if not isinstance(book, dict) or not book.get("lastTradePrice"):
                    continue
                try:
                    price = float(book["lastTradePrice"])
                    bids, asks = book.get("bids") or [], book.get("asks") or []
                    if not bids or not asks or price <= 0:
                        continue
                    bid, ask = float(bids[0][0]), float(asks[0][0])
                    spread = max(0.0, (ask - bid) / price * 100)
                    depth = sum(float(x[0])*float(x[1]) for x in bids[:5]) + sum(float(x[0])*float(x[1]) for x in asks[:5])
                    snapshots[symbol] = {"price": price, "bid": bid, "ask": ask, "spread_pct": spread, "depth": depth}
                except (TypeError, ValueError, IndexError):
                    continue
            if idx == 1:
                log("⚠️ Testnet در دسترس نیست؛ داده عمومی بازار اصلی فقط برای TEST_MODE استفاده شد.")
            return snapshots
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"دریافت بازار ناموفق بود: {last_error}")


def select_best_market(snapshots, history):
    scored = []
    for symbol, snap in snapshots.items():
        prices = history.setdefault(symbol, [])
        prices.append(snap["price"])
        if len(prices) > HISTORY_SIZE:
            del prices[:-HISTORY_SIZE]
        if len(prices) < HISTORY_SIZE:
            continue
        short_ma = mean(prices[-SHORT_WINDOW:])
        long_ma = mean(prices[-LONG_WINDOW:])
        momentum = prices[-1] / prices[-SHORT_WINDOW] - 1
        trend = short_ma / long_ma - 1
        liquidity = min(1.0, snap["depth"] / max(TRADE_AMOUNT_RLS * 5, 1))
        spread_penalty = min(1.0, snap["spread_pct"] / max(MAX_SPREAD_PCT, 0.01))
        score = trend * 4 + momentum * 3 + liquidity * 0.25 - spread_penalty * 0.25
        scored.append((score, symbol, snap, trend, momentum, liquidity))
    scored.sort(reverse=True, key=lambda x: x[0])
    if not scored:
        return None, []
    best = scored[0]
    if best[0] < MIN_SCORE or best[3] <= 0 or best[4] <= 0 or best[2]["spread_pct"] > MAX_SPREAD_PCT:
        return None, scored
    return best, scored


class OrderStatusUnknown(RuntimeError):
    """An order may have been accepted, but its final status is not confirmed."""
    def __init__(self, order_id, order_type="unknown", symbol=DEFAULT_SYMBOL):
        self.order_id = str(order_id) if order_id is not None else "unknown"
        self.order_type = str(order_type)
        self.symbol = str(symbol)
        super().__init__(f"وضعیت سفارش {self.order_id} قطعی نیست؛ برای جلوگیری از سفارش تکراری، ربات متوقف می‌شود.")


def get_order_status(order_id):
    r = SESSION.post(
        f"{TRADE_API_BASE}/market/orders/status",
        headers={"Authorization": f"Token {API_TOKEN}"},
        json={"id": order_id},
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    return r.json()


def place_order(order_type, amount, price, symbol):
    if TEST_MODE:
        log(f"🧪 [TEST MODE] {order_type.upper()} {symbol} | مقدار={amount:.8f} | قیمت={price:,.0f}")
        return {"status": "test", "filled": True, "amount": amount, "average_price": price}

    if not API_TOKEN:
        raise RuntimeError("NOBITEX_API_TOKEN تنظیم نشده است.")

    src_currency = symbol[:-3].lower() if symbol.endswith("IRT") else symbol[:-4].lower()
    dst_currency = "rls" if symbol.endswith("IRT") else "usdt"
    payload = {
        "type": order_type,
        "execution": "market",
        "srcCurrency": src_currency,
        "dstCurrency": dst_currency,
        "amount": str(amount),
        "clientOrderId": f"bot-{uuid.uuid4().hex[:24]}",
    }
    r = SESSION.post(
        f"{TRADE_API_BASE}/market/orders/add",
        headers={"Authorization": f"Token {API_TOKEN}", "Content-Type": "application/json"},
        json=payload,
        timeout=REQUEST_TIMEOUT,
    )
    r.raise_for_status()
    result = r.json()
    if result.get("status") != "ok":
        raise RuntimeError(f"ثبت سفارش ناموفق بود: {result}")
    order_id = result.get("order", {}).get("id")
    if not order_id:
        raise OrderStatusUnknown("unknown", order_type, symbol)

    # سریع status را چک می‌کنیم، اما تا وضعیت قطعی نیامده سفارش را تکرار نمی‌کنیم.
    for _ in range(ORDER_POLL_ATTEMPTS):
        try:
            status = get_order_status(order_id)
        except Exception as exc:
            raise OrderStatusUnknown(order_id, order_type, symbol) from exc
        order = status.get("order", {})
        if order.get("status") == "Done":
            return {
                "status": "ok",
                "filled": True,
                "amount": float(order.get("matchedAmount") or order.get("amount") or amount),
                "average_price": float(order.get("averagePrice") or price),
            }
        if order.get("status") in {"Canceled", "Rejected"}:
            raise RuntimeError(f"سفارش اجرا نشد: {status}")
        time.sleep(ORDER_POLL_SECONDS)

    # مهم: timeout را معامله انجام‌شده فرض نمی‌کنیم؛ از ارسال سفارش تکراری جلوگیری می‌شود.
    raise OrderStatusUnknown(order_id, order_type, symbol)


def run_bot():
    state = load_state()
    started_at = time.monotonic()
    log("================================")
    log("   NOBITEX SMART FAST BOT")
    log("================================")
    log(f"محیط={API_ENV} | حالت={'آزمایشی' if TEST_MODE else 'واقعی'} | فاصله بررسی={CHECK_SECONDS}s | poll سفارش={ORDER_POLL_SECONDS}s")
    if state.get("halted"):
        log(f"🛑 ربات به‌دلیل سفارش نامشخص قبلی متوقف می‌ماند؛ شناسه برای بررسی دستی: {state.get('pending_order_id') or 'نامشخص'}")
        raise SystemExit(2)
    if TEST_MODE:
        log("🛡️ TEST_MODE فعال است؛ سفارش واقعی ارسال نمی‌شود.")

    while True:
        if MAX_RUNTIME_SECONDS > 0 and time.monotonic() - started_at >= MAX_RUNTIME_SECONDS:
            save_state(state)
            log("⏹️ پایان چرخه؛ وضعیت ذخیره شد.")
            break
        try:
            snapshots = fetch_market_snapshot()
            if not snapshots:
                time.sleep(CHECK_SECONDS)
                continue

            if state["in_position"]:
                symbol = state["symbol"]
                snap = snapshots.get(symbol)
                if not snap:
                    log(f"⚠️ داده {symbol} موجود نیست؛ فروش اجباری نمی‌کنیم.")
                    time.sleep(CHECK_SECONDS)
                    continue
                price = snap["price"]
                entry = state["entry_price"]
                amount = state["amount"]
                sl = entry * (1 - STOP_LOSS)
                tp = entry * (1 + TAKE_PROFIT)
                if price <= sl:
                    log(f"🔴 SL سریع فعال شد: {symbol}")
                    result = place_order("sell", amount, snap["bid"], symbol)
                    if result.get("filled"):
                        state.update({"in_position": False, "entry_price": 0, "amount": 0, "last_exit_at": time.time()})
                elif price >= tp:
                    log(f"🟢 TP فعال شد: {symbol}")
                    result = place_order("sell", amount, snap["bid"], symbol)
                    if result.get("filled"):
                        state.update({"in_position": False, "entry_price": 0, "amount": 0, "last_exit_at": time.time()})
            else:
                if time.monotonic() - state.get("last_exit_at", 0) < REENTRY_COOLDOWN_SECONDS:
                    time.sleep(CHECK_SECONDS)
                    continue
                best, scored = select_best_market(snapshots, state["history"])
                if best:
                    score, symbol, snap, trend, momentum, liquidity = best
                    amount = TRADE_AMOUNT_RLS / snap["ask"]
                    log(f"🏆 {symbol} | score={score:.4f} | trend={trend*100:.2f}% | momentum={momentum*100:.2f}% | spread={snap['spread_pct']:.2f}%")
                    result = place_order("buy", amount, snap["ask"], symbol)
                    if result.get("filled"):
                        state.update({"in_position": True, "symbol": symbol, "entry_price": result["average_price"], "amount": result["amount"]})
                        log(f"🟢 ورود به {symbol} ثبت شد.")
                else:
                    log("⏳ سیگنال دقیق و کم‌ریسک کافی نیست؛ فعلاً معامله نمی‌کنیم.")
            save_state(state)
            time.sleep(CHECK_SECONDS)
        except OrderStatusUnknown as exc:
            state["halted"] = True
            state["pending_order_id"] = exc.order_id
            state["pending_order_type"] = exc.order_type
            state["pending_symbol"] = exc.symbol
            save_state(state)
            log(f"🛑 توقف ایمن: وضعیت سفارش قطعی نیست؛ سفارش جدید ارسال نمی‌شود. شناسه={exc.order_id}")
            raise SystemExit(2)
        except KeyboardInterrupt:
            save_state(state)
            log("⏹️ ربات متوقف شد.")
            break
        except Exception as exc:
            log(f"⚠️ خطا: {exc}")
            time.sleep(CHECK_SECONDS)


if __name__ == "__main__":
    run_bot()
