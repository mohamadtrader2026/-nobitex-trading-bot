"""Offline, fee-aware backtest for the bot's existing entry selector.

CSV columns required: timestamp,symbol,price,bid,ask,depth
Optional: spread_pct (otherwise computed from bid/ask and price).
Rows at the same timestamp form one market snapshot. Use real, consistently sampled
historical order-book snapshots; this script does not download or invent market data.
"""
import argparse
import csv
import json
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import nobitex_bot as bot


def load_snapshots(path):
    grouped = defaultdict(dict)
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"timestamp", "symbol", "price", "bid", "ask", "depth"}
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            raise ValueError("CSV columns required: timestamp,symbol,price,bid,ask,depth")
        for line, row in enumerate(reader, start=2):
            try:
                timestamp = row["timestamp"].strip()
                # Validate ISO-like timestamps without requiring a specific timezone format.
                datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
                symbol = row["symbol"].strip().upper()
                price, bid, ask, depth = (float(row[k]) for k in ("price", "bid", "ask", "depth"))
                spread = float(row["spread_pct"]) if row.get("spread_pct", "").strip() else max(0.0, (ask - bid) / price * 100)
                if not symbol or not all(math.isfinite(v) for v in (price, bid, ask, depth, spread)):
                    raise ValueError("non-finite or empty value")
                if price <= 0 or bid <= 0 or ask < bid or depth < 0 or spread < 0:
                    raise ValueError("invalid price/book values")
                grouped[timestamp][symbol] = {
                    "price": price, "bid": bid, "ask": ask,
                    "depth": depth, "spread_pct": spread,
                }
            except (TypeError, ValueError, ZeroDivisionError) as exc:
                raise ValueError(f"Invalid CSV row {line}: {exc}") from exc
    return [(ts, grouped[ts]) for ts in sorted(grouped)]


def run_backtest(rows, fee_pct=0.1, trade_amount=bot.TRADE_AMOUNT_RLS):
    if not 0 <= fee_pct < 10:
        raise ValueError("fee_pct must be between 0 and 10")
    if trade_amount <= 0:
        raise ValueError("trade_amount must be positive")

    history = {}
    position = None
    trades = []
    equity = 0.0
    equity_peak = 0.0
    max_drawdown = 0.0
    daily_loss = 0.0
    active_day = None
    fee_rate = fee_pct / 100.0

    for timestamp, snapshots in rows:
        day = timestamp[:10]
        if day != active_day:
            active_day, daily_loss = day, 0.0

        if position:
            snap = snapshots.get(position["symbol"])
            if snap:
                price = snap["price"]
                entry = position["entry_price"]
                reason = "stop_loss" if price <= entry * (1 - bot.STOP_LOSS) else (
                    "take_profit" if price >= entry * (1 + bot.TAKE_PROFIT) else None
                )
                if reason:
                    exit_price = snap["bid"]
                    gross = (exit_price - entry) * position["amount"]
                    fees = (entry * position["amount"] + exit_price * position["amount"]) * fee_rate
                    net = gross - fees
                    equity += net
                    daily_loss += max(0.0, -net)
                    equity_peak = max(equity_peak, equity)
                    max_drawdown = max(max_drawdown, equity_peak - equity)
                    trades.append({
                        "symbol": position["symbol"], "entry_time": position["entry_time"],
                        "exit_time": timestamp, "entry_price": entry, "exit_price": exit_price,
                        "gross_pnl_rials": round(gross, 2), "fees_rials": round(fees, 2),
                        "net_pnl_rials": round(net, 2), "exit_reason": reason,
                    })
                    position = None
            continue

        if daily_loss >= trade_amount * bot.DAILY_LOSS_LIMIT_PCT:
            # Same conservative policy: no new entries after the configured daily loss cap.
            continue

        best, _ = bot.select_best_market(snapshots, history)
        if best is not None:
            _score, symbol, snap, _trend, _momentum, _liquidity, _up_ratio, _volatility, eligible = best
            if eligible:
                entry_price = snap["ask"]
                position = {
                    "symbol": symbol, "entry_time": timestamp,
                    "entry_price": entry_price, "amount": trade_amount / entry_price,
                }

    # Do not mark an open position as a closed trade without an observed exit signal.
    wins = sum(1 for t in trades if t["net_pnl_rials"] > 0)
    losses = sum(1 for t in trades if t["net_pnl_rials"] <= 0)
    net_total = sum(t["net_pnl_rials"] for t in trades)
    return {
        "bars": len(rows),
        "closed_trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(wins / len(trades) * 100, 2) if trades else 0.0,
        "gross_pnl_rials": round(sum(t["gross_pnl_rials"] for t in trades), 2),
        "fees_rials": round(sum(t["fees_rials"] for t in trades), 2),
        "net_pnl_rials": round(net_total, 2),
        "max_realized_drawdown_rials": round(max_drawdown, 2),
        "open_position_at_end": position is not None,
        "open_position_symbol": position["symbol"] if position else None,
        "fee_pct_per_side": fee_pct,
        "trade_amount_rials": trade_amount,
        "trades": trades,
    }


def main():
    parser = argparse.ArgumentParser(description="Fee-aware offline backtest for Nobitex bot")
    parser.add_argument("csv_path", help="Historical order-book CSV file")
    parser.add_argument("--fee-pct", type=float, default=0.1, help="Estimated fee percent per side (default: 0.1)")
    parser.add_argument("--trade-amount", type=float, default=bot.TRADE_AMOUNT_RLS, help="Trade budget in Rials")
    parser.add_argument("--output", help="Optional JSON output file")
    args = parser.parse_args()
    report = run_backtest(load_snapshots(args.csv_path), args.fee_pct, args.trade_amount)
    encoded = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        Path(args.output).write_text(encoded + "\\n", encoding="utf-8")
    print(encoded)


if __name__ == "__main__":
    main()
