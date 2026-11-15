#!/usr/bin/env python3
"""
Paper-copy a Hyperliquid wallet. No keys, no real orders, read-only API only.

Every tick it reads the target wallet's open perp positions, scales them to
your paper capital, and "trades" at the current mid price plus slippage and
fees. State is saved to a JSON file so you can stop and restart it.

Usage:
    python3 hl_paper_copy.py 0xWALLET --capital 1000
    python3 hl_paper_copy.py 0xWALLET --capital 1000 --interval 60
    python3 hl_paper_copy.py 0xWALLET --once          # one tick, for cron

Useful options:
    --max-lev 3        cap total paper leverage (notional / equity)
    --min-share 0.01   ignore positions under 1% of their total notional
    --slip-bps 5       assumed slippage per fill
    --fee-bps 4.5      assumed taker fee per fill
    --reset            delete saved state and start over

Limits: it only sees positions, not spot holdings that might hedge them, and
it checks at an interval, so very fast trades are missed. Real results would
be worse than this simulation, not better.
"""
import argparse
import csv
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

API = "https://api.hyperliquid.xyz/info"


def post(payload):
    req = urllib.request.Request(
        API, data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"HTTP {e.code} for {payload.get('type')}: {body}")


def target_state(addr):
    st = post({"type": "clearinghouseState", "user": addr})
    acct = float(st["marginSummary"]["accountValue"])
    pos = {}
    for ap in st.get("assetPositions", []):
        p = ap["position"]
        pos[p["coin"]] = float(p["szi"])
    return acct, pos


def apply_fill(state, coin, delta, px, fee_bps):
    """Apply a signed size change at price px. Updates realized cash."""
    pos = state["positions"].get(coin, {"size": 0.0, "entry": px})
    s, e = pos["size"], pos["entry"]
    realized = 0.0
    if s == 0 or (s > 0) == (delta > 0):
        new_s = s + delta
        e = (abs(s) * e + abs(delta) * px) / abs(new_s) if new_s else px
    else:
        closing = min(abs(delta), abs(s))
        realized = closing * (px - e) * (1 if s > 0 else -1)
        new_s = s + delta
        if abs(delta) > abs(s):
            e = px  # flipped, remainder opened at px
    fee = abs(delta) * px * fee_bps / 1e4
    state["cash"] += realized - fee
    state["fees"] += fee
    state["realized"] += realized
    if abs(new_s) < 1e-12:
        state["positions"].pop(coin, None)
    else:
        state["positions"][coin] = {"size": new_s, "entry": e}
    return realized, fee


def equity_and_notional(state, mids):
    eq, notional = state["cash"], 0.0
    for coin, p in state["positions"].items():
        m = float(mids.get(coin, p["entry"]))
        eq += p["size"] * (m - p["entry"])
        notional += abs(p["size"]) * m
    return eq, notional


