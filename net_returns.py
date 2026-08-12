#!/usr/bin/env python3
"""What the screen would actually have earned, in won, net of opportunity cost.

시초/공모 measures the price move, not the trade. Subscribing means committing
증거금 (50% of 청약한도 x 확정공모가) from the 청약일 until the 환불일, during which
that cash earns nothing -- and on a hot deal a 억-won commitment is rewarded with
a handful of shares. This models the actual cash outcome:

    net = shares_received x 확정공모가 x (시초/공모) - committed_capital x r x days/365

Allocation has two channels:

  비례 (pro-rata)  floor(청약한도 / 비례경쟁률)   -- computed exactly, 38.co.kr
                                                publishes 비례경쟁률 per deal
  균등 (equal)     floor(균등물량 / 청약건수)     -- 청약건수 (the number of retail
                                                subscribers) is NOT published
                                                anywhere on 38.co.kr, so it is an
                                                assumption, not a measurement

Everything reported as "비례 only" is therefore a hard floor on the true result.

    ./net_returns.py --rate 3.0 --subscribers 150000
    ./net_returns.py --rate 3.0 --subscribers 0     # 비례 only, no assumption
"""

import argparse
import datetime
import html
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ipo_radar import fetch, parse, render_net

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, "state", "cache", "detail")
SRC = os.path.join(ROOT, "state", "backtest", "latest.json")

MARGIN_RATE_DEFAULT = 0.50   # 일반청약자 청약증거금율; parsed per deal, this is the fallback
EQUAL_POOL_SHARE = 0.50      # 균등 is >= 50% of the 일반청약자 pool by rule since 2021


def cached_detail(no):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, "%s.html" % no)
    if os.path.exists(path) and os.path.getsize(path) > 2000:
        with open(path, encoding="utf-8") as fh:
            return fh.read()
    raw = fetch.fetch_detail_page(no)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(raw)
    return raw


def extract(no):
    """Pull the subscription economics out of one detail page."""
    raw = cached_detail(no)
    txt = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw)))
    rec = parse.parse_detail(raw, no)

    m = re.search(r"청약경쟁률\s*([\d,.]+):1\s*\(비례\s*([\d,.]+):1\)", txt)
    total_ratio = float(m.group(1).replace(",", "")) if m else None
    prop_ratio = float(m.group(2).replace(",", "")) if m else None

    mm = re.search(r"일반청약자.*?청약증거금율\s*:\s*(\d+)\s*%", txt)
    margin = int(mm.group(1)) / 100.0 if mm else MARGIN_RATE_DEFAULT

    limit = max([u.get("limit_high") or 0 for u in rec["underwriters"]] or [0]) or None

    retail_pool = None
    if rec.get("retail_allocation"):
        rm = re.search(r"([\d,]+)", rec["retail_allocation"])
        if rm:
            retail_pool = int(rm.group(1).replace(",", ""))

    return {
        "total_ratio": total_ratio,
        "prop_ratio": prop_ratio,
        "margin": margin,
        "limit": limit,
        "retail_pool": retail_pool,
        "subscription_end": rec.get("subscription_end"),
        "refund_date": rec.get("refund_date"),
        "final_price": rec.get("final_price"),
        "broker": (rec["underwriters"][0]["name"] if rec.get("underwriters") else None),
        # 균등 share of the retail pool, derived: P_prop/P_total = 통합률/비례률.
        "equal_split": (1.0 - total_ratio / prop_ratio) if (total_ratio and prop_ratio and prop_ratio >= total_ratio) else None,
    }


def days_between(a, b):
    if not a or not b:
        return None
    d0 = datetime.date(*[int(x) for x in a.split("-")])
    d1 = datetime.date(*[int(x) for x in b.split("-")])
    return (d1 - d0).days


