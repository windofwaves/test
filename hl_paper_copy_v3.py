#!/usr/bin/env python3
"""
Paper-copy a Hyperliquid wallet, v2. No keys, no real orders, read-only API.

Copying logic
  * Hedge netting: copies NET exposure (perp position plus the spot tokens the
    wallet holds against it, e.g. HYPE, XPL, FARTCOIN, PURR), not the perp leg alone.
  * Fixed-scale sizing: your sizes are a fixed fraction of the wallet's net sizes
    (set on the first tick, then scaled with your own equity). Its transfers in
    and out don't change your size; its trims and adds do.
  * Funding: paper funding is accrued from live funding rates (shorts earn when
    the rate is positive), using the current rate each check (approximate).
  * Drawdown stop: --max-dd flattens everything and stops at that drawdown.

Monitoring
  * Live colored dashboard in a terminal (equity, drawdown, sparkline, positions
    vs target, wallet book, tape of the wallet's recent fills, run statistics).
  * It tells you when the wallet's net book last changed, and how many fills it
    makes, so you can see "static book" versus "busy but net-flat".
  * --plain (or --once, or a non-terminal such as cron) prints simple log lines.

Usage:
    python3 hl_paper_copy_v2.py 0xWALLET --capital 1000 --reset
    python3 hl_paper_copy_v2.py 0xWALLET --plain
    python3 hl_paper_copy_v2.py 0xWALLET --once

Files written: paper_<wallet><tag>_state.json, _log.csv, _book.csv
Limits: sampled at an interval, so trades that open and close between checks are
missed. Real results would be worse than this simulation, not better.
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
from collections import deque
from datetime import datetime, timezone

API = "https://api.hyperliquid.xyz/info"
SPOT_ALIASES = {"UFART": "FARTCOIN"}   # Unit-wrapped spot tokens whose names differ from the perp
RECENT = deque(maxlen=8)               # recent wallet fills, for the dashboard tape
FUND_CACHE = {"t": 0.0, "rates": {}}


# ------------------------------------------------------------------ helpers
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


def money(x):
    a = abs(x)
    if a >= 1e9:
        return f"{x / 1e9:.2f}B"
    if a >= 1e6:
        return f"{x / 1e6:.1f}M"
    if a >= 1e3:
        return f"{x / 1e3:.0f}K"
    return f"{x:,.0f}"


def make_c(enabled):
    codes = {"g": "32", "r": "31", "y": "33", "c": "36", "d": "2", "b": "1", "m": "35"}

    def c(txt, k):
        return f"\033[{codes[k]}m{txt}\033[0m" if enabled else str(txt)
    return c


def sgn(c, v, txt):
    return c(txt, "g" if v > 0 else "r" if v < 0 else "d")


def spark(vals):
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1.0
    return "".join("▁▂▃▄▅▆▇█"[min(7, int((v - lo) / rng * 7.999))] for v in vals)


# --------------------------------------------------------------- wallet data
def perp_for_spot(name, pos):
    """Which perp position (if any) a spot token hedges."""
    if name in SPOT_ALIASES:
        t = SPOT_ALIASES[name]
        return t if t in pos else None
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
                t = perp_for_spot(b["coin"], pos)
                if t:
                    hedge[t] = hedge.get(t, 0.0) + fnum(b["total"])
        except Exception as e:
            print(f"  warning: spot balances unavailable, using perp only ({e})", file=sys.stderr)
    net = {c: sz + hedge.get(c, 0.0) for c, sz in pos.items()}
    return acct, pos, hedge, net


def funding_rates():
    if time.time() - FUND_CACHE["t"] > 120:
        try:
            meta, ctxs = post({"type": "metaAndAssetCtxs"})
            FUND_CACHE["rates"] = {u["name"]: fnum(x.get("funding")) for u, x in zip(meta["universe"], ctxs)}
            FUND_CACHE["t"] = time.time()
        except Exception as e:
            print(f"  warning: funding rates unavailable ({e})", file=sys.stderr)
    return FUND_CACHE["rates"]


def accrue_funding(state, mids):
    """Accrue paper funding since the last tick (hourly rate x elapsed hours)."""
    now = time.time()
    last = state.get("last_ts")
    state["last_ts"] = now
    if last is None:
        return 0.0
    dt_h = min((now - last) / 3600.0, 0.5)
    rates = funding_rates()
    pay = 0.0
    for coin, p in state["positions"].items():
        rate, m = rates.get(coin), fnum(mids.get(coin))
        if rate is None or not m:
            continue
        pay += -p["size"] * m * rate * dt_h   # positive rate: longs pay, shorts receive
    state["cash"] += pay
    state["funding"] = state.get("funding", 0.0) + pay
    return pay


def poll_fills(addr, state, now_ms):
    """Count the wallet's new fills since the last check (activity monitor)."""
    if state.get("last_fill_ms") is None:
        state["last_fill_ms"] = now_ms
        return 0, 0.0
    cursor, new = state["last_fill_ms"] + 1, []
    for _ in range(5):
        pl = {"type": "userFillsByTime", "user": addr, "startTime": cursor,
              "endTime": now_ms, "aggregateByTime": True}
        try:
            b = post(pl)
        except RuntimeError:
            pl.pop("aggregateByTime")
            b = post(pl)
        if not b:
            break
        new.extend(b)
        if len(b) < 2000:
            break
        cursor = max(f["time"] for f in b) + 1
    if new:
        state["last_fill_ms"] = max(f["time"] for f in new)
    act = state.setdefault("act", {"fills": 0, "volume": 0.0, "realized": 0.0, "by_coin": {}})
    vol = 0.0
    for f in sorted(new, key=lambda f: f["time"]):
        usd = fnum(f["px"]) * fnum(f["sz"])
        vol += usd
        act["fills"] += 1
        act["volume"] += usd
        act["realized"] += fnum(f["closedPnl"]) - fnum(f["fee"])
        bc = act["by_coin"].setdefault(f["coin"], [0, 0.0])
        bc[0] += 1
        bc[1] += usd
        RECENT.append((f["time"], f["coin"], f.get("dir", ""), usd, bool(f.get("crossed"))))
    return len(new), vol


