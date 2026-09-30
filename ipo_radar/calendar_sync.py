"""Google Calendar events for GO-rated deals, over CalDAV.

Two events per deal, mirroring the two decisions the screen actually informs:

- **청약** on the LAST day of the subscription window, 10:00, alerting at 10:00
  sharp — some brokerages will not accept a 청약 before then, so an earlier
  nudge is one you cannot act on.
- **매도** on 상장일 at 09:00, alerting 30 minutes ahead at 08:30. That is when
  장전 동시호가 opens, and the 시초가 is fixed at 09:00 from the orders sitting
  in it. The strategy `backtest.py` measures is 시초/공모 — sell into that
  auction — so an alert arriving at 09:00 would already be late.

Why CalDAV and not the Calendar REST API: the REST API needs an OAuth client,
which means a Google Cloud project, a consent screen, and publishing an app with
a sensitive scope — verification paperwork for a one-user script. Google's CalDAV
endpoint accepts the **same Gmail app password already used for SMTP**, so this
adds no credential, no project, and no consent screen. One PUT per event over
urllib, so the stdlib-only rule holds.

Authenticating as the account owner also fixes a subtler problem: calendar
reminders belong to whoever set them. Events written as the user carry VALARMs
that fire for the user — the entire point, and not true of a service account
writing into a shared calendar.

Event UIDs are deterministic (`ipo-radar-<no>-<slot>`), so a PUT for a deal that
already has an event *replaces* it rather than adding a second one. Duplication
is therefore impossible even if state/calendar.json is lost — which is exactly
what a fresh checkout does.

This stays inside the decision-support boundary: it writes reminders to a
calendar. It never touches a brokerage, and must not grow to.
"""

import base64
import datetime
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

# apps.google.com answers 405 to PROPFIND; www.google.com is the host that
# speaks CalDAV. Verified empirically -- do not "modernise" this hostname.
DAV_BASE = "https://www.google.com/calendar/dav"

CALENDAR_DEFAULTS = {
    "calendar_sync": False,          # off until --test-calendar passes
    "calendar_id": "primary",        # "primary" -> the account's own calendar
    "calendar_timezone": "Asia/Seoul",
    "cheongyak_hour": 10,            # 청약 event start
    "cheongyak_reminder_min": 0,     # alert at the event, not before
    "maedo_hour": 9,                 # 매도 event start (상장일)
    "maedo_reminder_min": 30,        # alert at 08:30, when 동시호가 opens
    "event_duration_min": 30,
}

# Why the last call failed, for run.py to report. Same reasoning as notify.py:
# a channel whose failure mode is a missing reminder has to say why.
LAST_ERROR = None


# --- credentials -----------------------------------------------------------

def _keychain_password(service, account):
    """The Gmail app password -- the same keychain item SMTP reads."""
    cmd = ["security", "find-generic-password", "-s", service]
    if account:
        cmd += ["-a", account]
    cmd += ["-w"]
    try:
        out = subprocess.run(cmd, capture_output=True, timeout=15)
        if out.returncode != 0:
            return None
        return out.stdout.decode("utf-8").strip() or None
    except (subprocess.SubprocessError, OSError):
        return None


def _auth(cfg):
    """Returns (user, basic_auth_header), or (None, None) with LAST_ERROR set."""
    global LAST_ERROR
    user = cfg.get("email_from") or cfg.get("email_to")
    if not user:
        LAST_ERROR = ("email_from not set -- CalDAV signs in as the Google account, "
                      "so it needs the address from config.local.json")
        return None, None
    pw = _keychain_password(cfg.get("keychain_service", "ipo-radar-smtp"), user)
    if not pw:
        LAST_ERROR = ("no app password in keychain for '%s' -- the same item SMTP "
                      "uses (service '%s')" % (user, cfg.get("keychain_service")))
        return None, None
    token = base64.b64encode(("%s:%s" % (user, pw)).encode("utf-8")).decode("ascii")
    return user, "Basic " + token


def _collection(cfg, user):
    cal = cfg.get("calendar_id") or "primary"
    if cal == "primary":
        cal = user
    return "%s/%s/events/" % (DAV_BASE, urllib.parse.quote(cal, safe="@."))


# --- iCalendar -------------------------------------------------------------

def _esc(text):
    """Escape a value for an iCalendar text property (RFC 5545 3.3.11)."""
    return (text.replace("\\", "\\\\").replace(";", "\;")
                .replace(",", "\\,").replace("\r\n", "\\n").replace("\n", "\\n"))


