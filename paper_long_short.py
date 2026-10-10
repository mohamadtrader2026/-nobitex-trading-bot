#!/usr/bin/env python3
"""
Nobitex LONG/SHORT paper-trading bot.

SAFETY: simulation only. This program reads public market data and NEVER
submits orders, needs an API token, or accesses private account endpoints.

Run:
    pip install -r requirements.txt
    python paper_long_short.py
Stop with Ctrl+C.
"""
import json
import logging
import math
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

API_URL = "https://apiv2.nobitex.ir/v3/orderbook/all"
SYMBOLS = tuple(dict.fromkeys(
    s.strip().upper()
    for s in os.getenv(
        "PAPER_SYMBOLS", "BTCIRT,ETHIRT,USDTIRT,DOGEIRT,XRPIRT,SOLIRT"
    ).split(",")
    if s.strip()
))
INTERVAL_SECONDS = max(15, int(os.getenv("PAPER_INTERVAL_SECONDS", "60")))
FAST = max(2, int(os.getenv("PAPER_FAST_WINDOW", "5")))
SLOW = max(FAST + 1, int(os.getenv("PAPER_SLOW_WINDOW", "20")))
STOP_LOSS_PCT = max(0.1, float(os.getenv("PAPER_STOP_LOSS_PCT", "1.0"))) / 100
TAKE_PROFIT_PCT = max(0.1, float(os.getenv("PAPER_TAKE_PROFIT_PCT", "2.0"))) / 100
FEE_PCT_PER_SIDE = max(0.0, float(os.getenv("PAPER_FEE_PCT", "0.1"))) / 100
VIRTUAL_NOTIONAL_RLS = max(1.0, float(os.getenv("PAPER_NOTIONAL_RLS", "1000000")))
STATE_PATH = Path(os.getenv("PAPER_STATE_FILE", "paper_long_short_state.json"))
HTTP_TIMEOUT_SECONDS = 10
MAX_HISTORY = SLOW + 5
MAX_SPREAD_PCT = max(0.0, float(os.getenv("PAPER_MAX_SPREAD_PCT", "0.25"))) / 100