# ------------------------------------------------------------- paper account
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
    now_ms = int(time.time() * 1000)
    try:
        fills_n, fills_vol = poll_fills(args.address, state, now_ms)
    except Exception as e:
        fills_n, fills_vol = 0, 0.0
        print(f"  warning: fill monitor failed ({e})", file=sys.stderr)
    funding_pay = accrue_funding(state, mids)

    net_notional = {c: abs(sz) * price(c) for c, sz in net.items() if price(c)}
    tot_net = sum(net_notional.values())
    perp_gross = sum(abs(sz) * price(c) for c, sz in pos.items() if price(c))

    prev = state.get("prev_net")
    change = None
    if prev is not None and tot_net:
        delta = sum(abs(net.get(c, 0.0) - prev.get(c, 0.0)) * (price(c) or 0.0)
                    for c in set(net) | set(prev))
        change = delta / tot_net * 100
        if change > 0.05:
            state["last_book_change"] = time.time()
    state["prev_net"] = net

    r = dict(their_acct=their_acct, tot_net=tot_net, perp_gross=perp_gross, n_pos=len(pos),
             change=change, hedge=hedge, mids=mids, targets={}, trades=[], fills_n=fills_n,
             fills_vol=fills_vol, funding_pay=funding_pay)

    eq, _ = equity_and_notional(state, mids)
    if eq <= 0:
        state["dead"] = True
        r.update(eq=eq, notional=0.0, eq_exit=eq)
        return r

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
        closing_all = tgt == 0.0 and cur != 0.0
        drift_ok = abs(delta) >= args.rebalance * max(abs(tgt), abs(cur))
        if abs(delta) * px < args.min_trade and not closing_all:
            continue
        if not (closing_all or cur == 0.0 or drift_ok):
            continue
        fill_px = px * (1 + args.slip_bps / 1e4 * (1 if delta > 0 else -1))
        realized, fee = apply_fill(state, coin, delta, fill_px, args.fee_bps)
        trades.append((coin, delta, fill_px, realized, fee))

    eq, notional = equity_and_notional(state, mids)
    r.update(eq=eq, notional=notional, trades=trades, targets=targets,
             eq_exit=eq - notional * (args.slip_bps + args.fee_bps) / 1e4)
    return r


