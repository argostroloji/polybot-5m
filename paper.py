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


def load_bankroll_stop() -> float:
    if os.path.exists(config.BANKROLL_STOP_FILE):
        with open(config.BANKROLL_STOP_FILE) as f:
            return json.load(f)["bankroll"]
    return config.STARTING_BANKROLL


def save_bankroll_stop(v: float):
    with open(config.BANKROLL_STOP_FILE, "w") as f:
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


def write_heartbeat(note: str, feed=None):
    hb = {
        "utc": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "bankroll_hold": load_bankroll(),
        "bankroll_stop": load_bankroll_stop(),
        "open_bets": len(load_pending()),
        "last": note,
        "last_eval": LAST_EVAL,
    }
    if feed is not None:
        hb["feed_price"] = feed.latest_price
        hb["feed_age_sec"] = (int(time.time()) - feed.latest_ts
                              if feed.latest_ts else None)
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
    """Return (best_ask_price, size_at_that_price) or (None, None).

    Size matters: in reality we can only buy as much as is actually offered at
    that price (liquidity), not an unlimited amount.
    """
    try:
        b = requests.get(f"{config.CLOB}/book", params={"token_id": token_id},
                        headers=HEADERS, timeout=15).json()
    except Exception:
        return None, None
    asks = b.get("asks", [])
    if not asks:
        return None, None
    best = min(asks, key=lambda x: float(x["price"]))
    return float(best["price"]), float(best["size"])


def best_bid(token_id: str):
    """Best bid price we could SELL into (to exit a position early)."""
    try:
        b = requests.get(f"{config.CLOB}/book", params={"token_id": token_id},
                        headers=HEADERS, timeout=15).json()
    except Exception:
        return None
    bids = b.get("bids", [])
    if not bids:
        return None
    return max(float(x["price"]) for x in bids)


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


def decide(feed: ChainlinkFeed, window_start: int, window_end: int, pending,
           window_open: dict):
    global LAST_EVAL
    if any(p.get("window_start") == window_start for p in pending):
        return False  # already acted this window

    # price-to-beat = the price we captured when this window opened (first loop
    # tick at/after the boundary), matching Polymarket's resolution rule.
    price_to_beat = window_open.get(window_start)
    cur = feed.price_now()
    if price_to_beat is None or cur is None:
        LAST_EVAL = f"window {window_start}: no price-to-beat (joined mid-window)"
        print(f"  [{window_start}] no price-to-beat yet, skip window")
        return False

    t_remaining = window_end - time.time()
    sig = sigma_per_sec(feed)
    move = cur - price_to_beat
    p_up = prob_up(move, t_remaining, sig)

    # Which side do we believe wins, and how confident?
    if p_up >= 0.5:
        side, token_idx, q = "Up", 0, p_up
    else:
        side, token_idx, q = "Down", 1, 1 - p_up

    summary = f"P(up)={p_up:.3f} move={move:+.1f} t={t_remaining:.0f}s"

    # Need high confidence (outcome near-settled) before we'll buy anything.
    if q < config.MIN_CONFIDENCE:
        LAST_EVAL = f"low conf {q:.2f} ({summary})"
        return False

    m = market_by_window(window_start)
    if not m:
        LAST_EVAL = f"window {window_start}: market not found"
        return False
    toks = ast.literal_eval(m["clobTokenIds"])
    ask, ask_size = best_ask(toks[token_idx])
    if ask is None:
        LAST_EVAL = f"{side}: no ask offered ({summary})"
        return False

    # REALISTIC LIMIT ORDER: we only buy if the market is offering the winning
    # side cheaper than our fair value minus the required edge. Our max price
    # is q - MIN_EDGE; we fill at the actual best ask (price-taker), and only
    # up to the size actually available (liquidity), capped by Kelly.
    max_price = q - config.MIN_EDGE
    if ask > max_price:
        LAST_EVAL = f"{side} ask {ask:.2f} > limit {max_price:.2f} ({summary})"
        return False

    bankroll = load_bankroll()
    target_usd = kelly_stake(q, ask, bankroll)
    shares_target = target_usd / ask
    shares = min(shares_target, ask_size)        # can't buy more than offered
    cost = shares * ask
    if cost < config.MIN_STAKE:
        LAST_EVAL = (f"{side}@{ask:.2f} liquidity ${cost:.2f}<min "
                     f"(ask_size={ask_size:.1f}) ({summary})")
        return False

    edge = q - ask
    LAST_EVAL = (f"FILL {side}@{ask:.2f} ${cost:.2f} edge={edge:+.3f} "
                 f"({summary})")
    pending.append({
        "decided_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "market": m["slug"],
        "window_start": window_start,
        "window_end": window_end,
        "side": side,
        "token_id": toks[token_idx],
        "entry_price": ask,
        "shares": round(shares, 4),
        "model_prob": round(q, 4),
        "edge": round(edge, 4),
        "price_to_beat": price_to_beat,
        "stake": round(cost, 4),
        # STOP-strategy bookkeeping (HOLD ignores these):
        "stopped": False,
        "stop_proceeds": None,
        "stop_price": None,
    })
    save_pending(pending)
    print(f"  window {dt.datetime.utcfromtimestamp(window_start):%H:%M} -> "
          f"FILL {side} {shares:.1f}sh @ {ask:.2f} = ${cost:.2f} "
          f"(P={q:.3f}, edge {edge:+.3f}, ask_size={ask_size:.1f})")
    return True


