#!/usr/bin/env python3
"""
Read-only Hyperliquid wallet report.

Uses only Hyperliquid's public info API. It needs no API key or private key,
and it cannot place trades or move funds.

Usage:
    python3 hl_wallet_report.py 0xWALLET_ADDRESS [--days 90]

Notes:
  * Hyperliquid only returns about the 10,000 most recent fills per wallet,
    so very active wallets may not have the full window available.
  * Stats are for what that wallet did. They are not a prediction.
"""
import argparse
import json
import sys
import time
import re
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

API = "https://api.hyperliquid.xyz/info"


def post(payload):
    req = urllib.request.Request(
        API,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        body = e.read().decode(errors="replace")[:300]
        raise RuntimeError(f"HTTP {e.code} from Hyperliquid for "
                           f"{payload.get('type')}: {body}")


def fetch_fills(addr, start_ms, end_ms):
    fills, cursor = [], start_ms
    while True:
        batch = post({"type": "userFillsByTime", "user": addr,
                      "startTime": cursor, "endTime": end_ms})
        if not batch:
            break
        fills.extend(batch)
        if len(batch) < 2000:
            break
        cursor = max(f["time"] for f in batch) + 1
        time.sleep(0.3)
    seen, out = set(), []
    for f in sorted(fills, key=lambda f: f["time"]):
        key = (f.get("tid"), f["time"], f["coin"], f["px"], f["sz"])
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def usd(x):
    return f"{x:,.2f}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("address")
    ap.add_argument("--days", type=int, default=90)
    args = ap.parse_args()
    addr = args.address.strip().strip("<>\"'").lower()
    if not re.fullmatch(r"0x[0-9a-f]{40}", addr):
        sys.exit("Invalid address. It must be 0x followed by 40 hex characters "
                 "(42 characters total). Copy the full address, not the "
                 "shortened version shown on the leaderboard, e.g. 0x1234...abcd.")

    now_ms = int(time.time() * 1000)
    start_ms = now_ms - args.days * 86400 * 1000

    state = post({"type": "clearinghouseState", "user": addr})
    acct_value = float(state["marginSummary"]["accountValue"])
    print(f"Wallet: {addr}")
    print(f"Account value now: {usd(acct_value)} USDC")

    # Current open positions and their leverage
    print("\n== Open positions ==")
    positions = [p["position"] for p in state.get("assetPositions", [])]
    if not positions:
        print("None")
    for p in positions:
        lev = p["leverage"]
        liq = p.get("liquidationPx")
        print(f'{p["coin"]:8} size {p["szi"]:>12}  entry {p.get("entryPx")}  '
              f'lev {lev["value"]}x {lev["type"]}  liq {liq}  '
              f'uPnL {usd(float(p["unrealizedPnl"]))}  '
              f'value {usd(float(p["positionValue"]))}')

    try:
        fills = fetch_fills(addr, start_ms, now_ms)
    except RuntimeError as e:
        print(f"\n(userFillsByTime failed: {e})\nFalling back to the latest fills only.")
        fills = sorted(post({"type": "userFills", "user": addr}),
                       key=lambda f: f["time"])
        fills = [f for f in fills if f["time"] >= start_ms]
    print(f"\n== Last {args.days} days: {len(fills)} fills ==")
    if not fills:
        print("No fills returned.")
        return
    first = datetime.fromtimestamp(fills[0]["time"] / 1000, timezone.utc)
    print(f"Earliest fill returned: {first:%Y-%m-%d}")

    closing = [f for f in fills if float(f["closedPnl"]) != 0.0]
    gross = sum(float(f["closedPnl"]) for f in fills)
    fees = sum(float(f["fee"]) for f in fills)
    net = gross - fees
    volume = sum(float(f["px"]) * float(f["sz"]) for f in fills)

    wins = [float(f["closedPnl"]) for f in closing if float(f["closedPnl"]) > 0]
    losses = [float(f["closedPnl"]) for f in closing if float(f["closedPnl"]) < 0]
    win_rate = len(wins) / len(closing) * 100 if closing else 0
    pf = (sum(wins) / abs(sum(losses))) if losses else float("inf")

    print(f"Volume traded:        {usd(volume)}")
    print(f"Realized PnL (gross): {usd(gross)}")
    print(f"Fees paid:            {usd(fees)}")
    print(f"Realized PnL (net):   {usd(net)}")
    print(f"Closing fills:        {len(closing)}  win rate {win_rate:.1f}%")
    if wins:
        print(f"Avg win:              {usd(sum(wins) / len(wins))}")
    if losses:
        print(f"Avg loss:             {usd(sum(losses) / len(losses))}")
        print(f"Largest single loss:  {usd(min(losses))}")
    print(f"Profit factor:        {pf:.2f}")

    # Daily net PnL and max drawdown of cumulative realized PnL
    daily = defaultdict(float)
    for f in fills:
        d = datetime.fromtimestamp(f["time"] / 1000, timezone.utc).strftime("%Y-%m-%d")
        daily[d] += float(f["closedPnl"]) - float(f["fee"])
    cum = peak = max_dd = 0.0
    for d in sorted(daily):
        cum += daily[d]
        peak = max(peak, cum)
        max_dd = max(max_dd, peak - cum)
    print(f"\nMax drawdown of cumulative realized PnL: {usd(max_dd)} USDC "
          f"(~{max_dd / acct_value * 100:.1f}% of current account value)")
    best = max(daily.items(), key=lambda kv: kv[1])
    worst = min(daily.items(), key=lambda kv: kv[1])
    print(f"Best day:  {best[0]}  {usd(best[1])}")
    print(f"Worst day: {worst[0]}  {usd(worst[1])}")
    pos_days = sum(1 for v in daily.values() if v > 0)
    print(f"Profitable days: {pos_days} of {len(daily)}")
    top = sorted(daily.values(), reverse=True)[:3]
    if net > 0:
        print(f"Top 3 days = {sum(top) / net * 100:.0f}% of net PnL "
              f"(high % means results depend on a few lucky days)")

    # Breakdown by coin
    by_coin = defaultdict(lambda: [0.0, 0.0, 0])
    for f in fills:
        c = by_coin[f["coin"]]
        c[0] += float(f["closedPnl"]) - float(f["fee"])
        c[1] += float(f["px"]) * float(f["sz"])
        c[2] += 1
    print("\n== By coin (net PnL, volume, fills) ==")
    for coin, (p, v, n) in sorted(by_coin.items(), key=lambda kv: -abs(kv[1][0]))[:10]:
        print(f"{coin:10} PnL {usd(p):>14}  vol {usd(v):>16}  fills {n}")

    # Time between fills as a rough idea of trading style
    gaps = [(b["time"] - a["time"]) / 1000 for a, b in zip(fills, fills[1:])]
    if gaps:
        gaps.sort()
        med = gaps[len(gaps) // 2]
        print(f"\nMedian gap between fills: {med:.0f} seconds "
              f"(small gaps mean fast trading that is hard to copy)")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
