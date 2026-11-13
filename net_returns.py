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

Where OpenDART is set up (key in the keychain, see ipo_radar/dart.py), both
channels are READ instead: the 증권발행실적보고서 lists, per brokerage and per
청약 amount, the 균등 and 비례 shares each application received, and the real
청약건수. Those deals carry alloc_source "dart" (the row at that 청약 amount)
or "dart_interp" (between the two rows either side); the rest fall back to the
formulas above ("38").

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

from ipo_radar import dart, fetch, parse, render_net, score

ROOT = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(ROOT, "state", "cache", "detail")
SRC = os.path.join(ROOT, "state", "backtest", "latest.json")
DATASET = os.path.join(ROOT, "state", "cache", "dataset.json")

MARGIN_RATE_DEFAULT = 0.50   # 일반청약자 청약증거금율; parsed per deal, this is the fallback
EQUAL_POOL_SHARE = 0.50      # 균등 is >= 50% of the 일반청약자 pool by rule since 2021


def cached_detail(no, listing_date=None):
    """The detail page, fetched once it is final and never again.

    A page saved on or before the deal's 상장일 is a snapshot of a deal still in
    progress: 니어스랩, 해치텍, 기도산업 were cached on 2026-08-12, their 청약
    day, with an empty 청약경쟁률, and read for three months as "38 never
    published it". So a cache entry counts only if it postdates listing.
    """
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, "%s.html" % no)
    if os.path.exists(path) and os.path.getsize(path) > 2000:
        saved = datetime.date.fromtimestamp(os.path.getmtime(path)).isoformat()
        if not listing_date or saved > listing_date:
            with open(path, encoding="utf-8") as fh:
                return fh.read()
    raw = fetch.fetch_detail_page(no)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(raw)
    return raw


def extract(no, listing_date=None):
    """Pull the subscription economics out of one detail page."""
    raw = cached_detail(no, listing_date)
    txt = re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", raw)))
    rec = parse.parse_detail(raw, no)

    sr = parse.subscription_ratios(txt)
    total_ratio, prop_ratio = sr["total"], sr["prop"]
    # 비례 is 2 x 통합 whenever both are published: the ratio sat at 1.97..2.02
    # (p2..p98) over all 246 deals that have both, because 균등 takes half the
    # pool. So a missing 비례 (REITs publish 통합 only) or an impossible one
    # (져스텍: '비례 4:1' against 통합 2,784:1, a truncated figure) is filled
    # from 통합 and labelled, rather than dropping the deal.
    prop_source = "38"
    if total_ratio and (not prop_ratio or prop_ratio < total_ratio):
        prop_ratio, prop_source = 2.0 * total_ratio, "estimated"

    mm = re.search(r"일반청약자.*?청약증거금율\s*:\s*(\d+)\s*%", txt)
    margin = int(mm.group(1)) / 100.0 if mm else MARGIN_RATE_DEFAULT

    best = max(rec["underwriters"], key=lambda u: u.get("limit_high") or 0, default=None)
    limit = (best or {}).get("limit_high") or None
    broker = (best or {}).get("name")
    limit_low = (best or {}).get("limit_low")
    limit_source = "38"
    if not limit:
        # 38 published no 청약한도 at all (세나테크놀로지). A deal is not dropped
        # for that: estimate it from the brokerage's own allotment and label it.
        est = parse.estimate_limit(rec["underwriters"])
        if est:
            limit, broker, limit_source = est["shares"], est["broker"], "estimated"

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
        "limit_source": limit_source if limit else None,
        "prop_source": prop_source if prop_ratio else None,
        "retail_pool": retail_pool,
        "subscription_start": rec.get("subscription_start"),
        "tenor3": score.long_tenor_share(rec.get("lockup_breakdown") or {}),
        "subscription_end": rec.get("subscription_end"),
        "refund_date": rec.get("refund_date"),
        "final_price": rec.get("final_price"),
        "broker": broker,
        "limit_low": limit_low,
        "ticker": rec.get("ticker"),
        # 균등 share of the retail pool, derived: P_prop/P_total = 통합률/비례률.
        "equal_split": (1.0 - total_ratio / prop_ratio) if (total_ratio and prop_ratio and prop_ratio >= total_ratio) else None,
    }