def monitor_stops(feed: ChainlinkFeed, pending, window_open: dict):
    """STOP strategy: for each open position still inside its window, if our
    side's win-prob has fallen below EXIT_PROB, 'sell' the shares at the best
    bid to cut the loss. Records the proceeds on the entry (HOLD ignores it).
    Returns True if any position changed."""
    now = time.time()
    sig = sigma_per_sec(feed)
    cur = feed.price_now()
    changed = False
    for p in pending:
        if p.get("stopped") or "window_end" not in p:
            continue
        if now >= p["window_end"] or cur is None:
            continue  # window over -> settlement handles it
        t_remaining = p["window_end"] - now
        move = cur - p["price_to_beat"]
        p_up = prob_up(move, t_remaining, sig)
        q_now = p_up if p["side"] == "Up" else 1 - p_up
        if q_now >= config.EXIT_PROB:
            continue  # still looks like a winner, hold
        bid = best_bid(p.get("token_id", ""))
        if bid is None:
            continue  # no buyer to sell into; can't exit, will settle
        p["stopped"] = True
        p["stop_price"] = bid
        p["stop_proceeds"] = round(p["shares"] * bid, 4)
        changed = True
        print(f"  STOP-EXIT {p['side']} {p['market']}: sold {p['shares']:.1f}sh "
              f"@ {bid:.2f} (q_now={q_now:.2f})")
    if changed:
        save_pending(pending)
    return changed


