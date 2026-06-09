"""Compare the two paper strategies: HOLD vs STOP-loss.

Usage:  python status.py
"""
import csv
import json
import os

import config


def _bankroll(path):
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)["bankroll"]
    return config.STARTING_BANKROLL


def main():
    start = config.STARTING_BANKROLL
    hold = _bankroll(config.BANKROLL_FILE)
    stop = _bankroll(config.BANKROLL_STOP_FILE)

    pending = []
    if os.path.exists("paper_pending.json"):
        with open("paper_pending.json") as f:
            pending = json.load(f)

    print("=" * 56)
    print(f"  Starting budget : ${start:,.2f}")
    print(f"  HOLD strategy   : ${hold:,.2f}   ({hold-start:+.2f}, {(hold-start)/start*100:+.1f}%)")
    print(f"  STOP strategy   : ${stop:,.2f}   ({stop-start:+.2f}, {(stop-start)/start*100:+.1f}%)")
    print(f"  Open positions  : {len(pending)}")
    print("=" * 56)

    if not os.path.exists("paper_log.csv"):
        print("No resolved trades yet.")
        return
    with open("paper_log.csv") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print("No resolved trades yet.")
        return

    n = len(rows)
    wins = sum(1 for r in rows if r.get("won") == "True")
    stops = sum(1 for r in rows if r.get("stopped") == "True")
    hold_pnl = sum(float(r.get("hold_pnl", 0)) for r in rows)
    stop_pnl = sum(float(r.get("stop_pnl", 0)) for r in rows)

    print(f"\n  Trades          : {n}")
    print(f"  Settle win rate : {wins/n*100:.1f}%  ({wins}W / {n-wins}L)")
    print(f"  Early exits     : {stops}  (STOP sold before settlement)")
    print(f"  HOLD total P&L  : ${hold_pnl:+.2f}")
    print(f"  STOP total P&L  : ${stop_pnl:+.2f}")
    better = "HOLD" if hold_pnl >= stop_pnl else "STOP"
    print(f"  --> Better so far: {better}")

    print("\n  Last 6 trades:")
    for r in rows[-6:]:
        st = "STOP" if r.get("stopped") == "True" else "hold"
        print(f"    {r['side']:>4} @ {r['entry_price']}  won={r.get('won'):>5} "
              f"[{st}]  hold={float(r.get('hold_pnl',0)):+.2f} "
              f"stop={float(r.get('stop_pnl',0)):+.2f}")


if __name__ == "__main__":
    main()
