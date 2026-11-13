#!/usr/bin/env python3
"""공모주 레이더 — daily Korean IPO subscription screen.

Scrapes 38.co.kr for upcoming 공모청약, pulls the 수요예측 (institutional
demand-forecast) result for each, scores it, and raises a macOS notification for
anything rated GO.

Decision support only -- it never places an order.

Usage:
    ./run.py                 # normal daily run
    ./run.py --all           # ignore the lead-time window, score everything upcoming
    ./run.py --no-notify     # write the report, stay quiet
    ./run.py --force         # re-notify even if already alerted
    ./run.py --test-email    # verify email delivery without waiting for a GO
    ./run.py --test-calendar # verify calendar access without waiting for a GO
"""

import argparse
import datetime
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ipo_radar import calendar_sync, dart, fetch, forecast, notify, parse, report, score

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(ROOT, "state")
REPORT_DIR = os.path.join(STATE_DIR, "reports")
SEEN_PATH = os.path.join(STATE_DIR, "seen.json")
# Which calendar events already exist, per deal. Kept apart from seen.json:
# that file records that an ALERT was delivered, this one records that an
# EVENT was created, and the two advance independently -- a deal is alerted
# once but may gain its 매도 event days later when 상장일 publishes.
CALENDAR_PATH = os.path.join(STATE_DIR, "calendar.json")
# Parse warnings already bannered, per deal, so a persistent one alerts once.
WARNED_PATH = os.path.join(STATE_DIR, "warned.json")
CONFIG_PATH = os.path.join(ROOT, "config.json")
# Machine-local overrides, gitignored. Anything personal to one machine or one
# person -- the alert address, say -- belongs here rather than in the tracked
# config, which is public. Applied last, so it wins over config.json.
LOCAL_CONFIG_PATH = os.path.join(ROOT, "config.local.json")


def load_config():
    cfg = dict(score.DEFAULT_CONFIG)
    cfg.update(calendar_sync.CALENDAR_DEFAULTS)
    for path in (CONFIG_PATH, LOCAL_CONFIG_PATH):
        if os.path.exists(path):
            with open(path, "r") as fh:
                cfg.update(json.load(fh))
    return cfg


def load_seen():
    if os.path.exists(SEEN_PATH):
        try:
            with open(SEEN_PATH, "r") as fh:
                return json.load(fh)
        except ValueError:
            return {}
    return {}


def save_seen(seen):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(SEEN_PATH, "w") as fh:
        json.dump(seen, fh, ensure_ascii=False, indent=2, sort_keys=True)


def _load_json(path):
    try:
        with open(path, "r") as fh:
            return json.load(fh)
    except (IOError, OSError, ValueError):
        return {}


def _save_json(path, data):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(path, "w") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=2, sort_keys=True)


def load_calendar_state():
    if os.path.exists(CALENDAR_PATH):
        try:
            with open(CALENDAR_PATH, "r") as fh:
                return json.load(fh)
        except ValueError:
            return {}
    return {}


def save_calendar_state(state):
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(CALENDAR_PATH, "w") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=2, sort_keys=True)


def select_candidates(schedule, cfg, today, take_all=False):
    """Deals whose subscription window is open or opens within the lead window."""
    horizon = (today + datetime.timedelta(days=cfg["alert_lead_days"])).isoformat()
    today_s = today.isoformat()
    out = []
    for row in schedule:
        if not cfg["include_spac"] and parse.is_spac(row["name"]):
            continue
        end = row.get("subscription_end") or row.get("subscription_start")
        start = row.get("subscription_start")
        if not start:
            continue
        if end and end < today_s:
            continue  # window already closed
        if not take_all and start > horizon:
            continue  # too far out to act on
        out.append(row)
    return out


DATASET = os.path.join(STATE_DIR, "cache", "dataset.json")