def _fold(line):
    """Fold to 75 octets per RFC 5545, without splitting a UTF-8 character."""
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    out, start, limit = [], 0, 75
    while start < len(raw):
        end = min(start + limit, len(raw))
        # Back off to a character boundary (continuation bytes are 10xxxxxx).
        while end > start and end < len(raw) and (raw[end] & 0xC0) == 0x80:
            end -= 1
        out.append(raw[start:end].decode("utf-8"))
        start = end
        limit = 74  # continuation lines carry a leading space
    return "\r\n ".join(out)


def _valarms(ev, user):
    """VALARMs for one event, pinned against the calendar's default reminder.

    Google normalises every alarm to a relative popup and then DISCARDS it if
    the resulting set equals the calendar's default -- storing the event as
    "use default" instead. Measured: a lone -PT30M popup against a 30-minute
    default came back as useDefaultReminders=true, and an absolute
    TRIGGER;VALUE=DATE-TIME collapsed the same way. Only -PT29M survived, i.e.
    the test is on the VALUE, not the form.

    So an event whose alarm happens to match the default silently starts
    tracking that default: change the default, and the 매도 alert moves off
    08:30 with it. Emitting a second alarm on a different ACTION makes the set
    {popup, email} -- never equal to a single-popup default -- which pins the
    popup at the exact offset for any default the user might set. The extra
    item is one calendar email at the same moment, which on a morning money
    moves is not unwelcome.
    """
    mins = int(ev["_reminder"])
    L = ["BEGIN:VALARM", "ACTION:DISPLAY", "DESCRIPTION:" + _esc(ev["summary"]),
         "TRIGGER:-PT%dM" % mins, "END:VALARM"]
    if ev.get("_pin") and user:
        L += ["BEGIN:VALARM", "ACTION:EMAIL",
              "DESCRIPTION:" + _esc(ev["description"][:200]),
              "SUMMARY:" + _esc(ev["summary"]),
              "ATTENDEE:mailto:" + user,
              "TRIGGER:-PT%dM" % mins, "END:VALARM"]
    return L


