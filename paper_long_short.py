#!/usr/bin/env python3
"""
Nobitex LONG/SHORT paper-trading bot.
SAFETY: simulation only. It never submits orders and has no API-token support.
Run locally with: python paper_long_short.py
"""
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

API_URL = "https://apiv2.nobitex.ir/v3/orderbook/all"
SYMBOLS = [s.strip().upper() for s in os.getenv(
    "PAPER_SYMBOLS", "BTCIRT,ETHIRT,USDTIRT,DOGEIRT,XRPIRT,SOLIRT"
).split(",") if s.strip()]
INTERVAL_SECONDS = max(15, int(os.getenv("PAPER_INTERVAL_SECONDS", "60")))
FAST = max(2, int(os.getenv("PAPER_FAST_WINDOW", "5")))
SLOW = max(FAST + 1, int(os.getenv("PAPER_SLOW_WINDOW", "20")))
STOP_LOSS_PCT = max(0.1, float(os.getenv("PAPER_STOP_LOSS_PCT", "1.0"))) / 100
TAKE_PROFIT_PCT = max(0.1, float(os.getenv("PAPER_TAKE_PROFIT_PCT", "2.0"))) / 100
FEE_PCT_PER_SIDE = max(0.0, float(os.getenv("PAPER_FEE_PCT", "0.1"))) / 100
STATE_PATH = Path(os.getenv("PAPER_STATE_FILE", "paper_long_short_state.json"))
TIMEOUT = 10

session = requests.Session()
session.headers.update({"User-Agent": "Nobitex-LongShort-PaperBot/1.0"})


def log(msg):
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    print(f"[{now}] {msg}", flush=True)


def load_state():
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(state, dict):
            return state
    except (OSError, json.JSONDecodeError):
        pass
    return {"history": {}, "position": None, "realized_pnl": 0.0, "trades": 0}


def save_state(state):
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE_PATH)


def fetch_prices():
    response = session.get(API_URL, timeout=TIMEOUT)
    response.raise_for_status()
    data = response.json()
    if data.get("status") != "ok":
        raise RuntimeError("پاسخ API بازار معتبر نیست")
    result = {}
    for symbol in SYMBOLS:
        book = data.get(symbol)
        if not isinstance(book, dict):
            continue
        try:
            price = float(book.get("lastTradePrice") or 0)
        except (TypeError, ValueError):
            continue
        if price > 0:
            result[symbol] = price
    return result


def signal(prices):
    if len(prices) < SLOW:
        return "FLAT"
    fast = sum(prices[-FAST:]) / FAST
    slow = sum(prices[-SLOW:]) / SLOW
    # Require short-term momentum to agree with the moving-average direction.
    momentum = prices[-1] / prices[-4] - 1 if len(prices) >= 4 else 0
    if fast > slow and momentum > 0:
        return "LONG"
    if fast < slow and momentum < 0:
        return "SHORT"
    return "FLAT"


def pnl_pct(position, price):
    entry = float(position["entry"])
    if position["side"] == "LONG":
        return price / entry - 1
    return entry / price - 1


def close_position(state, price, reason):
    pos = state["position"]
    gross = pnl_pct(pos, price) * float(pos["notional"])
    fees = float(pos["notional"]) * FEE_PCT_PER_SIDE + float(pos["notional"]) * (price / float(pos["entry"])) * FEE_PCT_PER_SIDE
    net = gross - fees
    state["realized_pnl"] = float(state.get("realized_pnl", 0)) + net
    state["trades"] = int(state.get("trades", 0)) + 1
    log(f"بستن {pos['side']} {pos['symbol']} | علت: {reason} | سود/زیان خالص فرضی: {net:,.0f} ریال | مجموع: {state['realized_pnl']:,.0f} ریال")
    state["position"] = None


def main():
    state = load_state()
    log("ربات LONG/SHORT آزمایشی شروع شد؛ فقط داده عمومی می‌خواند و سفارش واقعی نمی‌فرستد.")
    log(f"بازارها: {', '.join(SYMBOLS)} | پنجره‌ها: {FAST}/{SLOW} | کارمزد فرضی هر سمت: {FEE_PCT_PER_SIDE*100:.3f}%")
    while True:
        try:
            prices = fetch_prices()
            for symbol, price in prices.items():
                history = state.setdefault("history", {}).setdefault(symbol, [])
                history.append(price)
                if len(history) > SLOW + 5:
                    del history[:-SLOW - 5]

                pos = state.get("position")
                if pos and pos["symbol"] == symbol:
                    move = pnl_pct(pos, price)
                    if move <= -STOP_LOSS_PCT:
                        close_position(state, price, "حد ضرر")
                    elif move >= TAKE_PROFIT_PCT:
                        close_position(state, price, "حد سود")
                    elif signal(history) not in ("FLAT", pos["side"]):
                        close_position(state, price, "سیگنال معکوس")
                    continue

                if state.get("position") is not None:
                    continue
                direction = signal(history)
                if direction in ("LONG", "SHORT"):
                    # Fixed virtual notional for comparison only; no funds are used.
                    state["position"] = {
                        "symbol": symbol,
                        "side": direction,
                        "entry": price,
                        "notional": 1_000_000,
                        "opened_at": datetime.now(timezone.utc).isoformat(),
                    }
                    log(f"ورود فرضی {direction} روی {symbol} با قیمت {price:,.0f} | ارزش مجازی ۱,۰۰۰,۰۰۰ ریال")
            save_state(state)
            if not prices:
                log("قیمت قابل استفاده دریافت نشد؛ در چرخه بعدی دوباره تلاش می‌کنم.")
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            log(f"خطای دریافت داده: {exc}")
        except KeyboardInterrupt:
            log("ربات توسط کاربر متوقف شد.")
            save_state(state)
            return
        time.sleep(INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
