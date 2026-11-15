#!/usr/bin/env python3
"""
Summarize the CSV log written by hl_paper_copy.py.

Usage:
    python3 hl_paper_summary.py paper_0x5b5d51_log.csv
"""
import csv
import sys
from collections import defaultdict
from datetime import datetime


def main():
    if len(sys.argv) != 2:
        sys.exit("Usage: python3 hl_paper_summary.py <log.csv>")
    rows = []
    with open(sys.argv[1]) as fh:
        for r in csv.DictReader(fh):
            rows.append((datetime.strptime(r["time_utc"], "%Y-%m-%d %H:%M:%S"),
                         float(r["equity"]), float(r["notional"]),
                         float(r["fees"]), int(r["n_trades"])))
    if len(rows) < 2:
        sys.exit("Not enough data yet.")

    t0, e0 = rows[0][0], rows[0][1]
    t1, e1 = rows[-1][0], rows[-1][1]
    hours = (t1 - t0).total_seconds() / 3600

    peak, max_dd, dd_time = e0, 0.0, t0
    for t, e, *_ in rows:
        peak = max(peak, e)
        dd = (peak - e) / peak
        if dd > max_dd:
            max_dd, dd_time = dd, t
    cur_dd = (peak - e1) / peak

    print(f"Period:        {t0:%Y-%m-%d %H:%M} -> {t1:%Y-%m-%d %H:%M} UTC "
          f"({hours:.1f} h, {len(rows)} samples)")
    print(f"Start equity:  {e0:,.2f}")
    print(f"Latest equity: {e1:,.2f}  ({(e1 / e0 - 1) * 100:+.2f}%)")
    print(f"Peak equity:   {peak:,.2f}")
    print(f"Max drawdown:  {max_dd * 100:.2f}% (at {dd_time:%Y-%m-%d %H:%M})")
    print(f"Current drawdown from peak: {cur_dd * 100:.2f}%")
    print(f"Fees paid:     {rows[-1][3]:,.2f}")
    print(f"Rebalance ticks (any trade): {sum(1 for r in rows if r[4] > 0)}")

    # Change per sample, then best/worst single-sample and hourly moves
    changes = [(rows[i][0], (rows[i][1] / rows[i - 1][1] - 1) * 100)
               for i in range(1, len(rows))]
    best = max(changes, key=lambda x: x[1])
    worst = min(changes, key=lambda x: x[1])
    print(f"\nBest single interval:  {best[1]:+.2f}% at {best[0]:%m-%d %H:%M}")
    print(f"Worst single interval: {worst[1]:+.2f}% at {worst[0]:%m-%d %H:%M}")

    hourly = {}
    for t, e, *_ in rows:
        hourly.setdefault(t.strftime("%Y-%m-%d %H:00"), []).append(e)
    keys = sorted(hourly)
    hr = []
    prev_last = e0
    for k in keys:
        last = hourly[k][-1]
        hr.append((k, (last / prev_last - 1) * 100))
        prev_last = last
    if len(hr) >= 2:
        bh = max(hr, key=lambda x: x[1])
        wh = min(hr, key=lambda x: x[1])
        print(f"Best hour:  {bh[0]}  {bh[1]:+.2f}%")
        print(f"Worst hour: {wh[0]}  {wh[1]:+.2f}%")

    # Daily table
    daily = defaultdict(list)
    for t, e, *_ in rows:
        daily[t.strftime("%Y-%m-%d")].append(e)
    print("\nDay          open      close     change")
    prev = e0
    for d in sorted(daily):
        close = daily[d][-1]
        print(f"{d}  {prev:9,.2f} {close:9,.2f}  {(close / prev - 1) * 100:+7.2f}%")
        prev = close

    if hours < 24 * 7:
        print("\nNote: less than a week of data. Treat all of this as noise.")


if __name__ == "__main__":
    main()
