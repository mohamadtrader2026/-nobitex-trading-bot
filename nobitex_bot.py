import os
import time
import uuid
import json
import requests
from statistics import mean, pstdev
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
STOP_LOSS = float(os.getenv("NOBITEX_STOP_LOSS_PCT", "0.01"))
TAKE_PROFIT = float(os.getenv("NOBITEX_TAKE_PROFIT_PCT", "0.05"))
DAILY_LOSS_LIMIT_PCT = float(os.getenv("NOBITEX_DAILY_LOSS_LIMIT_PCT", "0.03"))
DAILY_LOSS_BASE_RLS = float(os.getenv("NOBITEX_DAILY_LOSS_BASE_RLS", str(TRADE_AMOUNT_RLS)))
if not (0 < STOP_LOSS < 1 and 0 < TAKE_PROFIT < 1 and 0 < DAILY_LOSS_LIMIT_PCT < 1 and DAILY_LOSS_BASE_RLS > 0):
    raise ValueError("تنظیمات حد سود/ضرر روزانه نامعتبر است.")

# سرعت بالاتر، بدون بی‌دقتی در ارسال سفارش
CHECK_SECONDS = float(os.getenv("NOBITEX_CHECK_SECONDS", "1"))
ORDER_POLL_SECONDS = float(os.getenv("NOBITEX_ORDER_POLL_SECONDS", "0.25"))
ORDER_POLL_ATTEMPTS = 20
REQUEST_TIMEOUT = 5
MAX_RUNTIME_SECONDS = int(os.getenv("NOBITEX_MAX_RUNTIME_SECONDS", "0"))
MIN_SCORE = float(os.getenv("NOBITEX_MIN_SCORE", "0.18"))
MAX_SPREAD_PCT = float(os.getenv("NOBITEX_MAX_SPREAD_PCT", "0.25"))
MAX_VOLATILITY_PCT = float(os.getenv("NOBITEX_MAX_VOLATILITY_PCT", "0.25"))
MIN_UP_MOVE_RATIO = float(os.getenv("NOBITEX_MIN_UP_MOVE_RATIO", "0.50"))
MIN_BOOK_IMBALANCE = float(os.getenv("NOBITEX_MIN_BOOK_IMBALANCE", "0.40"))
FEE_PCT = float(os.getenv("NOBITEX_FEE_PCT", "0.1"))
if not (0 <= FEE_PCT < 5 and 0 <= MIN_BOOK_IMBALANCE <= 1):
    raise ValueError("تنظیمات کارمزد یا عدم‌تعادل اردربوک نامعتبر است.")
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
            "daily_loss_date": str(s.get("daily_loss_date", datetime.now(timezone.utc).date().isoformat())),
            "daily_loss_rials": float(s.get("daily_loss_rials", 0)),
        }
    except (FileNotFoundError, ValueError, TypeError):
        return {"in_position": False, "symbol": DEFAULT_SYMBOL, "entry_price": 0, "amount": 0, "history": {}, "last_exit_at": 0, "halted": False, "pending_order_id": "", "pending_order_type": "", "pending_symbol": "", "daily_loss_date": datetime.now(timezone.utc).date().isoformat(), "daily_loss_rials": 0.0}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)



def reset_daily_loss_if_new_day(state):
    today = datetime.now(timezone.utc).date().isoformat()
    if state.get("daily_loss_date") != today:
        state["daily_loss_date"] = today
        state["daily_loss_rials"] = 0.0
        log("📅 روز UTC جدید؛ شمارنده زیان روزانه از صفر شروع شد.")
    state["daily_loss_rials"] = max(0.0, float(state.get("daily_loss_rials", 0.0)))


def daily_loss_limit_rials():
    return DAILY_LOSS_BASE_RLS * DAILY_LOSS_LIMIT_PCT


