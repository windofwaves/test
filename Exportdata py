#!/usr/bin/env python3
"""
Export a Hyperliquid wallet's public data to CSV files for analysis.

Read-only: public info API only. No keys, no trading. Safe to run while your
paper bots are running (it paces its requests and retries on rate limits).

Usage:
    python3 hl_export_wallet.py 0xFULL_WALLET_ADDRESS
    python3 hl_export_wallet.py 0xFULL_WALLET_ADDRESS --fill-days 3 --raw

Writes a folder export_<wallet>_<time>/ containing:
  summary.json              what was fetched, window sizes, truncation warnings, account summary
  positions.csv             open perp positions (size, entry, mark, uPnL, leverage, liquidation, funding)
  spot_balances.csv         spot holdings
  open_orders.csv           resting orders (side, price, size, reduce-only)
  historical_orders.csv     most recent ~2000 orders with their final status
  ledger.csv                deposits, withdrawals, sends, transfers
  funding.csv               funding payments per coin
  portfolio_<period>.csv    account value and PnL history (day/week/month/all, perp-only variants)
  fills_hourly.csv          fills, volume, realized PnL, fees, buy/sell volume, maker/taker per hour
  fills_hourly_by_coin.csv  the same per hour, coin and direction (open/close, long/short)
  candles_<coin>_<iv>.csv   price candles for the main coins (for volatility and benchmark analysis)
  fills_raw.csv.gz          every fill (only with --raw; can be large)
"""
import argparse
import csv
import gzip
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone

API = "https://api.hyperliquid.xyz/info"


def post(payload, tries=5):
    last = None
    for i in range(tries):
        req = urllib.request.Request(
            API, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:200]
            last = RuntimeError(f"HTTP {e.code} for {payload.get('type')}: {body}")
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(2 * (i + 1))
                continue
            raise last
        except urllib.error.URLError as e:
            last = RuntimeError(f"network error: {e}")
            time.sleep(2 * (i + 1))
    raise last


def fnum(x, default=0.0):
    try:
        return float(x)
    except (TypeError, ValueError):
        return default


