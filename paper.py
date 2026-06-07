"""Polymarket BTC 5-minute LAST-SECOND paper-trading bot.

Strategy (from researched profitable bots):
  * Don't predict BTC 5 min ahead — that's a coin flip.
  * Wait until ~45s before each 5-minute window closes. By then the Chainlink
    oracle price has nearly settled the outcome.
  * Estimate P(up at settlement) with a Brownian model:
        P(up) = Phi( move / sigma_remaining )
    where move = current price - price-to-beat (window start price), and
    sigma_remaining is the $ volatility over the remaining seconds.
  * Polymarket's thin 5m order book lags, so the near-certain winning side is
    often still mispriced. If P(side) - ask > MIN_EDGE, buy it (paper).
  * Size with fractional Kelly, capped.

Price + resolution both come from Polymarket's own Chainlink oracle (the exact
settlement source) via the public RTDS websocket. No exchange, no API key.

Usage:
    python paper.py                       # run one window then exit (debug)
    python paper.py --minutes 290 --autopush   # CI long loop
"""
import csv
import json
import math
import os
import sys
import time
import datetime as dt

import requests

import config
from feed import ChainlinkFeed

os.chdir(os.path.dirname(os.path.abspath(__file__)))

HEADERS = {"User-Agent": "polybot"}
LOG_FILE = "paper_log.csv"
PENDING_FILE = "paper_pending.json"


# ---------------- normal CDF ----------------
def phi(z: float) -> float:
    return 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))


# ---------------- state ----------------
def load_bankroll() -> float:
    if os.path.exists(config.BANKROLL_FILE):
        with open(config.BANKROLL_FILE) as f:
            return json.load(f)["bankroll"]
    return config.STARTING_BANKROLL


def save_bankroll(v: float):
    with open(config.BANKROLL_FILE, "w") as f:
        json.dump({"bankroll": round(v, 4)}, f)


def load_pending():
    if os.path.exists(PENDING_FILE):
        with open(PENDING_FILE) as f:
            return json.load(f)
    return []


def save_pending(p):
    with open(PENDING_FILE, "w") as f:
        json.dump(p, f, indent=2)


def append_log(row: dict):
    exists = os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(row.keys()))
        if not exists:
            w.writeheader()
        w.writerow(row)


LAST_EVAL = "starting up"


def write_heartbeat(note: str):
    hb = {
        "utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "bankroll": load_bankroll(),
        "open_bets": len(load_pending()),
        "last": note,
        "last_eval": LAST_EVAL,
    }
    with open("heartbeat.json", "w") as f:
        json.dump(hb, f, indent=2)


# ---------------- Polymarket market lookup ----------------
def market_by_window(window_start: int):
    """Fetch the 5m up/down market for a given window start unix ts."""
    slug = f"{config.MARKET_SLUG_PREFIX}-{window_start}"
    try:
        r = requests.get(f"{config.GAMMA}/markets", params={"slug": slug},
                         headers=HEADERS, timeout=15)
        d = r.json()
    except Exception:
        return None
    if not d:
        return None
    return d[0]


def best_ask(token_id: str):
    try:
        b = requests.get(f"{config.CLOB}/book", params={"token_id": token_id},
                        headers=HEADERS, timeout=15).json()
    except Exception:
        return None
    asks = [float(x["price"]) for x in b.get("asks", [])]
    return min(asks) if asks else None


# ---------------- volatility + probability ----------------
def sigma_per_sec(feed: ChainlinkFeed) -> float:
    prices = feed.recent_prices(config.VOL_LOOKBACK_SEC)
    if len(prices) < 5:
        return config.MIN_SIGMA
    diffs = [prices[i] - prices[i - 1] for i in range(1, len(prices))]
    mean = sum(diffs) / len(diffs)
    var = sum((d - mean) ** 2 for d in diffs) / max(len(diffs) - 1, 1)
    return max(math.sqrt(var), config.MIN_SIGMA)