def finalize_exit(state, exit_price):
    entry = float(state.get("entry_price", 0))
    amount = float(state.get("amount", 0))
    exit_price = float(exit_price)
    gross_pnl = (exit_price - entry) * amount
    fees = (entry * amount + exit_price * amount) * (FEE_PCT / 100.0)
    pnl_rials = gross_pnl - fees
    if pnl_rials < 0:
        state["daily_loss_rials"] = float(state.get("daily_loss_rials", 0.0)) + abs(pnl_rials)
    state.update({"in_position": False, "entry_price": 0, "amount": 0, "last_exit_at": time.time()})
    log(f"📊 نتیجه خالص تخمینی: {pnl_rials:,.0f} ریال (کارمزد تخمینی {fees:,.0f}) | زیان تحقق‌یافته امروز: {state['daily_loss_rials']:,.0f}/{daily_loss_limit_rials():,.0f} ریال")
    if state["daily_loss_rials"] >= daily_loss_limit_rials():
        log("🛑 سقف زیان روزانه رسید؛ ورودهای جدید تا روز UTC بعدی متوقف می‌شوند.")


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
                    bid_depth = sum(float(x[0])*float(x[1]) for x in bids[:5])
                    ask_depth = sum(float(x[0])*float(x[1]) for x in asks[:5])
                    depth = bid_depth + ask_depth
                    book_imbalance = bid_depth / depth if depth > 0 else 0.5
                    snapshots[symbol] = {"price": price, "bid": bid, "ask": ask, "spread_pct": spread, "depth": depth, "bid_depth": bid_depth, "ask_depth": ask_depth, "book_imbalance": book_imbalance}
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
        recent = prices[-SHORT_WINDOW:]
        up_move_ratio = sum(1 for i in range(1, len(recent)) if recent[i] > recent[i - 1]) / max(len(recent) - 1, 1)
        returns_pct = [(prices[i] / prices[i - 1] - 1) * 100 for i in range(1, len(prices)) if prices[i - 1] > 0]
        volatility_pct = pstdev(returns_pct[-LONG_WINDOW:]) if len(returns_pct) >= 2 else 0.0

        book_imbalance = float(snap.get("book_imbalance", 0.5))
        # Normalize trend and momentum to percentage points; reward bid-side depth only modestly.
        trend_pct = trend * 100
        momentum_pct = momentum * 100
        score = (
            trend_pct * 2.0
            + momentum_pct * 1.5
            + liquidity * 0.20
            + up_move_ratio * 0.15
            - snap["spread_pct"] * 1.2
            - volatility_pct * 0.8
            + (book_imbalance - 0.5) * 0.4
        )
        eligible = (
            trend > 0
            and momentum > 0
            and up_move_ratio >= MIN_UP_MOVE_RATIO
            and snap["spread_pct"] <= MAX_SPREAD_PCT
            and volatility_pct <= MAX_VOLATILITY_PCT
            and book_imbalance >= MIN_BOOK_IMBALANCE
            and score >= MIN_SCORE
        )
        scored.append((score, symbol, snap, trend, momentum, liquidity, up_move_ratio, volatility_pct, eligible))

    scored.sort(reverse=True, key=lambda x: x[0])
    # Don't let one poor market prevent selecting a different market that passes every filter.
    for candidate in scored:
        if candidate[8]:
            return candidate, scored
    return None, scored


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
    reset_daily_loss_if_new_day(state)
    log(f"🎯 حد سود={TAKE_PROFIT*100:.2f}% | 🛑 حد ضرر={STOP_LOSS*100:.2f}% | سقف زیان روزانه={DAILY_LOSS_LIMIT_PCT*100:.2f}% از پایه {DAILY_LOSS_BASE_RLS:,.0f} ریال ({daily_loss_limit_rials():,.0f} ریال)")
    if TEST_MODE:
        log("🛡️ TEST_MODE فعال است؛ سفارش واقعی ارسال نمی‌شود.")

    while True:
        if MAX_RUNTIME_SECONDS > 0 and time.monotonic() - started_at >= MAX_RUNTIME_SECONDS:
            save_state(state)
            log("⏹️ پایان چرخه؛ وضعیت ذخیره شد.")
            break
        try:
            reset_daily_loss_if_new_day(state)
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
                # Exit checks use executable bid, not last-traded price, to avoid optimistic triggers.
                price = snap["bid"]
                entry = state["entry_price"]
                amount = state["amount"]
                sl = entry * (1 - STOP_LOSS)
                tp = entry * (1 + TAKE_PROFIT)
                if price <= sl:
                    log(f"🔴 SL سریع فعال شد: {symbol}")
                    result = place_order("sell", amount, snap["bid"], symbol)
                    if result.get("filled"):
                        finalize_exit(state, result.get("average_price", snap["bid"]))
                elif price >= tp:
                    log(f"🟢 TP فعال شد: {symbol}")
                    result = place_order("sell", amount, snap["bid"], symbol)
                    if result.get("filled"):
                        finalize_exit(state, result.get("average_price", snap["bid"]))
            else:
                if float(state.get("daily_loss_rials", 0.0)) >= daily_loss_limit_rials():
                    log("🛑 سقف زیان روزانه فعال است؛ ورود جدید تا روز UTC بعدی مجاز نیست.")
                    time.sleep(CHECK_SECONDS)
                    continue
                if time.time() - state.get("last_exit_at", 0) < REENTRY_COOLDOWN_SECONDS:
                    time.sleep(CHECK_SECONDS)
                    continue
                best, scored = select_best_market(snapshots, state["history"])
                if best:
                    score, symbol, snap, trend, momentum, liquidity, up_move_ratio, volatility_pct, _eligible = best
                    amount = TRADE_AMOUNT_RLS / snap["ask"]
                    log(f"🏆 {symbol} | score={score:.4f} | trend={trend*100:.2f}% | momentum={momentum*100:.2f}% | spread={snap['spread_pct']:.2f}% | نسبت عمق خرید={snap.get('book_imbalance', 0.5)*100:.0f}% | حرکت صعودی={best[6]*100:.0f}% | نوسان={best[7]:.3f}%")
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
