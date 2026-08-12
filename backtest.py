#!/usr/bin/env python3
"""Calibrate the screen against realised listing-day outcomes.

Joins 수요예측결과 (what we can know *before* subscribing) against 신규상장
(what actually happened at the open), so the thresholds in config.json can be
set from evidence rather than folklore.

The return measured is 시초/공모 -- buy at the offer price, sell at the opening
auction -- which is the strategy this repo screens for.

Writes three things:
    state/backtest/<date>.json  + latest.json   full dataset + aggregates
    state/backtest/<date>.html  + latest.html   self-contained visual report
    stdout                                      the same summary, as text

    ./backtest.py --pages 18
    ./backtest.py --pages 18 --no-write   # stdout only
"""

import argparse
import datetime
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ipo_radar import fetch, parse, render_backtest, score

ROOT = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(ROOT, "state", "backtest")

RATIO_BUCKETS = [(0, 100), (100, 300), (300, 600), (600, 800), (800, 1200), (1200, 10 ** 9)]
LOCKUP_BUCKETS = [(0, 5, "<5%"), (5, 10, "5~10%"), (10, 20, "10~20%"), (20, 10 ** 9, "≥20%")]
BAND_POSITIONS = (("above", "밴드 상단 초과"), ("top", "밴드 상단"),
                  ("within", "밴드 내"), ("bottom", "밴드 하단"))


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


def _stats(rets):
    return {
        "n": len(rets),
        "win_rate": round(_pct(rets), 2),
        "mean": round(_mean(rets), 2),
        "median": round(_median(rets), 2),
    }


def collect(pages):
    """Join 수요예측결과 against 신규상장 by company name."""
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
                "no": d.get("no"),
                "ratio": d["institutional_ratio"],
                "lockup": d["lockup_pct"],
                "band_low": d["band"].get("low"),
                "band_high": d["band"].get("high"),
                "final_price": d["final_price"],
                "offer_price": l.get("offer_price"),
                "open_price": l.get("open_price"),
                "ret": l["open_vs_offer_pct"],
                "listing_date": l["listing_date"],
            }
        )
    return joined


def band_pos(r):
    f, low, high = r.get("final_price"), r.get("band_low"), r.get("band_high")
    if not f or not high or not low:
        return None
    if f > high:
        return "above"
    if f == high:
        return "top"
    if f == low:
        return "bottom"
    return "within"


def load_config():
    cfg = dict(score.DEFAULT_CONFIG)
    path = os.path.join(ROOT, "config.json")
    if os.path.exists(path):
        with open(path) as fh:
            cfg.update(json.load(fh))
    return cfg


def evaluate_all(rows, cfg):
    """Attach the live screen's verdict to each historical deal."""
    for r in rows:
        rec = {
            "name": r["name"],
            "market": "코스닥",
            "institutional_ratio": r["ratio"],
            "lockup_pct": r["lockup"],
            "lockup_breakdown": {},
            "final_price": r["final_price"],
            "band_low": r["band_low"],
            "band_high": r["band_high"],
        }
        v = score.evaluate(rec, cfg)
        r["verdict"] = v["verdict"]
        r["score"] = v["score"]
        r["reasons"] = v["reasons"]
        r["band_position"] = band_pos(r)
    return rows


def aggregate(rows, cfg):
    rets = [r["ret"] for r in rows]
    agg = {
        "n": len(rows),
        "date_min": min(r["listing_date"] for r in rows),
        "date_max": max(r["listing_date"] for r in rows),
        "baseline": _stats(rets),
        "by_verdict": {},
        "by_ratio_bucket": [],
        "by_band_position": [],
        "by_lockup_bucket": [],
    }
    for v in ("GO", "WATCH", "PASS", "PENDING", "EXCLUDED"):
        sel = [r["ret"] for r in rows if r["verdict"] == v]
        if sel:
            agg["by_verdict"][v] = _stats(sel)
    for lo, hi in RATIO_BUCKETS:
        sel = [r["ret"] for r in rows if lo <= r["ratio"] < hi]
        if sel:
            label = "%d+" % lo if hi > 10 ** 8 else "%d–%d" % (lo, hi)
            agg["by_ratio_bucket"].append(dict(label=label, **_stats(sel)))
    for pos, label in BAND_POSITIONS:
        sel = [r["ret"] for r in rows if r["band_position"] == pos]
        if sel:
            agg["by_band_position"].append(dict(label=label, key=pos, **_stats(sel)))
    for lo, hi, label in LOCKUP_BUCKETS:
        sel = [r["ret"] for r in rows if r["lockup"] is not None and lo <= r["lockup"] < hi]
        if sel:
            agg["by_lockup_bucket"].append(dict(label=label, **_stats(sel)))
    return agg


def print_summary(agg):
    b = agg["baseline"]
    print("\nJoined %d non-SPAC deals, %s .. %s" % (agg["n"], agg["date_min"], agg["date_max"]))
    print("Baseline (subscribe to everything): win %.0f%%  mean %+.1f%%  median %+.1f%%"
          % (b["win_rate"], b["mean"], b["median"]))

    def block(title, items):
        print("\n%-22s %4s  %5s  %8s  %8s" % (title, "n", "win%", "mean", "median"))
        print("-" * 54)
        for it in items:
            print("%-22s %4d  %4.0f%%  %+7.1f%%  %+7.1f%%"
                  % (it["label"], it["n"], it["win_rate"], it["mean"], it["median"]))

    block("기관경쟁률", agg["by_ratio_bucket"])
    block("확정공모가 vs 밴드", agg["by_band_position"])
    block("의무보유확약", agg["by_lockup_bucket"])
    block("Current rule", [dict(label=v, **agg["by_verdict"][v])
                           for v in ("GO", "WATCH", "PASS") if v in agg["by_verdict"]])
    print("\nNote: 시초/공모 return, before fees/tax. Past results do not guarantee future ones.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pages", type=int, default=15, help="list pages to pull (20 rows each)")
    ap.add_argument("--no-write", action="store_true", help="print only, write no files")
    args = ap.parse_args()

    cfg = load_config()
    print("Fetching %d pages of 수요예측결과 + 신규상장 ..." % args.pages)
    rows = collect(args.pages)
    if not rows:
        print("No joined rows -- site layout may have changed.")
        return 1

    rows = evaluate_all(rows, cfg)
    agg = aggregate(rows, cfg)
    print_summary(agg)

    if args.no_write:
        return 0

    payload = {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "source": "38.co.kr (수요예측결과 + 신규상장)",
        "return_metric": "시초/공모 — offer price to opening auction, before fees and tax",
        "pages_fetched": args.pages,
        "config": cfg,
        "aggregates": agg,
        "deals": rows,
    }

    os.makedirs(OUT_DIR, exist_ok=True)
    stamp = datetime.date.today().isoformat()
    written = []
    for name, data in (
        ("%s.json" % stamp, json.dumps(payload, ensure_ascii=False, indent=2)),
        ("latest.json", json.dumps(payload, ensure_ascii=False, indent=2)),
        ("%s.html" % stamp, render_backtest.render(payload)),
        ("latest.html", render_backtest.render(payload)),
    ):
        path = os.path.join(OUT_DIR, name)
        with open(path, "w") as fh:
            fh.write(data)
        written.append(path)
    print("\nWrote:")
    for p in written:
        print("  %s" % p)
    return 0


if __name__ == "__main__":
    sys.exit(main())