def tick(args, state):
    their_acct, their_pos = target_state(args.address)
    mids = post({"type": "allMids"})
    price = lambda c: float(mids[c]) if c in mids else None

    their_notional = sum(abs(sz) * price(c) for c, sz in their_pos.items()
                         if price(c))
    # Ignore tiny positions in their book
    wanted = {}
    for c, sz in their_pos.items():
        px = price(c)
        if px and abs(sz) * px >= args.min_share * their_notional:
            wanted[c] = sz

    eq, _ = equity_and_notional(state, mids)
    if eq <= 0:
        state["dead"] = True
        return their_acct, eq, 0.0, []
    scale = eq / their_acct if their_acct > 0 else 0.0
    targets = {c: sz * scale for c, sz in wanted.items()}

    # Leverage cap
    tgt_notional = sum(abs(sz) * price(c) for c, sz in targets.items())
    if eq > 0 and tgt_notional > args.max_lev * eq:
        f = args.max_lev * eq / tgt_notional
        targets = {c: sz * f for c, sz in targets.items()}

    trades = []
    for coin in set(targets) | set(state["positions"]):
        px = price(coin)
        if not px:
            continue
        cur = state["positions"].get(coin, {"size": 0.0})["size"]
        tgt = targets.get(coin, 0.0)
        delta = tgt - cur
        dn = abs(delta) * px
        closing_all = tgt == 0.0 and cur != 0.0
        drift_ok = abs(delta) >= args.rebalance * max(abs(tgt), abs(cur))
        if dn < args.min_trade and not closing_all:
            continue
        if not (closing_all or cur == 0.0 or drift_ok):
            continue
        fill_px = px * (1 + args.slip_bps / 1e4 * (1 if delta > 0 else -1))
        realized, fee = apply_fill(state, coin, delta, fill_px, args.fee_bps)
        trades.append((coin, delta, fill_px, realized, fee))

    eq, notional = equity_and_notional(state, mids)
    return their_acct, eq, notional, trades


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("address")
    ap.add_argument("--capital", type=float, default=1000.0)
    ap.add_argument("--max-lev", type=float, default=3.0)
    ap.add_argument("--min-share", type=float, default=0.01)
    ap.add_argument("--min-trade", type=float, default=10.0,
                    help="ignore paper trades smaller than this many USD")
    ap.add_argument("--rebalance", type=float, default=0.10,
                    help="only resize a position if off target by this fraction")
    ap.add_argument("--slip-bps", type=float, default=5.0)
    ap.add_argument("--fee-bps", type=float, default=4.5)
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()

    args.address = args.address.strip().strip("<>\"'").lower()
    if not re.fullmatch(r"0x[0-9a-f]{40}", args.address):
        sys.exit("Invalid address: need the full 0x + 40 hex characters.")

    base = f"paper_{args.address[:8]}"
    state_file, log_file = base + "_state.json", base + "_log.csv"
    if args.reset:
        for f in (state_file, log_file):
            if os.path.exists(f):
                os.remove(f)
    if os.path.exists(state_file):
        state = json.load(open(state_file))
    else:
        state = {"cash": args.capital, "capital": args.capital,
                 "positions": {}, "fees": 0.0, "realized": 0.0,
                 "started": time.time(), "dead": False}

    print(f"Paper-copying {args.address}  capital {state['capital']:,.2f} USDC  "
          f"max lev {args.max_lev}x  state: {state_file}")
    new_log = not os.path.exists(log_file)
    while True:
        try:
            their_acct, eq, notional, trades = tick(args, state)
        except Exception as e:
            print(f"[{datetime.now():%H:%M:%S}] error: {e}", file=sys.stderr)
            if args.once:
                sys.exit(1)
            time.sleep(args.interval)
            continue
        json.dump(state, open(state_file, "w"), indent=1)
        ret = (eq / state["capital"] - 1) * 100
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        print(f"[{now}] equity {eq:,.2f} ({ret:+.2f}%)  notional {notional:,.0f}  "
              f"lev {notional / eq if eq > 0 else 0:.2f}x  fees {state['fees']:.2f}  "
              f"positions {len(state['positions'])}")
        for coin, d, px, rl, fee in trades:
            print(f"    {'BUY ' if d > 0 else 'SELL'} {coin:8} {abs(d):.6g} @ {px:.6g}"
                  f"  realized {rl:+.2f}  fee {fee:.2f}")
        with open(log_file, "a", newline="") as fh:
            w = csv.writer(fh)
            if new_log:
                w.writerow(["time_utc", "equity", "return_pct", "notional",
                            "fees", "their_account_value", "n_trades"])
                new_log = False
            w.writerow([now, f"{eq:.2f}", f"{ret:.3f}", f"{notional:.0f}",
                        f"{state['fees']:.2f}", f"{their_acct:.0f}", len(trades)])
        if state.get("dead"):
            print("Paper account wiped out (equity <= 0). Use --reset to restart.")
            break
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