def resolve_due(feed: ChainlinkFeed, pending, window_open: dict):
    now = time.time()
    changed = False
    bankroll = load_bankroll()             # HOLD strategy
    bankroll_stop = load_bankroll_stop()   # STOP strategy
    still = []
    for p in pending:
        # skip malformed / old-format entries defensively
        if "window_end" not in p or "price_to_beat" not in p:
            continue
        if now < p["window_end"] + config.RESOLVE_BUFFER_SEC:
            still.append(p)
            continue
        # close price = the open price of the next window (= price at this
        # window's end boundary). Fall back to feed history / latest price.
        final = window_open.get(p["window_end"])
        if final is None:
            final = feed.price_at(p["window_end"])
        if final is None:
            final = feed.price_now()  # last resort
        if final is None:
            still.append(p)
            continue
        went_up = final >= p["price_to_beat"]
        won = (p["side"] == "Up" and went_up) or (p["side"] == "Down" and not went_up)
        cost = p["stake"]
        shares = p.get("shares", cost / p["entry_price"])

        # HOLD: hold to settlement. Shares pay $1 if won, $0 if lost.
        hold_pnl = ((shares - cost) if won else -cost) - config.TRADE_COST_USD

        # STOP: if we sold early, PnL = proceeds - cost; else same as HOLD.
        if p.get("stopped") and p.get("stop_proceeds") is not None:
            stop_pnl = p["stop_proceeds"] - cost - config.TRADE_COST_USD
        else:
            stop_pnl = hold_pnl

        bankroll += hold_pnl
        bankroll_stop += stop_pnl
        append_log({
            "decided_at": p["decided_at"],
            "market": p["market"],
            "side": p["side"],
            "model_prob": p["model_prob"],
            "entry_price": p["entry_price"],
            "shares": round(shares, 4),
            "edge": p["edge"],
            "cost_usd": round(cost, 4),
            "price_to_beat": p["price_to_beat"],
            "final_price": round(final, 2),
            "won": won,
            "stopped": bool(p.get("stopped")),
            "stop_price": p.get("stop_price"),
            "hold_pnl": round(hold_pnl, 4),
            "stop_pnl": round(stop_pnl, 4),
            "bankroll_hold": round(bankroll, 4),
            "bankroll_stop": round(bankroll_stop, 4),
        })
        print(f"  RESOLVED {p['side']} {p['market']}: won={won} "
              f"hold=${hold_pnl:+.2f} stop=${stop_pnl:+.2f} | "
              f"HOLD=${bankroll:.2f} STOP=${bankroll_stop:.2f}")
        changed = True
    if changed:
        save_bankroll(bankroll)
        save_bankroll_stop(bankroll_stop)
    save_pending(still)
    return changed


# ---------------- git autopush ----------------
def git_autopush():
    import subprocess

    def run(*a):
        return subprocess.run(["git", *a], capture_output=True, text=True)

    run("config", "user.name", "polybot")
    run("config", "user.email", "polybot@users.noreply.github.com")
    files = [f for f in (config.BANKROLL_FILE, config.BANKROLL_STOP_FILE,
                         LOG_FILE, PENDING_FILE, "heartbeat.json")
             if os.path.exists(f)]
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
    window_open = {}       # window_start -> price captured when window opened

    while time.time() < deadline:
        now = time.time()
        ws = int(now // config.WINDOW_SEC) * config.WINDOW_SEC
        we = ws + config.WINDOW_SEC

        # Capture this window's open price the first time we see it (the first
        # tick at/after the boundary), like Polymarket's price-to-beat.
        if ws not in window_open:
            pr = feed.price_now()
            if pr is not None:
                window_open[ws] = pr
            # prune old entries (keep ~30 min)
            window_open = {k: v for k, v in window_open.items() if k >= ws - 1800}

        pending = load_pending()
        state_changed = resolve_due(feed, pending, window_open)
        # STOP strategy: check open positions for early-exit each tick.
        if monitor_stops(feed, pending, window_open):
            state_changed = True

        # Hunt for a favorable fill across the back half of the window. We keep
        # checking every tick (until filled) instead of firing once — so we
        # take the cheap offer whenever it appears, no last-second race.
        entry_open = ws + config.ENTRY_START_SEC
        entry_close = we - config.ENTRY_CUTOFF_SEC
        already = any(p.get("window_start") == ws for p in pending)
        if not already and entry_open <= now < entry_close:
            if decide(feed, ws, we, pending, window_open):
                state_changed = True
            if one_shot:
                while time.time() < we + config.RESOLVE_BUFFER_SEC + 2:
                    n2 = time.time()
                    w2 = int(n2 // config.WINDOW_SEC) * config.WINDOW_SEC
                    if w2 not in window_open and feed.price_now() is not None:
                        window_open[w2] = feed.price_now()
                    time.sleep(2)
                resolve_due(feed, load_pending(), window_open)
                break

        write_heartbeat("ok", feed)
        if autopush and (state_changed or now - last_push > 60):
            git_autopush()
            last_push = now

        time.sleep(3)

    print("Done.")


if __name__ == "__main__":
    main()