def prob_up(move: float, t_remaining: float, sig_sec: float) -> float:
    sigma_rem = sig_sec * math.sqrt(max(t_remaining, 1.0))
    return phi(move / sigma_rem)


# ---------------- sizing ----------------
def kelly_stake(q: float, ask: float, bankroll: float) -> float:
    """Fractional Kelly for a binary contract bought at price `ask` (pays 1)."""
    b = (1.0 - ask) / ask
    f = q - (1.0 - q) / b
    f = max(0.0, f) * config.KELLY_FRACTION
    stake = min(f * bankroll, config.MAX_STAKE_FRAC * bankroll)
    return stake


# ---------------- core: decide + resolve ----------------
import ast


def decide(feed: ChainlinkFeed, window_start: int, window_end: int, pending):
    global LAST_EVAL
    if any(p.get("window_start") == window_start for p in pending):
        return False  # already acted this window

    price_to_beat = feed.price_at(window_start)
    cur = feed.price_now()
    if price_to_beat is None or cur is None:
        LAST_EVAL = f"window {window_start}: no price-to-beat (feed gap)"
        print(f"  [{window_start}] no price-to-beat yet, skip window")
        return False

    t_remaining = window_end - time.time()
    sig = sigma_per_sec(feed)
    move = cur - price_to_beat
    p_up = prob_up(move, t_remaining, sig)

    m = market_by_window(window_start)
    if not m:
        LAST_EVAL = f"window {window_start}: market not found"
        print(f"  [{window_start}] market not found, skip")
        return False
    toks = ast.literal_eval(m["clobTokenIds"])
    up_ask = best_ask(toks[0])
    down_ask = best_ask(toks[1])

    summary = (f"P(up)={p_up:.3f} move={move:+.1f} t={t_remaining:.0f}s "
               f"UpAsk={up_ask} DownAsk={down_ask}")
    print(f"  window {dt.datetime.utcfromtimestamp(window_start):%H:%M} | "
          f"beat={price_to_beat:.1f} cur={cur:.1f} move={move:+.1f} "
          f"sig/s={sig:.2f} t={t_remaining:.0f}s | P(up)={p_up:.3f} "
          f"| Up ask={up_ask} Down ask={down_ask}")

    edge_up = (p_up - up_ask) if up_ask else -1
    edge_down = ((1 - p_up) - down_ask) if down_ask else -1

    if edge_up >= edge_down and edge_up > config.MIN_EDGE:
        side, ask, q, edge = "Up", up_ask, p_up, edge_up
    elif edge_down > config.MIN_EDGE:
        side, ask, q, edge = "Down", down_ask, 1 - p_up, edge_down
    else:
        LAST_EVAL = f"no edge ({summary})"
        print("  -> no edge, skip")
        return False

    bankroll = load_bankroll()
    stake = kelly_stake(q, ask, bankroll)
    if stake < config.MIN_STAKE:
        LAST_EVAL = f"stake<min ({summary})"
        print(f"  -> Kelly stake ${stake:.2f} < min, skip")
        return False
    LAST_EVAL = f"BET {side}@{ask} edge={edge:+.3f} ({summary})"

    pending.append({
        "decided_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "market": m["slug"],
        "window_start": window_start,
        "window_end": window_end,
        "side": side,
        "entry_price": ask,
        "model_prob": round(q, 4),
        "edge": round(edge, 4),
        "price_to_beat": price_to_beat,
        "stake": round(stake, 2),
    })
    save_pending(pending)
    print(f"  -> PAPER BET {side} @ {ask} stake ${stake:.2f} "
          f"(P={q:.3f}, edge {edge:+.3f})")
    return True