def update_stats(state, eq, n_trades):
    s = state.setdefault("stats", {"ticks": 0, "best": None, "worst": None,
                                   "max_dd": 0.0, "profit_ticks": 0, "trades": 0})
    s["ticks"] += 1
    s["trades"] += n_trades
    prev = state.get("prev_eq")
    if prev:
        chg = (eq / prev - 1) * 100
        s["best"] = chg if s["best"] is None else max(s["best"], chg)
        s["worst"] = chg if s["worst"] is None else min(s["worst"], chg)
    state["prev_eq"] = eq
    if eq >= state["capital"]:
        s["profit_ticks"] += 1
    peak = state.get("peak", state["capital"])
    if peak:
        s["max_dd"] = max(s["max_dd"], (peak - eq) / peak * 100)


# ----------------------------------------------------------------- dashboard
def render(args, state, r, c, hist, t_start):
    eq, cap = r["eq"], state["capital"]
    ret = (eq / cap - 1) * 100
    peak = state.get("peak", cap)
    dd = (peak - eq) / peak * 100 if peak else 0.0
    notional = r["notional"]
    lev = notional / eq if eq > 0 else 0.0
    up = int(time.time() - t_start)
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    s = state.get("stats", {})
    act = state.get("act", {"fills": 0, "volume": 0.0, "realized": 0.0, "by_coin": {}})
    unreal = eq - state["cash"]
    funding = state.get("funding", 0.0)
    L = []
    L.append(c("HL PAPER COPY", "b") + f"  {args.address[:8]}...{args.address[-4:]}   {now} UTC   "
             f"running {up // 3600}h{(up % 3600) // 60:02d}m   check every {args.interval}s")
    L.append(c("=" * 92, "d"))
    eq_txt = sgn(c, ret, f"{eq:,.2f}")
    ret_txt = sgn(c, ret, f"{ret:+.2f}%")
    dd_txt = c(f"-{dd:.2f}%", "y" if dd > 5 else "d")
    L.append(f"EQUITY {eq_txt} ({ret_txt})   peak {peak:,.2f}   drawdown {dd_txt} "
             f"(stop at -{args.max_dd * 100:.0f}%)   if closed now {r['eq_exit']:,.2f}")
    lev_txt = c(f"{lev:.2f}x", "y" if lev > args.max_lev * 1.05 else "d")
    realized = state["realized"]
    real_txt = sgn(c, realized, f"{realized:+,.2f}")
    unreal_txt = sgn(c, unreal, f"{unreal:+,.2f}")
    fund_txt = sgn(c, funding, f"{funding:+,.2f}")
    fees_txt = c("-" + format(state["fees"], ",.2f"), "d")
    L.append(f"LEVERAGE {lev_txt} of {args.max_lev:g}x   exposure {notional:,.0f}   "
             f"realized {real_txt}   unrealized {unreal_txt}   funding {fund_txt}   fees {fees_txt}")
    if len(hist) >= 2:
        h = list(hist)
        L.append(f"TREND  {sgn(c, h[-1] - h[0], spark(h))}  low {min(h):,.2f}  high {max(h):,.2f}  (last {len(h)} checks)")

    L.append("")
    L.append(c("YOUR POSITIONS (paper)", "b"))
    L.append(c(f"{'COIN':<9}{'SIDE':<6}{'SIZE':>13}{'ENTRY':>12}{'MARK':>12}{'UPNL':>11}{'EXPOSURE':>10}{'OFF-TGT':>9}", "d"))
    rows = []
    for coin, p in state["positions"].items():
        m = fnum(r["mids"].get(coin), p["entry"])
        tgt = r["targets"].get(coin)
        off = (p["size"] - tgt) / abs(tgt) * 100 if tgt else None
        rows.append((abs(p["size"]) * m, coin, p, m, p["size"] * (m - p["entry"]), off))
    rows.sort(key=lambda x: -x[0])
    for expo, coin, p, m, upl, off in rows[:12]:
        side = c("SHORT", "r") if p["size"] < 0 else c("LONG ", "g")
        off_txt = "-" if off is None else f"{off:+.0f}%"
        L.append(f"{c(f'{coin:<9}', 'c')}{side} {p['size']:>12.5g} {p['entry']:>11.6g} {m:>11.6g} "
                 f"{sgn(c, upl, f'{upl:>+10,.2f}')} {expo:>9,.0f} {off_txt:>8}")
    if len(rows) > 12:
        L.append(c(f"... and {len(rows) - 12} more", "d"))
    if not rows:
        L.append(c("(no positions)", "d"))

    L.append("")
    L.append(c("THE WALLET", "b"))
    chg = "n/a" if r["change"] is None else f"{r['change']:.2f}%"
    lc = state.get("last_book_change")
    if lc:
        mins = (time.time() - lc) / 60
        lc_txt = f"net book last changed {mins:.0f} min ago" if mins < 120 else f"net book last changed {mins / 60:.1f} h ago"
    else:
        lc_txt = "net book has NOT changed since we started"
    L.append(f"perp exposure {money(r['perp_gross'])}   net after hedges {money(r['tot_net'])}   "
             f"positions {r['n_pos']}   change this check {chg}")
    L.append(c(lc_txt, "y" if not lc else "g"))
    if r["hedge"]:
        L.append("hedged with spot: " + ", ".join(f"{k} {money(v)}" for k, v in sorted(r["hedge"].items())))
    top = sorted(act["by_coin"].items(), key=lambda kv: -kv[1][0])[:4]
    top_txt = ", ".join(f"{k} {v[0]:,}" for k, v in top) or "-"
    L.append(f"fills: {r['fills_n']} (${money(r['fills_vol'])}) in the last check | since start {act['fills']:,} fills, "
             f"${money(act['volume'])} traded, their realized {sgn(c, act['realized'], money(act['realized']))} | top: {top_txt}")
    if RECENT:
        L.append(c("recent fills (UTC):", "d"))
        for t, coin, d, usd, taker in list(RECENT)[-5:]:
            hh = datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%H:%M:%S")
            L.append(f"  {hh}  {coin:<8} {d:<12} ${usd:>10,.0f}  {'taker' if taker else 'maker'}")

    L.append("")
    L.append(c("RUN STATISTICS", "b"))
    best = "n/a" if s.get("best") is None else f"{s['best']:+.2f}%"
    worst = "n/a" if s.get("worst") is None else f"{s['worst']:+.2f}%"
    pt = s.get("profit_ticks", 0) / s["ticks"] * 100 if s.get("ticks") else 0
    L.append(f"checks {s.get('ticks', 0)}   paper trades {s.get('trades', 0)}   best check {best}   worst check {worst}   "
             f"max drawdown -{s.get('max_dd', 0):.2f}%   time in profit {pt:.0f}%")
    if r["trades"]:
        L.append(c("trades this check: ", "d") + ", ".join(
            f"{'BUY' if d > 0 else 'SELL'} {coin} {abs(d):.4g}" for coin, d, *_ in r["trades"]))
    L.append(c("Paper results only: no real orders. Real fills would be worse. Ctrl+C to stop.", "d"))
    return "\n".join(L)