def days_between(a, b):
    if not a or not b:
        return None
    d0 = datetime.date(*[int(x) for x in a.split("-")])
    d1 = datetime.date(*[int(x) for x in b.split("-")])
    return (d1 - d0).days


def compute(deal, ec, rate, subscribers, report=None):
    price = ec["final_price"] or deal.get("final_price")
    limit, prop = ec["limit"], ec["prop_ratio"]
    prop_source = ec.get("prop_source")
    # DART's own 비례 ratio -- 청약수량 over the 비례 pool, the same definition
    # 38 uses -- replaces a missing or estimated one (the REITs).
    if report and report["prop_pool"] and prop_source != "38":
        prop, prop_source = report["qty"] / report["prop_pool"], "dart"
    # Name what is missing. "incomplete data" hid three different causes, and
    # two of them were parser bugs rather than gaps on the site.
    if not price:
        return {"skip": "확정공모가 없음"}
    if not prop:
        # Every listed deal had a 청약. An empty figure here is a page fetched
        # too early or a gap on 38 -- a data problem, not a fact about the deal.
        return {"skip": "청약경쟁률 없음 — 상장된 종목이므로 데이터 누락 (페이지 확인)"}
    if prop_source == "estimated" and "리츠" in (deal.get("name") or ""):
        # 2 x 통합 was measured on deals publishing both figures, and no REIT in
        # the sample does -- whether their 균등 split matches is unverified.
        return {"skip": "리츠: 비례 미공개, 2×통합 추정은 리츠에서 미검증"}
    if not limit:
        return {"skip": "청약한도·배정물량 모두 미공개"}

    days = days_between(ec["subscription_end"], ec["refund_date"])
    if days is None or days < 0:
        return {"skip": "청약일/환불일 없음"}
    days = max(days, 1)

    alloc = None
    if report:
        alloc = dart.allocation_at(report, ec["broker"], limit, interpolate=False)
        if alloc is None and ec.get("limit_low") and ec["limit_low"] != limit:
            # 38's upper 청약한도 assumes the offering is upsized by 20%; when it
            # wasn't, the real limit is the low end, and that is the row the
            # filing has (빅웨이브 유진 20,000 vs 38's 24,000; 24 deals).
            alloc = dart.allocation_at(report, ec["broker"], ec["limit_low"], interpolate=False)
            if alloc:
                limit = ec["limit_low"]
        if alloc is None:
            # Nobody applied at exactly that amount (undersubscribed deals list
            # only the amounts people chose): the rows either side, if they
            # agree on the 비례 rate.
            alloc = dart.allocation_at(report, ec["broker"], limit)
    capital = limit * price * ec["margin"]
    if alloc:
        # Read, not computed: that 청약 amount's row at that brokerage. Shares
        # are expected values -- the lottery top-ups are averaged in.
        shares_prop, shares_equal = alloc["shares_prop"], alloc["shares_equal"]
        alloc_source = "dart" if alloc["exact"] else "dart_interp"
    else:
        shares_prop = int(limit // prop)
        shares_equal = 0
        if subscribers and ec["retail_pool"]:
            shares_equal = int((ec["retail_pool"] * EQUAL_POOL_SHARE) // subscribers)
        alloc_source = "38"
    shares = shares_prop + shares_equal

    gain = shares * price * (deal["ret"] / 100.0)
    cost = capital * rate * days / 365.0
    return {
        "capital": capital, "days": days, "shares_prop": shares_prop,
        "shares_equal": shares_equal, "shares": shares, "gain": gain,
        "cost": cost, "net": gain - cost, "limit": limit, "price": price,
        "prop_ratio": prop, "broker": ec["broker"],
        "limit_source": ec.get("limit_source"), "prop_source": prop_source,
        "alloc_source": alloc_source,
        "alloc_interpolated": alloc["interpolated"] if alloc else None,
        "applications": report["apps"] if report else None,
        "equal_split": ec.get("equal_split"),
        # Carried for the forecast dataset, not used by the 실질손익 report.
        "sub_start": ec.get("subscription_start"), "tenor3": ec.get("tenor3"),
        "pool": ec.get("retail_pool"),
    }


def reconcile(report, ec):
    """None if a filing's tables add up to its own summary, else why not.

    Every row read was proven by its own arithmetic, so what is left to show
    is that the rows that matter were all read and none was invented. One
    filing used without this check (워트) added 5,256만 to GO on its own.
    """
    summ = report.get("summary") or {}
    q, fmt = report["qty"], "{:,.0f}".format
    # The tables can allot LESS than the final figure -- 잔여주 and 실권주 go to
    # the 인수인, not to a row -- but never more.
    bound = summ.get("final") or ec.get("retail_pool")
    if bound and report["allotted"] > bound * 1.001:
        return "표 배정수량 %s주 > 최종배정 %s주" % (fmt(report["allotted"]), fmt(bound))
    cands = dart.summary_qty(summ, ec.get("final_price")) if summ else []
    if cands:
        # Below 100%: a table left unread costs only its own rows -- the
        # deal's row is then missing and the formula is used. Above: rows
        # that should not exist. Under 80% the filing was not understood.
        # The filer's 합계 may count NH at its half-net A (LGCNS: exact that
        # way, 0.8% over the other), so either total may match.
        if any(0.80 <= x / c <= 1.001 for c in cands for x in (q, report["qty_printed"])):
            return None
        return "표 청약수량 %s주 ≠ 요약 %s주" % (fmt(q), fmt(cands[0]))
    # No usable 청약수량: the 건수, which no filer has copied, plus 38's ratio.
    # 38's is announced before 중복·부적격 were voided, so it only has to agree
    # within 15% (they differ by 3-12% routinely).
    alloc = summ.get("alloc") or ec.get("retail_pool")
    if not alloc or not ec.get("total_ratio"):
        return "요약 청약수량 없음, 38 경쟁률도 없어 검증 불가"
    if summ.get("apps") and not 0.80 <= report["apps"] / summ["apps"] <= 1.001:
        return "표 청약건수 %s건 ≠ 요약 %s건" % (fmt(report["apps"]), fmt(summ["apps"]))
    ratio = q / float(alloc)
    if abs(ratio / ec["total_ratio"] - 1) > 0.15:
        return "요약 청약수량 없음: 표 %.2f vs 38 %.2f" % (ratio, ec["total_ratio"])
    return None


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


BAND_POS = {"above": 2, "top": 1, "within": 0, "bottom": -1, "below": -2}


def build_dataset(rows):
    """The analogue set `forecast.py` learns from, and `evaluate.py` validates on.

    This is the only writer of `state/cache/dataset.json`. It is a projection of
    the same rows the 실질손익 report is built from, reshaped to what the forecast
    needs: the features it kernel-weights on (기관경쟁률, 확약, and the tenor/band
    fields kept for re-testing), and the two outcomes it draws jointly -- the
    retail-enthusiasm multiple `mult` and the opening move `ret`.

    Two invariants matter more than they look:

    * **비례 only, always.** `shares` and `net` are recomputed from `shares_prop`
      even when --subscribers assumed a 균등 allocation, so the dataset does not
      change shape with the flag. `forecast.predict` adds 균등 itself via
      `equal_shares`; baking an assumed one in here would double-count it.
    * **`mult` = 비례경쟁률 / 기관경쟁률** -- how much harder retail competed than
      institutions did. It is the whole reason the forecast can turn a 수요예측
      number into an allocation, and it is drawn jointly with that deal's `ret`.

    Deals missing 기관경쟁률 or 청약일 are dropped: without them there is no
    feature to weight on and no way to date the leakage cutoff.
    """
    out = []
    for r in rows:
        inst, prop = r.get("ratio"), r["prop_ratio"]
        if not inst or not prop or not r.get("sub_start"):
            continue
        shares = r["shares_prop"]
        gain = shares * r["price"] * (r["ret"] / 100.0)
        out.append({
            "no": r["no"], "name": r["name"],
            "sub_start": r["sub_start"], "listing": r["listing_date"],
            "inst": inst, "lock": r.get("lockup") or 0.0,
            "tenor3": r.get("tenor3"), "bpos": BAND_POS.get(r.get("band_position"), 0),
            "limit": r["limit"], "price": r["price"], "pool": r.get("pool"),
            "days": r["days"], "capital": r["capital"], "cost": r["cost"],
            "mult": prop / inst, "prop": prop,
            "ret": r["ret"], "net": gain - r["cost"], "shares": shares,
            # An analogue needs only mult and ret, so an estimated 청약한도
            # costs it nothing as history. Its realised net is built on a
            # guessed limit, though, so evaluate.py does not score it as a
            # target.
            "estimated": r.get("limit_source") == "estimated",
        })
    out.sort(key=lambda d: d["sub_start"])
    return out


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
    key = dart.api_key()
    if not key:
        print("OpenDART key not in keychain (service %s) — 균등 stays an assumption." % dart.KEYCHAIN_SERVICE)
    rows, skipped, dart_miss, mismatch, differs_38 = [], [], [], [], []
    prospectus_miss, limit_diff = [], []
    for i, d in enumerate(deals, 1):
        try:
            ec = extract(d["no"], d.get("listing_date"))
        except Exception as exc:
            skipped.append((d["name"], str(exc)[:40]))
            continue
        report = None
        if key and ec.get("ticker") and ec.get("subscription_start"):
            try:
                xml = dart.report_xml(ec["ticker"], ec["subscription_start"], d["listing_date"], key,
                                      name=d["name"])
                report = dart.parse_report(xml) if xml else None
                if report and not report["tables"]:
                    dart_miss.append((d["name"], "실적보고서에 배정표 없음"))
                    report = None
                elif xml is None:
                    dart_miss.append((d["name"], dart.LAST_ERROR or "?"))
            except Exception as exc:   # DART trouble must not cost the 38 figures
                dart_miss.append((d["name"], "DART 오류: %s" % str(exc)[:50]))
        if key and ec.get("ticker") and ec.get("subscription_start") and report:
            # The 청약한도 from the 투자설명서, not 38. 38's figure is a different
            # tier from brokerage to brokerage -- 한투/삼성/IBK print their 50%
            # tier, 대신 sometimes its 200%, 신영 100-300% -- and was another
            # brokerage's limit outright on several deals (필에너지: 미래 shown
            # 삼성's 5,000~6,000; 케이엔알: DB shown NH's 150%).
            try:
                s0 = datetime.date(*map(int, ec["subscription_start"].split("-")))
                px = dart.report_xml(ec["ticker"], (s0 - datetime.timedelta(days=60)).isoformat(),
                                     ec.get("subscription_end") or ec["subscription_start"], key,
                                     name=d["name"], kind=dart.PROSPECTUS)
                pl = dart.prospectus_limit(dart.parse_limits(px), (report.get("summary") or {}).get("alloc"),
                                           prefer=ec.get("broker"), report=report) if px else None
            except Exception as exc:
                pl = None
                prospectus_miss.append((d["name"], "투자설명서 오류: %s" % str(exc)[:50]))
            if pl:
                # 38's low end counts as agreeing: its high end is an upsizing.
                if pl["limit"] not in (ec.get("limit"), ec.get("limit_low")) \
                        or not dart._same_broker(pl["broker"], ec.get("broker")):
                    limit_diff.append((d["name"], ec.get("broker"), ec.get("limit_low"), ec.get("limit"),
                                       pl["broker"], pl["limit"]))
                ec = dict(ec, broker=pl["broker"], limit=pl["limit"], limit_low=None,
                          limit_source="prospectus_derived" if pl["derived"] else "prospectus")
            else:
                prospectus_miss.append((d["name"], dart.LAST_ERROR or "투자설명서 청약한도 미해석"))
        if report:
            why = reconcile(report, ec)
            if why:
                mismatch.append((d["name"], why))
                report = None
        denom = ((report.get("summary") or {}).get("alloc") or ec.get("retail_pool")) if report else None
        if report and ec.get("total_ratio") and denom:
            # Information, not a failure: 38 reports the ratio announced at
            # close, the filing the final valid one after 중복·부적격 were voided
            # (빅웨이브: dealsite 1375.34 = 38; alphasquare 1307.78 = filing).
            # Denominator is the 일반 allotment, not the tables' own B + C,
            # which leave out 잔여주 and 실권주 (that made 아이씨티케이 look like
            # 1899.67 against a reported 1,107.95).
            dart_total = report["qty"] / float(denom)
            if abs(dart_total / ec["total_ratio"] - 1) > 0.02:
                differs_38.append((d["name"], ec["total_ratio"], dart_total))
        r = compute(d, ec, rate, args.subscribers, report)
        if report and "skip" not in r and r.get("alloc_source") not in ("dart", "dart_interp"):
            # Found the filing but could not read this deal's row from it.
            dart_miss.append((d["name"], "실적보고서는 있으나 %s %s주 행 없음 (브로커 %s)" % (
                ec.get("broker"), "{:,}".format(ec.get("limit") or 0),
                ", ".join(str(t["broker"]) for t in report["tables"]))))
        if "skip" in r:
            skipped.append((d["name"], r["skip"]))
            continue
        r.update(name=d["name"], verdict=d["verdict"], ret=d["ret"], listing_date=d["listing_date"],
                 no=d["no"], ratio=d.get("ratio"), lockup=d.get("lockup"),
                 band_position=d.get("band_position"))
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
        # Every one, by name: a truncated list is how 6 of 12 went unread.
        print("\nSkipped (%d):" % len(skipped))
        for name, why in skipped:
            print("  %-20s %s" % (name[:19], why))
    n_dart = sum(1 for r in rows if r.get("alloc_source") == "dart")
    n_tot = sum(1 for r in rows if r.get("alloc_source") == "dart_interp")
    print("\nAllocation read from DART 실적보고서: %d of %d deals exact, +%d interpolated between two rows (rest: 38 formula)"
          % (n_dart, len(rows), n_tot))
    n_px = sum(1 for r in rows if (r.get("limit_source") or "").startswith("prospectus"))
    print("청약한도 from the 투자설명서: %d of %d deals (%d derived from another tier); 38 differed on %d"
          % (n_px, len(rows), sum(1 for r in rows if r.get("limit_source") == "prospectus_derived"),
             len(limit_diff)))
    for name, b38, lo38, hi38, b, lim in limit_diff:
        print("  %-20s 38 %s %s~%s  ->  투자설명서 %s %s" % (name[:19], b38, lo38, hi38, b, "{:,}".format(lim)))
    if prospectus_miss:
        print("투자설명서 limit not used (%d) — 38's instead:" % len(prospectus_miss))
        for name, why in prospectus_miss:
            print("  %-20s %s" % (name[:19], why))
    if dart_miss:
        print("DART not used (%d):" % len(dart_miss))
        for name, why in dart_miss:
            print("  %-20s %s" % (name[:19], why))
    if mismatch:
        print("\n⚠ DART filing does not reconcile with itself (%d) — not used, 38 formula instead:" % len(mismatch))
        for name, why in mismatch:
            print("  %-20s %s" % (name[:19], why))
    if differs_38:
        print("\n38 통합경쟁률 differs from the filing by >2%% (%d) — filing used; 38 is the announced ratio:" % len(differs_38))
        for name, a38, adart in differs_38:
            print("  %-20s 38 %.2f  filing %.2f  (%+.1f%%)" % (name[:19], a38, adart, 100 * (a38 / adart - 1)))
    est = [r for r in rows if r.get("limit_source") == "estimated" or r.get("prop_source") == "estimated"]
    if est:
        print("\nEstimated inputs (%d) — included above, labelled in the outputs:" % len(est))
        for r in est:
            print("  %-20s %-5s %s%s  순 %+11s" % (
                r["name"][:19], r["verdict"],
                "청약한도 추정 %s주" % "{:,}".format(r["limit"]) if r.get("limit_source") == "estimated" else "",
                " 비례 추정 %.0f:1" % r["prop_ratio"] if r.get("prop_source") == "estimated" else "",
                "{:,.0f}".format(r["net"])))

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

    # The forecast's analogue set. Written here because this is the only place
    # that has both halves of it: the 수요예측 features from the backtest and the
    # realised allocation from the detail pages.
    dataset = build_dataset(rows)
    os.makedirs(os.path.dirname(DATASET), exist_ok=True)
    with open(DATASET, "w", encoding="utf-8") as fh:
        json.dump(dataset, fh, ensure_ascii=False, indent=2)
    written.append("%s  (%d deals for forecast.py)" % (DATASET, len(dataset)))

    print("\nWrote:")
    for w in written:
        print("  %s" % w)
    return 0


if __name__ == "__main__":
    sys.exit(main())