def resolve_due(feed: ChainlinkFeed, pending):
    now = time.time()
    changed = False
    bankroll = load_bankroll()
    still = []
    for p in pending:
        # skip malformed / old-format entries defensively
        if "window_end" not in p or "price_to_beat" not in p:
            continue
        if now < p["window_end"] + config.RESOLVE_BUFFER_SEC:
            still.append(p)
            continue
        final = feed.price_at(p["window_end"])
        if final is None:
            final = feed.price_now()  # fallback
        if final is None:
            still.append(p)
            continue
        went_up = final >= p["price_to_beat"]
        won = (p["side"] == "Up" and went_up) or (p["side"] == "Down" and not went_up)
        stake = p["stake"]
        pnl = stake * (1.0 - p["entry_price"]) / p["entry_price"] if won else -stake
        bankroll += pnl
        append_log({
            "decided_at": p["decided_at"],
            "market": p["market"],
            "side": p["side"],
            "model_prob": p["model_prob"],
            "entry_price": p["entry_price"],
            "edge": p["edge"],
            "stake": stake,
            "price_to_beat": p["price_to_beat"],
            "final_price": round(final, 2),
            "went_up": went_up,
            "won": won,
            "pnl_usd": round(pnl, 4),
            "bankroll": round(bankroll, 4),
        })
        print(f"  RESOLVED {p['side']} {p['market']}: beat={p['price_to_beat']:.1f} "
              f"final={final:.1f} won={won} pnl=${pnl:+.2f} bankroll=${bankroll:.2f}")
        changed = True
    if changed:
        save_bankroll(bankroll)
        save_pending(still)
    else:
        save_pending(still)
    return changed


# ---------------- git autopush ----------------
def git_autopush():
    import subprocess

    def run(*a):
        return subprocess.run(["git", *a], capture_output=True, text=True)

    run("config", "user.name", "polybot")
    run("config", "user.email", "polybot@users.noreply.github.com")
    files = [f for f in (config.BANKROLL_FILE, LOG_FILE, PENDING_FILE,
                         "heartbeat.json") if os.path.exists(f)]
    run("add", *files)
    if run("diff", "--cached", "--quiet").returncode == 0:
        return
    run("commit", "-m", "paper: update state [skip ci]")
    run("pull", "--rebase", "--autostash", "origin", "main")
    push = run("push")
    if push.returncode != 0:
        print("  git push err:", (push.stderr + push.stdout).strip()[:200])
    else:
        print("  git push OK")


def _arg(flag, default):
    if flag in sys.argv:
        try:
            return sys.argv[sys.argv.index(flag) + 1]
        except (IndexError, ValueError):
            pass
    return default


# ---------------- main loop ----------------
def main():
    autopush = "--autopush" in sys.argv
    one_shot = ("--minutes" not in sys.argv) and ("--loop" not in sys.argv)
    minutes = int(_arg("--minutes", 290))
    deadline = time.time() + (minutes * 60 if not one_shot else 600)

    feed = ChainlinkFeed()
    feed.start()
    print("Connecting to Chainlink feed...")
    if not feed.wait_ready(25):
        print("Feed not ready, exiting.")
        return
    print(f"Feed live. price={feed.price_now():.1f}  "
          f"(autopush={autopush}, one_shot={one_shot})")

    last_push = 0.0
    handled = set()  # window_starts we've already decided on

    while time.time() < deadline:
        pending = load_pending()
        state_changed = resolve_due(feed, pending)

        now = time.time()
        ws = int(now // config.WINDOW_SEC) * config.WINDOW_SEC
        we = ws + config.WINDOW_SEC
        decision_t = we - config.DECISION_LEAD_SEC

        if ws not in handled and decision_t <= now < we - 2:
            if decide(feed, ws, we, pending):
                state_changed = True
            handled.add(ws)
            if one_shot:
                # wait for this window to resolve, then exit
                while time.time() < we + config.RESOLVE_BUFFER_SEC + 2:
                    time.sleep(2)
                resolve_due(feed, load_pending())
                break

        write_heartbeat("ok")
        if autopush and (state_changed or now - last_push > 60):
            git_autopush()
            last_push = now

        # prune handled set
        handled = {w for w in handled if w >= ws - config.WINDOW_SEC}
        time.sleep(5)

    print("Done.")


if __name__ == "__main__":
    main()