def load_hist(log_file, n=60):
    h = deque(maxlen=n)
    try:
        with open(log_file) as fh:
            for row in csv.DictReader(fh):
                h.append(float(row["equity"]))
    except Exception:
        pass
    return h


# --------------------------------------------------------------------- main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("address")
    ap.add_argument("--capital", type=float, default=1000.0)
    ap.add_argument("--max-lev", type=float, default=2.0, help="gross exposure / equity (default 2)")
    ap.add_argument("--max-dd", type=float, default=0.25, help="flatten and stop at this drawdown; 0 disables")
    ap.add_argument("--min-share", type=float, default=0.01)
    ap.add_argument("--min-trade", type=float, default=5.0)
    ap.add_argument("--rebalance", type=float, default=0.05)
    ap.add_argument("--slip-bps", type=float, default=5.0)
    ap.add_argument("--fee-bps", type=float, default=4.5)
    ap.add_argument("--interval", type=int, default=30)
    ap.add_argument("--no-hedge", action="store_true", help="ignore spot holdings (perp only)")
    ap.add_argument("--tag", default="_v2", help="suffix for the state and log files")
    ap.add_argument("--plain", action="store_true", help="simple log lines instead of the dashboard")
    ap.add_argument("--no-color", action="store_true")
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
                 "fees": 0.0, "realized": 0.0, "funding": 0.0, "started": time.time(), "dead": False}
    if state.get("halted"):
        sys.exit(f"Halted earlier by the drawdown stop (state: {state_file}). Use --reset to restart.")

    tty = sys.stdout.isatty()
    dash = tty and not args.plain and not args.once
    c = make_c(tty and not args.no_color and not os.environ.get("NO_COLOR"))
    hist = load_hist(log_file)
    t_start = time.time()
    new_log, new_book = not os.path.exists(log_file), not os.path.exists(book_file)
    if not dash:
        print(f"Paper-copying {args.address}  capital {state['capital']:,.2f}  max lev {args.max_lev}x  "
              f"stop {args.max_dd:.0%}  hedge netting {'off' if args.no_hedge else 'on'}  state: {state_file}")

    while True:
        try:
            r = tick(args, state)
        except Exception as e:
            msg = f"[{datetime.now():%H:%M:%S}] error: {e}"
            print(c(msg, "r"), file=sys.stderr)
            if args.once:
                sys.exit(1)
            time.sleep(args.interval)
            continue
        eq = r["eq"]
        update_stats(state, eq, len(r["trades"]))
        hist.append(eq)
        json.dump(state, open(state_file, "w"), indent=1)
        ret = (eq / state["capital"] - 1) * 100
        now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

        with open(log_file, "a", newline="") as fh:
            w = csv.writer(fh)
            if new_log:
                w.writerow(["time_utc", "equity", "return_pct", "notional", "fees",
                            "their_account_value", "n_trades"])
                new_log = False
            w.writerow([now, f"{eq:.2f}", f"{ret:.3f}", f"{r['notional']:.0f}", f"{state['fees']:.2f}",
                        f"{r['their_acct']:.0f}", len(r["trades"])])
        with open(book_file, "a", newline="") as fh:
            w = csv.writer(fh)
            if new_book:
                w.writerow(["time_utc", "their_perp_gross", "their_net_exposure", "n_positions",
                            "book_change_pct", "hedged_coins", "equity_after_exit_costs",
                            "fills_in_interval", "fill_volume_in_interval", "paper_funding_total"])
                new_book = False
            w.writerow([now, f"{r['perp_gross']:.0f}", f"{r['tot_net']:.0f}", r["n_pos"],
                        "" if r["change"] is None else f"{r['change']:.3f}",
                        ";".join(f"{k}:{v:.0f}" for k, v in sorted(r["hedge"].items())),
                        f"{r['eq_exit']:.2f}", r["fills_n"], f"{r['fills_vol']:.0f}",
                        f"{state.get('funding', 0.0):.2f}"])

        if dash:
            sys.stdout.write("\033[2J\033[H" + render(args, state, r, c, hist, t_start) + "\n")
            sys.stdout.flush()
        else:
            chg = "n/a" if r["change"] is None else f"{r['change']:.2f}%"
            notional = r["notional"]
            print(f"[{now}] equity {sgn(c, ret, f'{eq:,.2f}')} ({sgn(c, ret, f'{ret:+.2f}%')})  "
                  f"exposure {notional:,.0f}  lev {notional / eq if eq > 0 else 0:.2f}x  "
                  f"fees {state['fees']:.2f}  funding {state.get('funding', 0.0):+.2f}  "
                  f"positions {len(state['positions'])}  their book change {chg}  "
                  f"their fills {r['fills_n']}")
            for coin, d, px, rl, fee in r["trades"]:
                print(f"    {c('BUY ', 'g') if d > 0 else c('SELL', 'r')} {coin:8} {abs(d):.6g} @ {px:.6g}  "
                      f"realized {rl:+.2f}  fee {fee:.2f}")

        if state.get("halted"):
            print(c(f"Drawdown stop hit: positions flattened at equity {eq:,.2f}. Stopped.", "r"))
            break
        if state.get("dead"):
            print(c("Paper account wiped out (equity <= 0). Use --reset to restart.", "r"))
            break
        if args.once:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nStopped. State is saved; run the same command again to continue.")