logging.basicConfig(
    level=os.getenv("PAPER_LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s UTC | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("paper_long_short")


def _finite_positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _level_price(level: Any) -> float | None:
    """Accept the common [price, amount] or {'price': ...} order-book shapes."""
    if isinstance(level, (list, tuple)) and level:
        return _finite_positive(level[0])
    if isinstance(level, dict):
        return _finite_positive(level.get("price"))
    return None


def fetch_prices(session: requests.Session | None = None) -> dict[str, dict[str, float]]:
    """Fetch validated public snapshots. Invalid symbols are skipped safely."""
    client = session or SESSION
    response = client.get(API_URL, timeout=HTTP_TIMEOUT_SECONDS)
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict) or data.get("status") != "ok":
        raise RuntimeError("پاسخ API بازار معتبر نیست")

    result: dict[str, dict[str, float]] = {}
    for symbol in SYMBOLS:
        book = data.get(symbol)
        if not isinstance(book, dict):
            continue

        last = _finite_positive(book.get("lastTradePrice"))
        bids = book.get("bids")
        asks = book.get("asks")
        bid = _level_price(bids[0]) if isinstance(bids, list) and bids else None
        ask = _level_price(asks[0]) if isinstance(asks, list) and asks else None

        # Without valid two-sided quotes, do not create a new position based on
        # a potentially stale last-trade price. Keep the symbol out this cycle.
        if last is None or bid is None or ask is None or bid > ask:
            continue

        mid = (bid + ask) / 2
        if not math.isfinite(mid) or mid <= 0:
            continue
        spread_pct = (ask - bid) / mid
        # Avoid paper entries in unusually wide markets; the threshold is configurable.
        if spread_pct < 0 or spread_pct > MAX_SPREAD_PCT:
            continue
        result[symbol] = {"last": last, "bid": bid, "ask": ask, "mid": mid}
    return result


def signal(prices: list[float], fast: int = FAST, slow: int = SLOW) -> str:
    """Simple trend/momentum filter; returns LONG, SHORT, or FLAT."""
    if fast < 2 or slow <= fast or len(prices) < slow:
        return "FLAT"
    if any(not math.isfinite(float(p)) or float(p) <= 0 for p in prices[-slow:]):
        return "FLAT"
    recent = prices[-fast:]
    long_window = prices[-slow:]
    fast_ma = sum(recent) / fast
    slow_ma = sum(long_window) / slow
    momentum = prices[-1] / prices[-4] - 1 if len(prices) >= 4 else 0.0
    if fast_ma > slow_ma and momentum > 0:
        return "LONG"
    if fast_ma < slow_ma and momentum < 0:
        return "SHORT"
    return "FLAT"


def pnl_pct(position: dict[str, Any], exit_price: float) -> float:
    """Directional linear return on virtual notional for long and short."""
    entry = float(position["entry"])
    if not math.isfinite(exit_price) or exit_price <= 0 or entry <= 0:
        raise ValueError("قیمت ورود یا خروج نامعتبر است")
    if position["side"] == "LONG":
        return exit_price / entry - 1.0
    if position["side"] == "SHORT":
        return 1.0 - exit_price / entry
    raise ValueError("جهت موقعیت نامعتبر است")


def load_state(path: Path = STATE_PATH) -> dict[str, Any]:
    default = {"history": {}, "position": None, "realized_pnl": 0.0, "trades": 0}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return default
    if not isinstance(state, dict):
        return default

    history = state.get("history", {})
    if not isinstance(history, dict):
        history = {}
    cleaned_history: dict[str, list[float]] = {}
    for symbol, values in history.items():
        if not isinstance(symbol, str) or not isinstance(values, list):
            continue
        cleaned = [v for raw in values if (v := _finite_positive(raw)) is not None]
        cleaned_history[symbol] = cleaned[-MAX_HISTORY:]

    position = state.get("position")
    if not isinstance(position, dict) or position.get("side") not in ("LONG", "SHORT"):
        position = None
    elif (
        position.get("symbol") not in SYMBOLS
        or _finite_positive(position.get("entry")) is None
        or _finite_positive(position.get("notional")) is None
    ):
        position = None
    else:
        position = dict(position)
        position["entry"] = float(position["entry"])
        position["notional"] = float(position["notional"])

    try:
        realized = float(state.get("realized_pnl", 0.0))
        if not math.isfinite(realized):
            realized = 0.0
    except (TypeError, ValueError):
        realized = 0.0
    try:
        trades = max(0, int(state.get("trades", 0)))
    except (TypeError, ValueError, OverflowError):
        trades = 0
    return {"history": cleaned_history, "position": position, "realized_pnl": realized, "trades": trades}


def save_state(state: dict[str, Any], path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(
        json.dumps(state, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    temp_path.replace(path)


def close_position(
    state: dict[str, Any], exit_price: float, reason: str,
    fee_rate: float = FEE_PCT_PER_SIDE,
) -> float:
    position = state.get("position")
    if not isinstance(position, dict):
        return 0.0
    notional = float(position["notional"])
    entry = float(position["entry"])
    gross = pnl_pct(position, exit_price) * notional
    # Approximate entry and exit fees, using exit notional adjusted for price move.
    exit_notional = notional * exit_price / entry
    fees = (notional + exit_notional) * fee_rate
    net = gross - fees
    state["realized_pnl"] = float(state.get("realized_pnl", 0.0)) + net
    state["trades"] = int(state.get("trades", 0)) + 1
    log.info(
        "بستن %s %s | علت: %s | سود/زیان خالص فرضی: %,.0f ریال | مجموع: %,.0f ریال",
        position["side"], position["symbol"], reason, net, state["realized_pnl"],
    )
    state["position"] = None
    return net


def process_snapshot(state: dict[str, Any], snapshots: dict[str, dict[str, float]]) -> None:
    """Apply one polling cycle to state; quotes are used as conservative paper fills."""
    history_map = state.setdefault("history", {})
    for symbol, quote in snapshots.items():
        bid = _finite_positive(quote.get("bid"))
        ask = _finite_positive(quote.get("ask"))
        mid = _finite_positive(quote.get("mid"))
        if bid is None or ask is None or mid is None or bid > ask:
            continue

        history = history_map.setdefault(symbol, [])
        # Identical repeated API snapshots must not be counted as new price action.
        if not history or history[-1] != mid:
            history.append(mid)
            if len(history) > MAX_HISTORY:
                del history[:-MAX_HISTORY]

        position = state.get("position")
        if position is not None:
            if position.get("symbol") != symbol:
                continue
            exit_price = bid if position["side"] == "LONG" else ask
            move = pnl_pct(position, exit_price)
            if move <= -STOP_LOSS_PCT:
                close_position(state, exit_price, "حد ضرر")
            elif move >= TAKE_PROFIT_PCT:
                close_position(state, exit_price, "حد سود")
            elif signal(history) not in ("FLAT", position["side"]):
                close_position(state, exit_price, "سیگنال معکوس")
            continue

        direction = signal(history)
        if direction in ("LONG", "SHORT"):
            # Simulated fills account for the spread: long buys ask; short sells bid.
            entry_price = ask if direction == "LONG" else bid
            state["position"] = {
                "symbol": symbol,
                "side": direction,
                "entry": entry_price,
                "notional": VIRTUAL_NOTIONAL_RLS,
                "opened_at": datetime.now(timezone.utc).isoformat(),
            }
            log.info(
                "ورود فرضی %s روی %s | قیمت ورود: %,.8g | ارزش مجازی: %,.0f ریال",
                direction, symbol, entry_price, VIRTUAL_NOTIONAL_RLS,
            )


def build_session() -> requests.Session:
    client = requests.Session()
    retry = Retry(
        total=3, connect=3, read=3, backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET"]),
        respect_retry_after_header=True,
    )
    adapter = HTTPAdapter(max_retries=retry)
    client.mount("https://", adapter)
    client.headers.update({"User-Agent": "Nobitex-LongShort-PaperBot/2.0"})
    return client


SESSION = build_session()


def main() -> None:
    if not SYMBOLS:
        raise SystemExit("هیچ نمادی در PAPER_SYMBOLS تعریف نشده است.")
    if not (0 < STOP_LOSS_PCT < 1 and 0 < TAKE_PROFIT_PCT < 1):
        raise SystemExit("حد سود/ضرر باید بیشتر از صفر و کمتر از ۱۰۰٪ باشد.")
    if not (0 <= FEE_PCT_PER_SIDE < 0.05):
        raise SystemExit("کارمزد فرضی باید بین صفر و ۵٪ برای هر سمت باشد.")

    state = load_state()
    log.info("ربات آزمایشی LONG/SHORT شروع شد؛ فقط API عمومی و سفارش فرضی.")
    log.info("نمادها: %s | پنجره‌ها: %d/%d | حد ضرر: %.2f%% | حد سود: %.2f%%",
             ", ".join(SYMBOLS), FAST, SLOW, STOP_LOSS_PCT * 100, TAKE_PROFIT_PCT * 100)
    log.info("برای توقف، Ctrl+C را بزن. هیچ سفارش واقعی ارسال نمی‌شود.")

    while True:
        try:
            snapshots = fetch_prices()
            if snapshots:
                process_snapshot(state, snapshots)
                save_state(state)
            else:
                log.warning("این چرخه هیچ دفتر سفارش معتبر و دوطرفه‌ای نداشت؛ تصمیم جدیدی گرفته نشد.")
        except KeyboardInterrupt:
            log.info("توقف توسط کاربر؛ وضعیت ذخیره می‌شود.")
            save_state(state)
            break
            save_state(state)
            break
        except (requests.RequestException, ValueError, RuntimeError, json.JSONDecodeError) as exc:
            log.warning("خطای موقت دریافت/پردازش داده: %s؛ در چرخه بعد دوباره تلاش می‌شود.", exc)
        except OSError as exc:
            log.exception("خطای فایل وضعیت: %s؛ ربات از ارسال سفارش واقعی پشتیبانی نمی‌کند.", exc)
        try:
            time.sleep(INTERVAL_SECONDS)
        except KeyboardInterrupt:
            log.info("توقف توسط کاربر؛ وضعیت ذخیره می‌شود.")
            save_state(state)
            break


if __name__ == "__main__":
    main()
