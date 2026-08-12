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
"""

import argparse
import datetime
import json
import os
import sys
import traceback

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from ipo_radar import fetch, notify, parse, report, score

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_DIR = os.path.join(ROOT, "state")
REPORT_DIR = os.path.join(STATE_DIR, "reports")
SEEN_PATH = os.path.join(STATE_DIR, "seen.json")
CONFIG_PATH = os.path.join(ROOT, "config.json")


def load_config():
    cfg = dict(score.DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r") as fh:
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="ignore the lead-time window")
    ap.add_argument("--no-notify", action="store_true", help="write report only")
    ap.add_argument("--force", action="store_true", help="re-notify already-alerted deals")
    args = ap.parse_args()

    cfg = load_config()
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
        seen[no] = {"verdict": "GO", "name": r["rec"]["name"], "notified": run_date}
    for r in watches:
        seen.setdefault(r["rec"]["no"], {}).update(
            {"verdict": "WATCH", "name": r["rec"]["name"], "seen": run_date}
        )

    if not args.no_notify:
        for r in fresh:
            notify.notify_go(r["rec"], r["verdict"], report_path)
        if not fresh and (gos or watches):
            notify.notify_summary(len(gos), len(watches), report_path)
    save_seen(seen)

    print("%s — scored %d, GO %d (new %d), WATCH %d" % (run_date, len(results), len(gos), len(fresh), len(watches)))
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