def my_limit(rec, cfg, limits=None, limits_note=None):
    """Your own maximum 청약한도 for this deal, and which brokerage gives it.

    The source is the deal's final 투자설명서 wherever it was read
    (``limits``, from ``dart.live_limits``): it prints each brokerage's tiers
    (삼성: 50% / 100% / 200%), and your tier is a percentage of that
    brokerage's 100% base, kept in the gitignored config.local.json as
    {"my_tier_pct": {"키움증권": 150}}. The printed tier figure is used as is;
    only a tier the prospectus does not print is base x pct. A brokerage with
    no entry gets its 100% base.

    38 is the fallback, per brokerage, for any the prospectus left unread or
    when there is no prospectus (no DART key, not filed yet). 38 publishes one
    range per brokerage and never mentions tiers, and its low end is not one
    tier: the 투자설명서 audit (2026-11-12) found it the 50% tier at 한투 on
    14 of 33 deals and the 100% tier on the rest. So the fallback keeps its
    own key, ``my_limit_multiplier``, a multiple of 38's low end, and never
    shares numbers with ``my_tier_pct``; a brokerage with no entry falls back
    to the top of 38's range.

    Returns {"broker", "shares", "multiplier", "source"}; multiplier is None
    when no personal tier applies. ``source`` is "prospectus" or "38", and
    ``l38`` carries 38's range for comparison. A prospectus that still
    prints a pre-pricing range gives its low end, flagged ``ranged`` -- with
    no 실적보고서 yet there is nothing to settle it by, and 22 of 27 such
    deals in the backtest ran the low end. When neither source has a limit it
    is estimated from the brokerage's allotment (`parse.estimate_limit`) and
    marked {"estimated": True, "low", "high"}: a GO must not lose its numbers
    just because the limit is not out yet.
    """
    # Compare without a trailing 증권: 38 occasionally truncates a name (one
    # 신한 deal reads "신한투자" against 21 reading "신한투자증권"), and an exact
    # match would miss it silently, falling back to the standard limit.
    norm = lambda n: (n or "").strip()[:-2] if (n or "").strip().endswith("증권") else (n or "").strip()
    mult = {norm(k): v for k, v in (cfg.get("my_limit_multiplier") or {}).items()}
    tier = cfg.get("my_tier_pct") or {}
    # `limit_base_override` ({"<no>": {"<broker>": base}}) corrects the base
    # for one deal by hand (동원파츠 read 삼성 12,500 on 38 against 삼성's own
    # 25,000, before the 투자설명서 was read). It replaces the base, not the
    # result: the tier still multiplies it.
    base_fix = {norm(k): int(v) for k, v in
                ((cfg.get("limit_base_override") or {}).get(rec.get("no"), {}) or {}).items()}
    # Brokerages you hold no account at (`no_account` in config.local.json).
    # They still compete on shares: the point is to be told a GO there is worth
    # opening an account for, so the alert carries `needs_account` rather than
    # quietly falling back to a token 인수단 allocation elsewhere (유캐스트:
    # 유진 20,000주 vs 신영 1,300주).
    no_acct = list(cfg.get("no_account") or [])
    unders = rec.get("underwriters", [])

    def lookup(table, name):
        return next((v for k, v in table.items() if dart._same_broker(k, name)), None)

    cands = []
    read = set()
    for b, v in (limits or {}).items():
        if not v.get("base"):
            continue
        u = next((u for u in unders if dart._same_broker(u.get("name"), b)), None)
        name = u["name"] if u else b
        read.add(norm(name))
        fixed = lookup(base_fix, name)
        base = fixed or v["base"][0]
        pct = lookup(tier, name)
        p = float(pct) if pct else 100.0
        # Tier keys are whole percentages; 200.0 finds 200.
        if not fixed and p in v["tiers"]:
            shares = v["tiers"][p][0]
        else:
            shares = int(round(base * p / 100.0))
        cand = {"broker": name, "shares": int(shares), "source": "prospectus",
                "multiplier": float(pct) / 100.0 if pct else None, "base": base,
                "l38": (u.get("limit_low"), u.get("limit_high")) if u else None}
        if v["base"][0] != v["base"][1]:
            cand["ranged"] = tuple(v["base"])
        cands.append(cand)
    # A brokerage on 38 that the prospectus does not list at all is taken
    # from 38 only when the prospectus left some brokerage unread; when it
    # read every one, the extra name is 38's error (산일전기: 삼성 shown with
    # 미래's limit), as the backtest found.
    complete = bool(limits) and all(v.get("base") for v in limits.values())
    for u in unders:
        name = u.get("name")
        if norm(name) in read:
            continue
        if complete and not any(dart._same_broker(name, b) for b in limits):
            continue
        low = base_fix.get(norm(name)) or u.get("limit_low")
        if norm(name) in mult and low:
            m = float(mult[norm(name)])
            cands.append({"broker": name, "shares": int(low * m), "multiplier": m, "source": "38"})
        elif u.get("limit_high"):
            cands.append({"broker": name, "shares": int(u["limit_high"]), "multiplier": None,
                          "source": "38"})
    best = None
    for cand in cands:
        # By _same_broker: the prospectus spells 유진투자증권 "유진증권".
        cand["needs_account"] = any(dart._same_broker(k, cand["broker"]) for k in no_acct)
        if best is None or cand["shares"] > best["shares"]:
            best = cand
    if best is None:
        est = parse.estimate_limit(unders)
        if est:
            best = {"broker": est["broker"], "shares": est["shares"], "multiplier": None,
                    "estimated": True, "low": est["low"], "high": est["high"], "note": est["note"],
                    "needs_account": any(dart._same_broker(k, est["broker"]) for k in no_acct),
                    "source": "estimate"}
    if best is not None and best["source"] != "prospectus" and limits_note:
        best["limits_note"] = limits_note
    return best