def _vevent(uid, ev, cfg, user=None):
    tz = cfg.get("calendar_timezone", "Asia/Seoul")
    dur = int(cfg.get("event_duration_min", 30))
    day = ev["_date"].replace("-", "")
    start_min = int(ev["_hour"]) * 60
    end_min = start_min + dur
    stamp = datetime.datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//ipo-radar-kr//KR//EN",
        "CALSCALE:GREGORIAN",
        # Korea has no DST, so a fixed-offset VTIMEZONE is complete.
        "BEGIN:VTIMEZONE", "TZID:" + tz, "BEGIN:STANDARD",
        "DTSTART:19700101T000000", "TZOFFSETFROM:+0900", "TZOFFSETTO:+0900",
        "TZNAME:KST", "END:STANDARD", "END:VTIMEZONE",
        "BEGIN:VEVENT",
        "UID:" + uid,
        "DTSTAMP:" + stamp,
        "DTSTART;TZID=%s:%sT%02d%02d00" % (tz, day, start_min // 60, start_min % 60),
        "DTEND;TZID=%s:%sT%02d%02d00" % (tz, day, end_min // 60, end_min % 60),
        "SUMMARY:" + _esc(ev["summary"]),
        "LOCATION:" + _esc(ev["location"]),
        "DESCRIPTION:" + _esc(ev["description"]),
    ] + _valarms(ev, user) + [
        "END:VEVENT", "END:VCALENDAR",
    ]
    return "\r\n".join(_fold(l) for l in lines) + "\r\n"


# --- HTTP ------------------------------------------------------------------

def _request(method, url, auth, data=None, ctype=None, timeout=25):
    headers = {"Authorization": auth}
    if ctype:
        headers["Content-Type"] = ctype
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, None
    except urllib.error.HTTPError as e:
        hint = ""
        if e.code in (401, 403):
            hint = (" -- app password rejected for CalDAV; confirm 2FA is on and "
                    "that the password has not been revoked")
        return e.code, "HTTP %s %s%s" % (e.code, e.reason, hint)
    except (urllib.error.URLError, OSError) as e:
        return None, "%s: %s" % (type(e).__name__, e)


def _put(uid, ev, cfg, user, auth):
    """Create or replace one event. Returns True on success."""
    global LAST_ERROR
    url = _collection(cfg, user) + urllib.parse.quote(uid) + ".ics"
    status, err = _request("PUT", url, auth, _vevent(uid, ev, cfg, user).encode("utf-8"),
                           'text/calendar; charset="utf-8"')
    if status in (200, 201, 204):
        return True
    LAST_ERROR = "%s: %s" % (ev["summary"], err or "unexpected status %s" % status)
    return False


# --- event construction ----------------------------------------------------

def _won(n):
    return "{:+,.0f}원".format(n)


def _brokers(rec):
    names = [u.get("name") for u in rec.get("underwriters", []) if u.get("name")]
    return " / ".join(names) or "미확인"


def _limits_line(rec):
    parts = []
    for u in rec.get("underwriters", []):
        if u.get("limit_low") and u.get("limit_high"):
            parts.append("%s%s %s~%s주" % (
                u.get("name") or "?",
                " [%s]" % u["role"] if u.get("role") else "",
                "{:,}".format(u["limit_low"]), "{:,}".format(u["limit_high"])))
    return " · ".join(parts) or "미확인"


def _forecast_lines(fc, indent="  "):
    if not fc:
        return []
    L = ["", "기대손익 (실질, 최대 청약한도 기준)"]
    # 만원, matching the report -- 15,375만 reads faster than 153,750,000.
    L.append("%s투입증거금    %s만원" % (
        indent, "{:,.0f}".format((fc.get("capital") or 0) / 10000.0)))
    L.append("%s기대손익      %s" % (indent, _won(fc["ev"])))
    if fc.get("ev_lo") is not None:
        L.append("%s90%% 신뢰구간  %s ~ %s" % (indent, _won(fc["ev_lo"]), _won(fc["ev_hi"])))
    L.append("%s흑자확률      %.0f%%" % (indent, fc.get("win_prob", 0)))
    if fc.get("ev_lo") is not None and fc["ev_lo"] < 0:
        L.append("⚠ 신뢰구간이 0을 포함 — 평균을 양(+)으로 단정할 수 없습니다.")
    if not fc.get("calibrated", True):
        L.append("⚠ 기관경쟁률이 모형 검증 범위 밖 — EV 신뢰도 낮음.")
    return L


def _deal_url(rec):
    return "https://www.38.co.kr/html/fund/?o=v&no=%s" % rec["no"] if rec.get("no") else ""


def cheongyak_event(rec, verdict, fc, cfg):
    """청약 reminder on the last day of the subscription window."""
    L = ["🟢 GO (score %s) — 공모주 레이더" % (verdict or {}).get("score", "?"),
         "", "청약 마지막 날입니다.", ""]
    price, hi = rec.get("final_price"), rec.get("band_high")
    band_pos = ""
    if price and hi:
        band_pos = " — 상단" if price >= hi else " — 밴드 내"
    L.append("확정공모가    %s원 (밴드 %s~%s%s)" % (
        "{:,}".format(price) if price else "?",
        "{:,}".format(rec["band_low"]) if rec.get("band_low") else "?",
        "{:,}".format(hi) if hi else "?", band_pos))
    L.append("기관경쟁률    %s:1" % (
        "%.2f" % rec["institutional_ratio"] if rec.get("institutional_ratio") else "?"))
    L.append("의무보유확약  %s%%" % (
        "%.2f" % rec["lockup_pct"] if rec.get("lockup_pct") is not None else "?"))
    L.append("청약          %s ~ %s" % (rec.get("subscription_start") or "?",
                                        rec.get("subscription_end") or "?"))
    L.append("환불 / 상장   %s / %s" % (rec.get("refund_date") or "?",
                                        rec.get("listing_date") or "미정"))
    L.append("청약한도      %s" % _limits_line(rec))
    L += _forecast_lines(fc)
    L += ["", "배정은 비례만 계산한 하한값이며, 기회비용은 연 3% 기준입니다.",
          "투자 판단과 책임은 본인에게 있습니다.", "", _deal_url(rec)]
    return {
        "summary": "공모주 청약 — %s" % (rec.get("name") or "?"),
        "location": _brokers(rec),
        "description": "\n".join(L),
        "_date": rec.get("subscription_end"),
        "_hour": int(cfg.get("cheongyak_hour", 10)),
        "_reminder": int(cfg.get("cheongyak_reminder_min", 0)),
    }


def maedo_event(rec, verdict, fc, cfg):
    """매도 reminder on 상장일, timed to the 장전 동시호가."""
    price = rec.get("final_price")
    L = ["📈 상장일 — 시초가 매도", "",
         "이 스크리너가 측정한 전략은 시초/공모 (공모가에 청약 → 시초가에 매도)입니다.",
         "첫날종가까지 보유하는 것은 다른 전략이며, 임계값이 그에 맞춰 보정되어 있지 않습니다.",
         ""]
    L.append("확정공모가    %s원" % ("{:,}".format(price) if price else "?"))
    if price:
        # 시초가는 공모가의 60~400% 범위에서 결정됩니다 (2023-06 제도 변경).
        L.append("시초가 범위   %s원 ~ %s원 (공모가의 60~400%%)" % (
            "{:,}".format(int(price * 0.6)), "{:,}".format(int(price * 4))))
    if fc and fc.get("shares_med") is not None:
        L.append("예상배정      %d주 (비례만 계산한 하한값)" % int(fc["shares_med"]))
    L.append("청약          %s ~ %s (%s)" % (rec.get("subscription_start") or "?",
                                             rec.get("subscription_end") or "?",
                                             _brokers(rec)))
    L.append("환불          %s" % (rec.get("refund_date") or "?"))
    L += _forecast_lines(fc)
    L += ["", "※ 시초가는 08:30~09:00 장전 동시호가에서 결정됩니다.",
          "   시초가에 매도하려면 09:00 이전에 주문이 들어가 있어야 합니다.",
          "", _deal_url(rec)]
    return {
        "summary": "공모주 매도 — %s" % (rec.get("name") or "?"),
        "location": _brokers(rec),
        "description": "\n".join(L),
        "_date": rec.get("listing_date"),
        "_hour": int(cfg.get("maedo_hour", 9)),
        "_reminder": int(cfg.get("maedo_reminder_min", 30)),
        # Pin it: 30 minutes is a common calendar default, and this alert
        # landing exactly when 장전 동시호가 opens is the whole point.
        "_pin": True,
    }


def event_uid(no, slot):
    """Deterministic, so a repeat PUT replaces rather than duplicates."""
    return "ipo-radar-%s-%s" % (no, slot)


def sync_deal(rec, verdict, fc, cfg, state):
    """Create whichever of the two events this deal is still missing.

    `state` is the per-deal record from state/calendar.json, mutated in place:
    {"cheongyak": <uid or None>, "maedo": <uid or None>}. An event is written
    only when its slot is empty AND its date is known, so:

    - a run that already created 청약 never rewrites it, and
    - a GO whose 상장일 has not published yet (common -- it appears with the
      확정공모가, sometimes days later) gets 청약 now and 매도 on a later run.

    Returns the list of slot names created, so the caller can report them.
    """
    global LAST_ERROR
    LAST_ERROR = None
    # A reminder for a date that has passed is clutter, not information. This
    # comes up whenever a slot is empty for a deal whose window has closed --
    # after a state loss, or when a deal is re-examined late.
    today = datetime.date.today().isoformat()
    wanted = []
    if not state.get("cheongyak") and (rec.get("subscription_end") or "") >= today:
        wanted.append(("cheongyak", cheongyak_event(rec, verdict, fc, cfg)))
    if not state.get("maedo") and (rec.get("listing_date") or "") >= today:
        wanted.append(("maedo", maedo_event(rec, verdict, fc, cfg)))
    if not wanted:
        return []

    user, auth = _auth(cfg)
    if not auth:
        return []

    created = []
    for slot, ev in wanted:
        uid = event_uid(rec.get("no") or ev["summary"], slot)
        if _put(uid, ev, cfg, user, auth):
            state[slot] = uid
            created.append(slot)
    return created


def self_test(cfg):
    """Write a throwaway event and remove it. Returns (ok, detail).

    Proves the whole path -- keychain, CalDAV auth, write access -- without
    waiting for a GO and without leaving anything on the calendar.
    """
    global LAST_ERROR
    LAST_ERROR = None
    user, auth = _auth(cfg)
    if not auth:
        return False, LAST_ERROR
    day = (datetime.date.today() + datetime.timedelta(days=1)).isoformat()
    uid = "ipo-radar-selftest"
    ev = {"summary": "공모주 레이더 — 연결 테스트", "location": "테스트",
          "description": "이 이벤트는 자동으로 삭제됩니다.",
          "_date": day, "_hour": 12, "_reminder": 0}
    if not _put(uid, ev, cfg, user, auth):
        return False, LAST_ERROR
    url = _collection(cfg, user) + urllib.parse.quote(uid) + ".ics"
    status, err = _request("DELETE", url, auth)
    if status not in (200, 204, 404):
        return True, ("wrote a test event but could not remove it (%s) -- delete it "
                      "by hand (%s 12:00)" % (err or status, day))
    return True, "wrote and removed a test event on %s (as %s)" % (day, user)
