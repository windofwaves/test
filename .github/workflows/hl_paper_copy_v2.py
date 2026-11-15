#!/usr/bin/env python3
"""
Paper-copy a Hyperliquid wallet, v2. No keys, no real orders, read-only API.

What is different from v1:
  * Hedge netting: the wallet holds spot tokens against some perp shorts
    (for example HYPE, XPL, FARTCOIN, PURR). v2 copies NET exposure
    (perp position plus spot holding), not the perp leg alone.
  * Fixed-scale sizing: your position sizes are a fixed fraction of the
    wallet's net sizes (set on the first tick, then grown or shrunk with your
    own equity). The wallet's frequent transfers in and out no longer change
    your size, but when it trims or adds, you trim or add proportionally.
  * Kill switch: --max-dd flattens everything and stops if equity falls this
    far below its peak (tests a rule you could really use).
  * Extra log (paper_<wallet><tag>_book.csv) showing how much the wallet's
    book changed at each check, its hedges, and equity after exit costs.
  * --tag lets you run this next to v1 with separate state files.

Usage:
    python3 hl_paper_copy_v2.py 0xWALLET --capital 1000 --tag _v2
    python3 hl_paper_copy_v2.py 0xWALLET --capital 1000 --tag _v2 --once

Limits: positions and spot balances are sampled at an interval, so fast trades
between checks are missed. Real results would be worse than this simulation.
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
SPOT_ALIASES = {"UFART": "FARTCOIN"}   # Unit-wrapped spot tokens whose names differ from the perp


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


def fnum(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def perp_for_spot(name, pos):
    """Which perp position (if any) a spot token hedges."""
    if name in SPOT_ALIASES:
        c = SPOT_ALIASES[name]
        return c if c in pos else None
    if name in pos:
        return name
    if name.startswith("U") and name[1:] in pos:
        return name[1:]
    return None


def target_state(addr, use_spot):
    st = post({"type": "clearinghouseState", "user": addr})
    acct = fnum(st["marginSummary"]["accountValue"])
    pos = {a["position"]["coin"]: fnum(a["position"]["szi"]) for a in st.get("assetPositions", [])}
    hedge = {}
    if use_spot:
        try:
            ss = post({"type": "spotClearinghouseState", "user": addr})
            for b in ss.get("balances", []):
                c = perp_for_spot(b["coin"], pos)
                if c:
                    hedge[c] = hedge.get(c, 0.0) + fnum(b["total"])
        except Exception as e:
            print(f"  warning: spot balances unavailable, using perp only ({e})", file=sys.stderr)
    net = {c: sz + hedge.get(c, 0.0) for c, sz in pos.items()}
    return acct, pos, hedge, net


def apply_fill(state, coin, delta, px, fee_bps):
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
            e = px
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
        m = fnum(mids.get(coin), p["entry"])
        eq += p["size"] * (m - p["entry"])
        notional += abs(p["size"]) * m
    return eq, notional


def tick(args, state):
    their_acct, pos, hedge, net = target_state(args.address, not args.no_hedge)
    mids = post({"type": "allMids"})
    price = lambda c: fnum(mids.get(c)) or None

    net_notional = {c: abs(sz) * price(c) for c, sz in net.items() if price(c)}
    tot_net = sum(net_notional.values())
    perp_gross = sum(abs(sz) * price(c) for c, sz in pos.items() if price(c))

    # How much did the wallet's net book change since the last check?
    prev = state.get("prev_net")
    change = None
    if prev is not None and tot_net:
        delta = sum(abs(net.get(c, 0.0) - prev.get(c, 0.0)) * (price(c) or 0.0)
                    for c in set(net) | set(prev))
        change = delta / tot_net * 100
    state["prev_net"] = net

    eq, _ = equity_and_notional(state, mids)
    if eq <= 0:
        state["dead"] = True
        return dict(their_acct=their_acct, eq=eq, notional=0.0, trades=[], tot_net=tot_net,
                    perp_gross=perp_gross, n_pos=len(pos), change=change, hedge=hedge, eq_exit=eq)

    state["peak"] = max(state.get("peak", state["capital"]), eq)
    flatten = False
    if args.max_dd > 0 and eq <= state["peak"] * (1 - args.max_dd):
        flatten = True
        state["halted"] = True

    if state.get("scale0") is None and tot_net > 0:
        state["scale0"] = state["capital"] * args.max_lev / tot_net
    scale = (state.get("scale0") or 0.0) * (eq / state["capital"])

    wanted = {c: sz for c, sz in net.items()
              if price(c) and abs(sz) * price(c) >= args.min_share * tot_net}
    targets = {} if flatten else {c: sz * scale for c, sz in wanted.items()}

    tgt_notional = sum(abs(sz) * price(c) for c, sz in targets.items())
    if tgt_notional > args.max_lev * eq > 0:
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
    eq_exit = eq - notional * (args.slip_bps + args.fee_bps) / 1e4
    return dict(their_acct=their_acct, eq=eq, notional=notional, trades=trades, tot_net=tot_net,
                perp_gross=perp_gross, n_pos=len(pos), change=change, hedge=hedge, eq_exit=eq_exit)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("address")
    ap.add_argument("--capital", type=float, default=1000.0)
    ap.add_argument("--max-lev", type=float, default=2.0,
                    help="gross exposure / equity you copy at (default 2)")
    ap.add_argument("--max-dd", type=float, default=0.25,
                    help="flatten and stop at this drawdown from peak; 0 disables")
    ap.add_argument("--min-share", type=float, default=0.01)
    ap.add_argument("--min-trade", type=float, default=10.0)
    ap.add_argument("--rebalance", type=float, default=0.10)
    ap.add_argument("--slip-bps", type=float, default=5.0)
    ap.add_argument("--fee-bps", type=float, default=4.5)
    ap.add_argument("--interval", type=int, default=60)
    ap.add_argument("--no-hedge", action="store_true", help="ignore spot holdings (perp only)")
    ap.add_argument("--tag", default="_v2", help="suffix for the state and log files")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--reset", action="store_true")
    args = ap.parse_args()

    args.address = args.address.strip().strip("<>\"'").lower()
    if not re.fullmatch(r"0x[0-9a-f]{40}", args.address):
        sys.exit("Invalid address: need the full 0x + 40 hex characters.")

    base = f"paper_{args.address[:8]}{args.tag}"
    state_file, log_file, book_file = base + "_state.json", base + "_log.csv", base + "_book.csv"
    if args.reset:
        for f in (state_file, log_file, book_file):
            if os.path.exists(f):
                os.remove(f)
    if os.path.exists(state_file):
        state = json.load(open(state_file))
    else:
        state = {"cash": args.capital, "capital": args.capital, "positions": {},
                 "fees": 0.0, "realized": 0.0, "started": time.time(), "dead": False}
    if state.get("halted"):
        sys.exit(f"Halted earlier by the drawdown stop (state: {state_file}). Use --reset to restart.")

    print(f"Paper-copying {args.address}  capital {state['capital']:,.2f}  max lev {args.max_lev}x  "
          f"stop {args.max_dd:.0%}  hedge netting {'off' if args.no_hedge else 'on'}  state: {state_file}")
    new_log, new_book = not os.path.exists(log_file), not os.path.exists(book_file)
    while True:
        try:
            r = tick(args, state)
        except Exception as e:
            print(f"[{datetime.now():%H:%M:%S}] error: {e}", file=sys.stderr)
            if args.once:
                sys.exit(1)
            time.sleep(args.interval)
            continue
        json.dump(state, open(state_file, "w"), indent=1)
        eq, notional = r["eq"], r["notional"]
        ret = (eq / state["capital"] - 1) * 100
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
        chg = "n/a" if r["change"] is None else f"{r['change']:.2f}%"
        print(f"[{now}] equity {eq:,.2f} ({ret:+.2f}%)  notional {notional:,.0f}  "
              f"lev {notional / eq if eq > 0 else 0:.2f}x  fees {state['fees']:.2f}  "
              f"positions {len(state['positions'])}  their book change {chg}")
        for coin, d, px, rl, fee in r["trades"]:
            print(f"    {'BUY ' if d > 0 else 'SELL'} {coin:8} {abs(d):.6g} @ {px:.6g}  "
                  f"realized {rl:+.2f}  fee {fee:.2f}")
        with open(log_file, "a", newline="") as fh:
            w = csv.writer(fh)
            if new_log:
                w.writerow(["time_utc", "equity", "return_pct", "notional", "fees",
                            "their_account_value", "n_trades"])
                new_log = False
            w.writerow([now, f"{eq:.2f}", f"{ret:.3f}", f"{notional:.0f}", f"{state['fees']:.2f}",
                        f"{r['their_acct']:.0f}", len(r["trades"])])
        with open(book_file, "a", newline="") as fh:
            w = csv.writer(fh)
            if new_book:
                w.writerow(["time_utc", "their_perp_gross", "their_net_exposure", "n_positions",
                            "book_change_pct", "hedged_coins", "equity_after_exit_costs"])
                new_book = False
            w.writerow([now, f"{r['perp_gross']:.0f}", f"{r['tot_net']:.0f}", r["n_pos"],
                        "" if r["change"] is None else f"{r['change']:.3f}",
                        ";".join(f"{c}:{v:.0f}" for c, v in sorted(r["hedge"].items())),
                        f"{r['eq_exit']:.2f}"])
        if state.get("halted"):
            print(f"Drawdown stop hit: positions flattened at equity {eq:,.2f}. Stopped.")
            json.dump(state, open(state_file, "w"), indent=1)
            break
        if state.get("dead"):
            print("Paper account wiped out (equity <= 0). Use --reset to restart.")
            break
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