def attach_forecasts(results, cfg=None):
    """Add an expected-net-won estimate to GO/WATCH deals, where we can.

    Silently skipped when the historical dataset has not been built (it needs
    one cached detail page per past deal -- see net_returns.py), so the daily
    screen never depends on it.
    """
    if not os.path.exists(DATASET):
        return
    try:
        with open(DATASET, encoding="utf-8") as fh:
            history = json.load(fh)
    except ValueError:
        return
    key = dart.api_key()
    for r in results:
        if r["verdict"]["verdict"] not in ("GO", "WATCH"):
            continue
        rec = r["rec"]
        # Forecast at YOUR limit: allocation and 증거금 both scale with it, so a
        # 200% 우대 roughly doubles the expected won. The backtest and
        # net_returns.py deliberately stay on each brokerage's 100% tier -- the
        # screen is judged on the market, not on one person's account status.
        # The limits come from the 투자설명서, fetched only for GO/WATCH: two
        # DART calls a deal at most, at the same 1s pace.
        limits, why = dart.live_limits(rec, key)
        mine = my_limit(rec, cfg or {}, limits, why)
        rec["my_limit"] = mine
        limit = mine["shares"] if mine else 0
        price, sub_end, refund = rec.get("final_price"), rec.get("subscription_end"), rec.get("refund_date")
        if not (limit and price and sub_end and refund and rec.get("institutional_ratio")):
            continue
        d0 = datetime.date(*[int(x) for x in sub_end.split("-")])
        d1 = datetime.date(*[int(x) for x in refund.split("-")])
        days = max((d1 - d0).days, 1)
        capital = limit * price * 0.5
        target = {
            "no": rec["no"], "sub_start": rec.get("subscription_start") or sub_end,
            "inst": rec["institutional_ratio"], "lock": rec.get("lockup_pct") or 0.0,
            "limit": limit, "price": price, "days": days,
            "capital": capital, "cost": capital * 0.03 * days / 365.0,
        }
        hist = [h for h in history if h["listing"] < target["sub_start"]]
        p = forecast.predict(target, hist, bootstrap=800)
        if p:
            p["capital"] = capital
            p["limit_estimated"] = bool(mine.get("estimated"))
            r["forecast"] = p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="ignore the lead-time window")
    ap.add_argument("--no-notify", action="store_true", help="write report only")
    ap.add_argument("--force", action="store_true", help="re-notify already-alerted deals")
    ap.add_argument("--test-email", action="store_true",
                    help="send one test email and exit, to verify delivery")
    ap.add_argument("--test-calendar", action="store_true",
                    help="verify Google Calendar access, then exit")
    args = ap.parse_args()

    cfg = load_config()

    if args.test_calendar:
        ok, detail = calendar_sync.self_test(cfg)
        print("calendar access: %s" % ("OK" if ok else "FAILED"))
        print("  %s" % detail)
        return 0 if ok else 1

    if args.test_email:
        # The failure mode of an email alert is silence, so make it provable
        # without waiting for a real GO.
        to = cfg.get("email_to") or "(unset)"
        ok = notify.send_email(
            "[공모주 레이더] 테스트 메일",
            "이 메일이 도착했다면 GO 알림이 이메일로 전달됩니다.\n\n"
            "설정: %s -> %s (%s:%s)\n" % (
                cfg.get("email_from"), to, cfg.get("smtp_host"), cfg.get("smtp_port")),
            cfg,
        )
        print("test email to %s: %s" % (to, "SENT" if ok else "FAILED"))
        if not ok:
            sys.stderr.write("reason: %s\n" % (notify.LAST_ERROR or "unknown"))
        return 0 if ok else 1
    today = datetime.date.today()
    run_date = today.isoformat()

    schedule = parse.parse_schedule_list(fetch.fetch_schedule_page())
    candidates = select_candidates(schedule, cfg, today, take_all=args.all)

    # 수요예측 results land on a separate page; index by 38.co.kr's `no`.
    demand_warn = []
    demand = {d["no"]: d for d in parse.parse_demand_results(fetch.fetch_demand_results_page(), demand_warn)
              if d["no"]}

    results = []
    for row in candidates:
        try:
            rec = parse.parse_detail(fetch.fetch_detail_page(row["no"]), row["no"])
        except Exception as exc:  # one bad page must not kill the run
            sys.stderr.write("warn: detail fetch failed for %s (%s): %s\n" % (row["name"], row["no"], exc))
            continue
        # The list page carries the schedule; fall back to it where the detail
        # page is sparse, and backfill 수요예측 from the results page.
        rec.setdefault("name", row["name"])
        if not rec.get("name"):
            rec["name"] = row["name"]
        if rec.get("institutional_ratio") is None and row["no"] in demand:
            rec["institutional_ratio"] = demand[row["no"]]["institutional_ratio"]
            if rec.get("lockup_pct") is None:
                rec["lockup_pct"] = demand[row["no"]]["lockup_pct"]
        if not rec.get("subscription_start"):
            rec["subscription_start"] = row["subscription_start"]
            rec["subscription_end"] = row["subscription_end"]
        # 확정공모가 and 기관경쟁률 publish together, from the same 수요예측. A
        # price with no ratio after the backfill means a ratio we failed to
        # read -- 엠비디 sat at PENDING on its 청약일 exactly so.
        if rec.get("final_price") and rec.get("institutional_ratio") is None:
            rec.setdefault("parse_warnings", []).append(
                "확정공모가는 공시됐는데 기관경쟁률이 없음 — 파싱 확인 (PENDING 이 아닐 수 있음)")
        results.append({"rec": rec, "verdict": score.evaluate(rec, cfg)})

    attach_forecasts(results, cfg)

    # Parse anomalies, which used to fail silently into PENDING or a skip.
    warnings = [(r["rec"], w) for r in results for w in (r["rec"].get("parse_warnings") or [])]
    warnings += [({"name": "수요예측결과", "no": None}, w) for w in demand_warn]
    for rec, w in warnings:
        sys.stderr.write("parse warning: %s (no=%s): %s\n" % (rec.get("name"), rec.get("no"), w))

    os.makedirs(REPORT_DIR, exist_ok=True)
    md = report.render(results, run_date, parse_warnings=[(rec.get("name"), w) for rec, w in warnings])
    report_path = os.path.join(REPORT_DIR, "%s.md" % run_date)
    with open(report_path, "w") as fh:
        fh.write(md)
    with open(os.path.join(STATE_DIR, "latest.md"), "w") as fh:
        fh.write(md)

    gos = [r for r in results if r["verdict"]["verdict"] == "GO"]
    watches = [r for r in results if r["verdict"]["verdict"] == "WATCH"]

    seen = load_seen()
    fresh = []
    for r in gos:
        no = r["rec"]["no"]
        if args.force or seen.get(no, {}).get("verdict") != "GO":
            fresh.append(r)
    for r in watches:
        seen.setdefault(r["rec"]["no"], {}).update(
            {"verdict": "WATCH", "name": r["rec"]["name"], "seen": run_date}
        )

    # Only a new GO is worth interrupting for. net_returns.py measured WATCH at
    # +2.30% excess return over 77 deals excluding one lucky outlier, against
    # GO's +13% (2026-11-12) -- indistinguishable from leaving the
    # cash alone, and that is before the friction of actually moving funds. So
    # WATCH stays visible in the report and never raises a banner.
    #
    # A GO is recorded in seen.json ONLY once its banner actually posted. The
    # entry is the record of a delivered alert, not of a deal we noticed: if
    # delivery fails the deal stays fresh and the next run tries again. Marking
    # it eagerly (as this did until 2026-09-16) turns one dropped banner into
    # permanent silence, because every later run then sees verdict == "GO" and
    # reports `new 0`. That is also why --no-notify must not write a GO entry --
    # the documented dev loop would otherwise suppress the real alert.
    failed = []
    if not args.no_notify:
        for r in fresh:
            no = r["rec"]["no"]
            if notify.notify_go(r["rec"], r["verdict"], report_path,
                                cfg=cfg, fc=r.get("forecast")):
                seen[no] = {"verdict": "GO", "name": r["rec"]["name"], "notified": run_date}
            else:
                failed.append(r)
    save_seen(seen)

    # One banner per new warning. A wrong parse is as costly as a missed GO --
    # it can BE a missed GO -- so it interrupts, but only once per message.
    if warnings and not args.no_notify:
        warned = _load_json(WARNED_PATH)
        new = [(rec, w) for rec, w in warnings if w not in warned.get(rec.get("no") or rec["name"], [])]
        if new:
            first = new[0]
            ok = notify.notify(
                "공모주 레이더 — 파싱 경고 %d건" % len(new),
                "%s" % first[0].get("name"),
                first[1] + (" 외 %d건" % (len(new) - 1) if len(new) > 1 else ""),
                sound="Basso", open_target=report_path, group="ipo-radar-parse")
            if ok:
                for rec, w in new:
                    warned.setdefault(rec.get("no") or rec["name"], []).append(w)
                _save_json(WARNED_PATH, warned)

    # Calendar events for every GO, not just the newly-alerted ones: a deal is
    # alerted once, but its 매도 event cannot be created until 상장일 publishes,
    # which often lands days after the GO. Iterating all GOs lets a later run
    # fill that in. sync_deal() is idempotent -- it creates only empty slots.
    cal_made = []
    if not args.no_notify and cfg.get("calendar_sync"):
        cal_state = load_calendar_state()
        for r in gos:
            no = r["rec"]["no"]
            entry = cal_state.setdefault(no, {"cheongyak": None, "maedo": None})
            made = calendar_sync.sync_deal(r["rec"], r["verdict"], r.get("forecast"),
                                           cfg, entry)
            for slot in made:
                cal_made.append((r["rec"]["name"], slot))
            if calendar_sync.LAST_ERROR:
                sys.stderr.write("calendar: %s\n" % calendar_sync.LAST_ERROR)
        save_calendar_state(cal_state)

    print("%s — scored %d, GO %d (new %d), WATCH %d" % (run_date, len(results), len(gos), len(fresh), len(watches)))
    for name, slot in cal_made:
        print("  calendar: %s %s event created" % (name, slot))
    for r in failed:
        # stderr lands in radar.err.log, so a silent drop leaves a trace.
        sys.stderr.write(
            "notification FAILED for %s (no=%s) — stays unnotified, will retry next run\n"
            % (r["rec"]["name"], r["rec"]["no"])
        )
    print("report: %s" % report_path)
    for r in gos + watches:
        print("  %-6s %-20s %s" % (r["verdict"]["verdict"], r["rec"]["name"], r["rec"].get("subscription_start")))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        notify.notify("공모주 레이더", "실행 실패", "run.py 오류 — 로그 확인", sound="Basso")
        sys.exit(1)
