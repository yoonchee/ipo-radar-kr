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
"""

import argparse
import datetime
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ipo_radar import fetch, forecast, notify, parse, report, score

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(ROOT, "state")
REPORT_DIR = os.path.join(STATE_DIR, "reports")
SEEN_PATH = os.path.join(STATE_DIR, "seen.json")
CONFIG_PATH = os.path.join(ROOT, "config.json")
# Machine-local overrides, gitignored. Anything personal to one machine or one
# person -- the alert address, say -- belongs here rather than in the tracked
# config, which is public. Applied last, so it wins over config.json.
LOCAL_CONFIG_PATH = os.path.join(ROOT, "config.local.json")


def load_config():
    cfg = dict(score.DEFAULT_CONFIG)
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


def attach_forecasts(results):
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
    for r in results:
        if r["verdict"]["verdict"] not in ("GO", "WATCH"):
            continue
        rec = r["rec"]
        limit = max([u.get("limit_high") or 0 for u in rec.get("underwriters", [])] or [0])
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
            r["forecast"] = p


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="ignore the lead-time window")
    ap.add_argument("--no-notify", action="store_true", help="write report only")
    ap.add_argument("--force", action="store_true", help="re-notify already-alerted deals")
    ap.add_argument("--test-email", action="store_true",
                    help="send one test email and exit, to verify delivery")
    args = ap.parse_args()

    cfg = load_config()

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
    demand = {d["no"]: d for d in parse.parse_demand_results(fetch.fetch_demand_results_page()) if d["no"]}

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
        results.append({"rec": rec, "verdict": score.evaluate(rec, cfg)})

    attach_forecasts(results)

    os.makedirs(REPORT_DIR, exist_ok=True)
    md = report.render(results, run_date)
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
    # +0.03% excess return over 69 deals -- indistinguishable from leaving the
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

    print("%s — scored %d, GO %d (new %d), WATCH %d" % (run_date, len(results), len(gos), len(fresh), len(watches)))
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