def compute(deal, ec, rate, subscribers):
    price = ec["final_price"] or deal.get("final_price")
    limit, prop = ec["limit"], ec["prop_ratio"]
    if not price or not limit or not prop:
        return None
    # 비례 pool is half the retail pool, so its ratio must exceed the combined
    # ratio. A published 비례 below it is a site data error (e.g. a truncated
    # figure) and would fabricate an enormous allocation -- drop those.
    if ec["total_ratio"] and prop < ec["total_ratio"]:
        return {"skip": "비례경쟁률 %.0f < 통합 %.0f (source data error)" % (prop, ec["total_ratio"])}

    days = days_between(ec["subscription_end"], ec["refund_date"])
    if days is None or days < 0:
        return {"skip": "청약일/환불일 없음"}
    days = max(days, 1)

    capital = limit * price * ec["margin"]
    shares_prop = int(limit // prop)
    shares_equal = 0
    if subscribers and ec["retail_pool"]:
        shares_equal = int((ec["retail_pool"] * EQUAL_POOL_SHARE) // subscribers)
    shares = shares_prop + shares_equal

    gain = shares * price * (deal["ret"] / 100.0)
    cost = capital * rate * days / 365.0
    return {
        "capital": capital, "days": days, "shares_prop": shares_prop,
        "shares_equal": shares_equal, "shares": shares, "gain": gain,
        "cost": cost, "net": gain - cost, "limit": limit, "price": price,
        "prop_ratio": prop, "broker": ec["broker"],
        "equal_split": ec.get("equal_split"),
    }


def summarize(label, items):
    if not items:
        return "%-8s  (none)" % label
    n = len(items)
    net = [i["net"] for i in items]
    wins = sum(1 for x in net if x > 0)
    tot_net, tot_gain, tot_cost = sum(net), sum(i["gain"] for i in items), sum(i["cost"] for i in items)
    avg_cap = sum(i["capital"] for i in items) / n
    avg_sh = sum(i["shares"] for i in items) / float(n)
    return ("%-8s n=%-4d net-win %3.0f%%  총이익 %+12s  총기회비용 %12s  순손익 %+13s  "
            "평균 %+9s/건  평균배정 %4.1f주  평균투입 %s"
            % (label, n, 100.0 * wins / n, "{:,.0f}".format(tot_gain), "{:,.0f}".format(tot_cost),
               "{:,.0f}".format(tot_net), "{:,.0f}".format(tot_net / n), avg_sh,
               "{:,.0f}".format(avg_cap)))


def _bucket(items):
    if not items:
        return None
    net = [i["net"] for i in items]
    cap_years = sum(i["capital"] * i["days"] for i in items) / 365.0
    return {
        "n": len(items),
        "net": sum(net),
        "gain": sum(i["gain"] for i in items),
        "cost": sum(i["cost"] for i in items),
        "net_win_rate": round(100.0 * sum(1 for x in net if x > 0) / len(items), 1),
        "avg_shares": sum(i["shares"] for i in items) / float(len(items)),
        "avg_capital": sum(i["capital"] for i in items) / float(len(items)),
        "capital_years": cap_years,
        "excess_return": (100.0 * sum(net) / cap_years) if cap_years else 0.0,
    }


def build_payload(rows, args):
    agg = {"by_verdict": {}, "all": _bucket(rows),
           "date_min": min(r["listing_date"] for r in rows),
           "date_max": max(r["listing_date"] for r in rows),
           "pct_view_win_rate": round(100.0 * sum(1 for r in rows if r["ret"] > 0) / len(rows), 1),
           "equal_split_5050": sum(1 for r in rows if abs(r.get("equal_split", 0.5) - 0.5) <= 0.02),
           "equal_split_checked": len(rows)}
    for v in ("GO", "WATCH", "PASS"):
        b = _bucket([r for r in rows if r["verdict"] == v])
        if b:
            agg["by_verdict"][v] = b
    return {
        "generated_at": datetime.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "params": {"rate": args.rate, "subscribers": args.subscribers,
                   "allocation": "비례 only" if not args.subscribers else "비례 + 균등(assumed)"},
        "aggregates": agg,
        "deals": rows,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--rate", type=float, default=3.0, help="annual opportunity cost %%")
    ap.add_argument("--subscribers", type=int, default=150000,
                    help="assumed 청약건수 for 균등; 0 disables 균등 entirely")
    ap.add_argument("--limit", type=int, default=0, help="only process N deals (debug)")
    ap.add_argument("--no-write", action="store_true", help="print only, write no files")
    args = ap.parse_args()
    rate = args.rate / 100.0

    with open(SRC, encoding="utf-8") as fh:
        deals = [d for d in json.load(fh)["deals"] if d.get("no")]
    if args.limit:
        deals = sorted(deals, key=lambda d: d["listing_date"], reverse=True)[:args.limit]

    print("Fetching %d detail pages (cached after first run)..." % len(deals))
    rows, skipped = [], []
    for i, d in enumerate(deals, 1):
        try:
            ec = extract(d["no"])
        except Exception as exc:
            skipped.append((d["name"], str(exc)[:40]))
            continue
        r = compute(d, ec, rate, args.subscribers)
        if r is None:
            skipped.append((d["name"], "incomplete data"))
            continue
        if "skip" in r:
            skipped.append((d["name"], r["skip"]))
            continue
        r.update(name=d["name"], verdict=d["verdict"], ret=d["ret"], listing_date=d["listing_date"])
        rows.append(r)
        if i % 25 == 0:
            sys.stderr.write("  %d/%d\r" % (i, len(deals)))
    sys.stderr.write("\n")

    print("\n%d deals modelled, %d skipped" % (len(rows), len(skipped)))
    print("Assumptions: 최대 청약한도 at the single best broker, 증거금율 as published"
          " (50%%), opportunity cost %.1f%%/yr from 청약 종료일 to 환불일," % args.rate)
    print("             균등 = floor(균등물량 / %s)%s"
          % ("{:,}".format(args.subscribers) if args.subscribers else "n/a",
             "" if args.subscribers else "  [DISABLED — 비례 only, a hard floor]"))

    print("\n%s" % ("-" * 150))
    for v in ("GO", "WATCH", "PASS"):
        print(summarize(v, [r for r in rows if r["verdict"] == v]))
    print(summarize("ALL", rows))
    print("%s" % ("-" * 150))

    gos = [r for r in rows if r["verdict"] == "GO"]
    if gos:
        print("\nGO deals — worst 8 by net won:")
        for r in sorted(gos, key=lambda x: x["net"])[:8]:
            print("  %-18s %s  배정 %2d주 (비례 %2d)  투입 %11s  이익 %+10s  비용 %9s  순 %+11s  시초/공모 %+.0f%%"
                  % (r["name"][:17], r["listing_date"], r["shares"], r["shares_prop"],
                     "{:,.0f}".format(r["capital"]), "{:,.0f}".format(r["gain"]),
                     "{:,.0f}".format(r["cost"]), "{:,.0f}".format(r["net"]), r["ret"]))
        print("\nGO deals — best 5 by net won:")
        for r in sorted(gos, key=lambda x: -x["net"])[:5]:
            print("  %-18s %s  배정 %2d주 (비례 %2d)  투입 %11s  이익 %+10s  비용 %9s  순 %+11s  시초/공모 %+.0f%%"
                  % (r["name"][:17], r["listing_date"], r["shares"], r["shares_prop"],
                     "{:,.0f}".format(r["capital"]), "{:,.0f}".format(r["gain"]),
                     "{:,.0f}".format(r["cost"]), "{:,.0f}".format(r["net"]), r["ret"]))
    if skipped:
        print("\nSkipped (%d): %s" % (len(skipped), ", ".join("%s [%s]" % s for s in skipped[:6])))

    if args.no_write:
        return 0
    payload = build_payload(rows, args)
    out_dir = os.path.join(ROOT, "state", "backtest")
    os.makedirs(out_dir, exist_ok=True)
    stamp = datetime.date.today().isoformat()
    written = []
    blob = json.dumps(payload, ensure_ascii=False, indent=2)
    page = render_net.render(payload)
    for name, data in (("net-%s.json" % stamp, blob), ("net-latest.json", blob),
                       ("net-%s.html" % stamp, page), ("net-latest.html", page)):
        path = os.path.join(out_dir, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(data)
        written.append(path)
    print("\nWrote:")
    for w in written:
        print("  %s" % w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