def iso(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def hour_of(ms):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:00")


def paged(make_payload, start_ms, end_ms, page_limit, max_pages, pause):
    out, cursor, truncated = [], start_ms, False
    for i in range(max_pages):
        batch = post(make_payload(cursor, end_ms))
        if not batch:
            break
        out.extend(batch)
        if len(batch) < page_limit:
            break
        cursor = max(b["time"] for b in batch) + 1
        if i == max_pages - 1:
            truncated = True
        time.sleep(pause)
    return out, truncated


def write_csv(path, header, rows):
    with open(path, "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(header)
        w.writerows(rows)


# ------------------------------------------------------------------ exporters
def ex_positions(addr, out, summary):
    st = post({"type": "clearinghouseState", "user": addr})
    summary["account"] = {"margin_summary": st.get("marginSummary"),
                          "cross_maintenance_margin_used": st.get("crossMaintenanceMarginUsed"),
                          "withdrawable": st.get("withdrawable")}
    rows = []
    for a in st.get("assetPositions", []):
        p = a["position"]
        sz, val = fnum(p["szi"]), fnum(p["positionValue"])
        rows.append([p["coin"], sz, p.get("entryPx"), val / abs(sz) if sz else "", val,
                     p.get("unrealizedPnl"), p["leverage"]["value"], p["leverage"]["type"],
                     p.get("liquidationPx"), (p.get("cumFunding") or {}).get("sinceOpen"),
                     (p.get("cumFunding") or {}).get("allTime")])
    write_csv(os.path.join(out, "positions.csv"),
              ["coin", "size", "entry_px", "mark_px", "position_value", "unrealized_pnl", "leverage",
               "margin_mode", "liquidation_px", "funding_since_open", "funding_all_time"], rows)
    return len(rows)


def ex_spot(addr, out, summary):
    ss = post({"type": "spotClearinghouseState", "user": addr})
    rows = [[b["coin"], b.get("total"), b.get("hold"), b.get("entryNtl")] for b in ss.get("balances", [])]
    write_csv(os.path.join(out, "spot_balances.csv"), ["coin", "total", "held_in_orders", "cost_basis_usd"], rows)
    try:
        v = post({"type": "userVaultEquities", "user": addr})
        summary["vault_equities"] = v
    except Exception as e:
        summary["vault_equities"] = f"unavailable: {e}"
    return len(rows)


def ex_open_orders(addr, out):
    orders = post({"type": "frontendOpenOrders", "user": addr})
    rows = [[iso(o.get("timestamp", 0)), o["coin"], o["side"], o.get("limitPx"), o.get("sz"), o.get("origSz"),
             o.get("reduceOnly"), o.get("orderType"), o.get("isTrigger"), o.get("triggerPx"), o.get("oid")]
            for o in orders]
    write_csv(os.path.join(out, "open_orders.csv"),
              ["time_utc", "coin", "side", "limit_px", "size", "orig_size", "reduce_only", "order_type",
               "is_trigger", "trigger_px", "oid"], rows)
    return len(rows)


def ex_hist_orders(addr, out):
    orders = post({"type": "historicalOrders", "user": addr})
    rows = []
    for o in orders:
        d = o["order"]
        rows.append([iso(d.get("timestamp", 0)), iso(o.get("statusTimestamp", 0)), d["coin"], d["side"],
                     d.get("limitPx"), d.get("sz"), d.get("origSz"), d.get("orderType"),
                     d.get("reduceOnly"), o.get("status"), d.get("oid")])
    write_csv(os.path.join(out, "historical_orders.csv"),
              ["placed_utc", "status_time_utc", "coin", "side", "limit_px", "size", "orig_size",
               "order_type", "reduce_only", "status", "oid"], rows)
    return len(rows)


def ex_ledger(addr, out, start, end, summary):
    ups, trunc = paged(lambda s, e: {"type": "userNonFundingLedgerUpdates", "user": addr,
                                     "startTime": s, "endTime": e}, start, end, 100, 40, 0.7)
    rows = []
    for u in sorted(ups, key=lambda u: u["time"]):
        d = u["delta"]
        amt = d.get("usdc", d.get("usdcValue", d.get("amount", "")))
        rows.append([iso(u["time"]), u["time"], d.get("type"), amt, d.get("token", ""), d.get("user", ""),
                     d.get("destination", ""), json.dumps(d, separators=(",", ":"))])
    write_csv(os.path.join(out, "ledger.csv"),
              ["time_utc", "time_ms", "type", "amount", "token", "from_user", "destination", "raw_delta"], rows)
    summary["ledger_truncated"] = trunc
    return len(rows)


def ex_funding(addr, out, start, end, summary):
    ups, trunc = paged(lambda s, e: {"type": "userFunding", "user": addr, "startTime": s, "endTime": e},
                       start, end, 100, 60, 0.7)
    rows = [[iso(u["time"]), u["delta"]["coin"], u["delta"]["usdc"], u["delta"].get("szi"),
             u["delta"].get("fundingRate")] for u in sorted(ups, key=lambda u: u["time"])]
    write_csv(os.path.join(out, "funding.csv"), ["time_utc", "coin", "usdc", "size", "funding_rate"], rows)
    summary["funding_truncated"] = trunc
    return len(rows)


def ex_portfolio(addr, out):
    data = post({"type": "portfolio", "user": addr})
    n = 0
    for name, d in data:
        av = d.get("accountValueHistory", [])
        pn = d.get("pnlHistory", [])
        rows = []
        for i, (t, v) in enumerate(av):
            pnl = pn[i][1] if i < len(pn) else ""
            rows.append([iso(t), v, pnl])
        write_csv(os.path.join(out, f"portfolio_{name}.csv"), ["time_utc", "account_value", "pnl"], rows)
        n += len(rows)
    return n


def ex_fills(addr, out, start, end, raw, max_pages, summary):
    def run(agg):
        def mk(s, e):
            pl = {"type": "userFillsByTime", "user": addr, "startTime": s, "endTime": e}
            if agg:
                pl["aggregateByTime"] = True
            return pl
        return paged(mk, start, end, 2000, max_pages, 1.5)
    try:
        fills, trunc = run(True)
    except RuntimeError:
        fills, trunc = run(False)
    seen, uniq = set(), []
    for f in sorted(fills, key=lambda f: f["time"]):
        k = (f.get("tid"), f["time"], f["coin"], f["px"], f["sz"])
        if k not in seen:
            seen.add(k)
            uniq.append(f)

    tot = defaultdict(lambda: [0, 0.0, 0.0, 0.0, 0.0, 0.0, 0, 0])
    by = defaultdict(lambda: [0, 0.0, 0.0, 0.0])
    for f in uniq:
        h = hour_of(f["time"])
        usd = fnum(f["px"]) * fnum(f["sz"])
        pnl, fee = fnum(f["closedPnl"]), fnum(f["fee"])
        t = tot[h]
        t[0] += 1
        t[1] += usd
        t[2] += pnl
        t[3] += fee
        t[4] += usd if f["side"] == "B" else 0.0
        t[5] += usd if f["side"] == "A" else 0.0
        t[6] += 1 if f.get("crossed") is False else 0
        t[7] += 1 if f.get("crossed") else 0
        b = by[(h, f["coin"], f.get("dir", ""))]
        b[0] += 1
        b[1] += usd
        b[2] += pnl
        b[3] += fee
    write_csv(os.path.join(out, "fills_hourly.csv"),
              ["hour_utc", "fills", "volume_usd", "closed_pnl", "fees", "buy_usd", "sell_usd",
               "maker_fills", "taker_fills"],
              [[h] + [round(x, 2) if isinstance(x, float) else x for x in v] for h, v in sorted(tot.items())])
    write_csv(os.path.join(out, "fills_hourly_by_coin.csv"),
              ["hour_utc", "coin", "direction", "fills", "volume_usd", "closed_pnl", "fees"],
              [[h, c, d] + [round(x, 2) if isinstance(x, float) else x for x in v]
               for (h, c, d), v in sorted(by.items())])
    if raw:
        with gzip.open(os.path.join(out, "fills_raw.csv.gz"), "wt", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["time_utc", "time_ms", "coin", "side", "dir", "px", "sz", "usd", "closed_pnl", "fee",
                        "crossed", "start_position", "oid"])
            for f in uniq:
                w.writerow([iso(f["time"]), f["time"], f["coin"], f["side"], f.get("dir", ""), f["px"], f["sz"],
                            round(fnum(f["px"]) * fnum(f["sz"]), 4), f["closedPnl"], f["fee"], f.get("crossed"),
                            f.get("startPosition"), f.get("oid")])
    summary["fills_truncated"] = trunc
    if uniq:
        summary["fills_range_utc"] = [iso(uniq[0]["time"]), iso(uniq[-1]["time"])]
    return len(uniq)


def ex_candles(coin, interval, start, end, out):
    cs = post({"type": "candleSnapshot", "req": {"coin": coin, "interval": interval,
                                                 "startTime": start, "endTime": end}})
    rows = [[iso(c["t"]), c["o"], c["h"], c["l"], c["c"], c["v"], c.get("n")] for c in cs]
    write_csv(os.path.join(out, f"candles_{coin}_{interval}.csv"),
              ["time_utc", "open", "high", "low", "close", "volume", "trades"], rows)
    return len(rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("address")
    ap.add_argument("--days", type=int, default=14, help="window for ledger and portfolio (default 14)")
    ap.add_argument("--fill-days", type=float, default=3.0, help="window for fills (default 3)")
    ap.add_argument("--funding-days", type=float, default=7.0)
    ap.add_argument("--max-fill-pages", type=int, default=40, help="safety limit, 2000 fills per page")
    ap.add_argument("--coins", default="BTC,ETH,SOL,ZEC,HYPE", help="coins for candle export")
    ap.add_argument("--candle-interval", default="15m")
    ap.add_argument("--raw", action="store_true", help="also write every fill to fills_raw.csv.gz")
    args = ap.parse_args()

    addr = args.address.strip().strip("<>\"'").lower()
    if not re.fullmatch(r"0x[0-9a-f]{40}", addr):
        sys.exit("Invalid address: need the full 0x + 40 hex characters.")
    now = int(time.time() * 1000)
    out = f"export_{addr[:8]}_{datetime.now(timezone.utc):%Y%m%d_%H%M}"
    os.makedirs(out, exist_ok=True)
    summary = {"address": addr, "exported_utc": iso(now), "steps": {}}

    def step(name, fn):
        t0 = time.time()
        try:
            n = fn()
            summary["steps"][name] = f"ok ({n} rows)"
            print(f"  ok      {name:22} {n} rows  ({time.time() - t0:.0f}s)")
        except Exception as e:
            summary["steps"][name] = f"FAILED: {e}"
            print(f"  FAILED  {name:22} {e}")

    print(f"Exporting {addr} into {out}/")
    step("positions", lambda: ex_positions(addr, out, summary))
    step("spot_balances", lambda: ex_spot(addr, out, summary))
    step("open_orders", lambda: ex_open_orders(addr, out))
    step("historical_orders", lambda: ex_hist_orders(addr, out))
    step("portfolio", lambda: ex_portfolio(addr, out))
    step("ledger", lambda: ex_ledger(addr, out, now - args.days * 86400000, now, summary))
    step("funding", lambda: ex_funding(addr, out, now - int(args.funding_days * 86400000), now, summary))
    step("fills", lambda: ex_fills(addr, out, now - int(args.fill_days * 86400000), now,
                                   args.raw, args.max_fill_pages, summary))
    for coin in [c.strip() for c in args.coins.split(",") if c.strip()]:
        step(f"candles_{coin}", lambda c=coin: ex_candles(c, args.candle_interval,
                                                          now - int(max(args.fill_days, 7) * 86400000), now, out))
    with open(os.path.join(out, "summary.json"), "w") as fh:
        json.dump(summary, fh, indent=1)

    print("\nFiles:")
    for fn in sorted(os.listdir(out)):
        print(f"  {fn:34} {os.path.getsize(os.path.join(out, fn)) / 1024:8.1f} KB")
    for k in ("fills_truncated", "ledger_truncated", "funding_truncated"):
        if summary.get(k):
            print(f"WARNING: {k}: the page limit was reached, so older or newer rows are missing.")
    print("\nAttach the CSV files (and summary.json) in the chat. They contain only public wallet data.")


if __name__ == "__main__":
    main()
