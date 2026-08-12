#!/usr/bin/env python3
"""Calibrate the screen against realised listing-day outcomes.

Joins 수요예측결과 (what we can know *before* subscribing) against 신규상장
(what actually happened at the open), so the thresholds in config.json can be
set from evidence rather than folklore.

The return measured is 시초/공모 -- buy at the offer price, sell at the opening
auction -- which is the strategy this repo screens for.

    ./backtest.py --pages 15
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ipo_radar import fetch, parse, score

BUCKETS = [(0, 100), (100, 300), (300, 600), (600, 800), (800, 1200), (1200, 10 ** 9)]


def _pct(xs):
    return 100.0 * sum(1 for x in xs if x > 0) / len(xs) if xs else 0.0


def _mean(xs):
    return sum(xs) / float(len(xs)) if xs else 0.0


def _median(xs):
    if not xs:
        return 0.0
    s = sorted(xs)
    n = len(s)
    return s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def collect(pages):
    demand, listings = {}, {}
    for p in range(1, pages + 1):
        for row in parse.parse_demand_results(fetch.fetch(fetch.BASE + "?o=r1&page=%d" % p)):
            demand.setdefault(row["name"], row)
        for row in parse.parse_new_listings(fetch.fetch(fetch.BASE + "?o=nw&page=%d" % p)):
            listings.setdefault(row["name"], row)
        sys.stderr.write("  page %d/%d\r" % (p, pages))
    sys.stderr.write("\n")

    joined = []
    for name, d in demand.items():
        l = listings.get(name)
        if not l or l.get("open_vs_offer_pct") is None:
            continue
        if parse.is_spac(name):
            continue
        joined.append(
            {
                "name": name,
                "ratio": d["institutional_ratio"],
                "lockup": d["lockup_pct"],
                "band": d["band"],
                "final_price": d["final_price"],
                "ret": l["open_vs_offer_pct"],
                "listing_date": l["listing_date"],
            }
        )
    return joined


def band_pos(r):
    f, b = r["final_price"], r["band"]
    if not f or not b.get("high"):
        return None
    if f > b["high"]:
        return "above"
    if f == b["high"]:
        return "top"
    if f == b.get("low"):
        return "bottom"
    return "within"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=15, help="list pages to pull (20 rows each)")
    args = ap.parse_args()

    print("Fetching %d pages of 수요예측결과 + 신규상장 ..." % args.pages)
    rows = collect(args.pages)
    if not rows:
        print("No joined rows -- site layout may have changed.")
        return 1
    rets = [r["ret"] for r in rows]
    print("\nJoined %d non-SPAC deals, %s .. %s" % (
        len(rows), min(r["listing_date"] for r in rows), max(r["listing_date"] for r in rows)))
    print("Baseline (subscribe to everything): win %.0f%%  mean %+.1f%%  median %+.1f%%"
          % (_pct(rets), _mean(rets), _median(rets)))

    print("\n기관경쟁률 bucket        n    win%%   mean     median")
    print("-" * 52)
    for lo, hi in BUCKETS:
        sel = [r["ret"] for r in rows if lo <= r["ratio"] < hi]
        if not sel:
            continue
        label = "%d~%s" % (lo, "" if hi > 10 ** 8 else str(hi))
        print("%-20s %4d  %5.0f%%  %+7.1f%%  %+7.1f%%" % (label, len(sel), _pct(sel), _mean(sel), _median(sel)))

    print("\n확정공모가 vs 밴드         n    win%%   mean     median")
    print("-" * 52)
    for pos in ("above", "top", "within", "bottom"):
        sel = [r["ret"] for r in rows if band_pos(r) == pos]
        if not sel:
            continue
        print("%-20s %4d  %5.0f%%  %+7.1f%%  %+7.1f%%" % (pos, len(sel), _pct(sel), _mean(sel), _median(sel)))

    print("\n의무보유확약              n    win%%   mean     median")
    print("-" * 52)
    for lo, hi, label in [(0, 5, "<5%"), (5, 10, "5~10%"), (10, 20, "10~20%"), (20, 1000, ">=20%")]:
        sel = [r["ret"] for r in rows if r["lockup"] is not None and lo <= r["lockup"] < hi]
        if not sel:
            continue
        print("%-20s %4d  %5.0f%%  %+7.1f%%  %+7.1f%%" % (label, len(sel), _pct(sel), _mean(sel), _median(sel)))

    # What the live rule would actually have picked.
    print("\nCurrent rule (config.json):")
    print("-" * 52)
    cfg = dict(score.DEFAULT_CONFIG)
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if os.path.exists(cfg_path):
        import json
        with open(cfg_path) as fh:
            cfg.update(json.load(fh))
    by_verdict = {}
    for r in rows:
        rec = {
            "name": r["name"],
            "market": "코스닥",
            "institutional_ratio": r["ratio"],
            "lockup_pct": r["lockup"],
            "lockup_breakdown": {},
            "final_price": r["final_price"],
            "band_low": r["band"].get("low"),
            "band_high": r["band"].get("high"),
        }
        by_verdict.setdefault(score.evaluate(rec, cfg)["verdict"], []).append(r["ret"])
    for v in ("GO", "WATCH", "PASS"):
        sel = by_verdict.get(v, [])
        if not sel:
            continue
        print("%-20s %4d  %5.0f%%  %+7.1f%%  %+7.1f%%" % (v, len(sel), _pct(sel), _mean(sel), _median(sel)))
    print("\nNote: 시초/공모 return, before fees/tax. Past results do not guarantee future ones.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
