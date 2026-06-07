"""Show the paper bot's virtual portfolio: balance, P&L, win rate.

Usage:  python status.py
"""
import csv
import json
import os

import config


def main():
    start = config.STARTING_BANKROLL
    bankroll = start
    if os.path.exists(config.BANKROLL_FILE):
        with open(config.BANKROLL_FILE) as f:
            bankroll = json.load(f)["bankroll"]

    pending = []
    if os.path.exists("paper_pending.json"):
        with open("paper_pending.json") as f:
            pending = json.load(f)

    print("=" * 50)
    print(f"  Starting budget : ${start:,.2f}")
    print(f"  Current balance : ${bankroll:,.2f}")
    pnl = bankroll - start
    sign = "+" if pnl >= 0 else ""
    pct = pnl / start * 100 if start else 0
    print(f"  Total P&L       : {sign}${pnl:,.2f}  ({sign}{pct:.1f}%)")
    print(f"  Open paper bets : {len(pending)}")
    print("=" * 50)

    if not os.path.exists("paper_log.csv"):
        print("No resolved bets yet. Let it run and check back.")
        return

    with open("paper_log.csv") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print("No resolved bets yet.")
        return

    n = len(rows)
    wins = sum(1 for r in rows if r["won"] == "True")
    pnls = [float(r["pnl_usd"]) for r in rows]
    ups = sum(1 for r in rows if r["side"] == "Up")
    print(f"\n  Resolved bets   : {n}")
    print(f"  Wins / Losses   : {wins} / {n - wins}   ({wins / n * 100:.1f}% win)")
    print(f"  Avg P&L per bet : ${sum(pnls) / n:+.3f}")
    print(f"  Best / Worst    : ${max(pnls):+.2f} / ${min(pnls):+.2f}")
    print(f"  Up / Down bets  : {ups} / {n - ups}")

    print("\n  Last 5 bets:")
    for r in rows[-5:]:
        print(f"    {r['decided_at'][:19]}  {r['side']:>4} @ {r['entry_price']}"
              f"  won={r['won']:>5}  pnl=${float(r['pnl_usd']):+.2f}"
              f"  bal=${float(r['bankroll']):.2f}")


if __name__ == "__main__":
    main()
