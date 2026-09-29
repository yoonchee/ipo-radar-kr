"""Google Calendar events for GO-rated deals.

Two events per deal, mirroring the two decisions the screen actually informs:

- **청약** on the LAST day of the subscription window, 10:00, alerting at 10:00
  sharp — some brokerages will not accept a 청약 before then, so an earlier
  nudge is one you cannot act on.
- **매도** on 상장일 at 09:00, alerting 30 minutes ahead at 08:30. That is when
  장전 동시호가 opens, and the 시초가 is fixed at 09:00 from the orders sitting
  in it. The strategy `backtest.py` measures is 시초/공모 — sell into that
  auction — so an alert arriving at 09:00 would already be late.

Talks to Google over urllib. No dependency, per the stdlib-only rule; the
OAuth refresh grant is one form POST and event creation is one JSON POST.

This stays inside the decision-support boundary: it writes reminders to a
calendar. It never touches a brokerage, and must not grow to.

Credentials (client id/secret and the refresh token) live in the login keychain
as one JSON blob, never in either config file. Run `./run.py --gcal-setup` once
to put them there.
"""

import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, List, Optional

TOKEN_URL = "https://oauth2.googleapis.com/token"
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
API_BASE = "https://www.googleapis.com/calendar/v3"
# Only the events scope -- this never needs to read or delete a calendar.
SCOPE = "https://www.googleapis.com/auth/calendar.events"

CALENDAR_DEFAULTS = {
    "calendar_sync": False,          # off until --gcal-setup has run
    "calendar_id": "primary",
    "gcal_keychain_service": "ipo-radar-gcal",
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

def _keychain_read(service):
    try:
        out = subprocess.run(["security", "find-generic-password", "-s", service, "-w"],
                             capture_output=True, timeout=15)
        if out.returncode != 0:
            return None
        raw = out.stdout.decode("utf-8").strip()
        return json.loads(raw) if raw else None
    except (subprocess.SubprocessError, OSError, ValueError):
        return None


def _keychain_write(service, account, blob):
    try:
        out = subprocess.run(
            ["security", "add-generic-password", "-U", "-s", service,
             "-a", account, "-w", json.dumps(blob)],
            capture_output=True, timeout=15)
        return out.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


# --- HTTP ------------------------------------------------------------------

def _post(url, data, headers, timeout=20):
    """POST and decode JSON. Returns (body_dict, error_string)."""
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")), None
    except urllib.error.HTTPError as e:
        try:
            detail = json.loads(e.read().decode("utf-8"))
            msg = detail.get("error_description") or \
                (detail.get("error") or {}).get("message") or str(detail)
        except Exception:
            msg = e.reason
        return None, "HTTP %s: %s" % (e.code, msg)
    except (urllib.error.URLError, OSError, ValueError) as e:
        return None, "%s: %s" % (type(e).__name__, e)


def _access_token(cfg):
    """Exchange the stored refresh token for a short-lived access token."""
    global LAST_ERROR
    svc = cfg.get("gcal_keychain_service", "ipo-radar-gcal")
    creds = _keychain_read(svc)
    if not creds or not creds.get("refresh_token"):
        LAST_ERROR = ("no Google credentials in keychain service '%s' -- "
                      "run ./run.py --gcal-setup once" % svc)
        return None
    body = urllib.parse.urlencode({
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "refresh_token": creds["refresh_token"],
        "grant_type": "refresh_token",
    }).encode("utf-8")
    out, err = _post(TOKEN_URL, body,
                     {"Content-Type": "application/x-www-form-urlencoded"})
    if err:
        LAST_ERROR = ("refresh token rejected (%s) -- revoked or expired, "
                      "re-run ./run.py --gcal-setup" % err)
        return None
    return out.get("access_token")


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
    L.append("%s투입증거금    %s만원" % (indent, "{:,.0f}".format((fc.get("capital") or 0) / 10000.0)))
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
        # 상장일 시초가는 공모가의 60~400% 범위에서 결정됩니다 (2023-06 제도 변경).
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
    }


def _insert(ev, cfg, token):
    """POST one event. Returns its id, or None with LAST_ERROR set."""
    global LAST_ERROR
    tz = cfg.get("calendar_timezone", "Asia/Seoul")
    dur = int(cfg.get("event_duration_min", 30))
    start_h, mins = ev["_hour"], ev["_hour"] * 60 + dur
    body = {
        "summary": ev["summary"],
        "location": ev["location"],
        "description": ev["description"],
        "start": {"dateTime": "%sT%02d:00:00" % (ev["_date"], start_h), "timeZone": tz},
        "end": {"dateTime": "%sT%02d:%02d:00" % (ev["_date"], mins // 60, mins % 60),
                "timeZone": tz},
        "reminders": {"useDefault": False,
                      "overrides": [{"method": "popup", "minutes": ev["_reminder"]}]},
    }
    url = "%s/calendars/%s/events" % (
        API_BASE, urllib.parse.quote(cfg.get("calendar_id", "primary"), safe=""))
    out, err = _post(url, json.dumps(body).encode("utf-8"),
                     {"Authorization": "Bearer " + token,
                      "Content-Type": "application/json; charset=UTF-8"})
    if err:
        LAST_ERROR = "%s: %s" % (ev["summary"], err)
        return None
    return out.get("id")


def sync_deal(rec, verdict, fc, cfg, state):
    """Create whichever of the two events this deal is still missing.

    `state` is the per-deal record from state/calendar.json, mutated in place:
    {"cheongyak": <event id or None>, "maedo": <event id or None>}. An event is
    created only when its slot is empty AND its date is known, so:

    - a run that already created 청약 never creates it twice, and
    - a GO whose 상장일 has not published yet (common -- it appears with the
      확정공모가, sometimes days later) gets 청약 now and 매도 on a later run.

    Returns the list of slot names created, so the caller can report them.
    """
    global LAST_ERROR
    LAST_ERROR = None
    wanted = []
    if not state.get("cheongyak") and rec.get("subscription_end"):
        wanted.append(("cheongyak", cheongyak_event(rec, verdict, fc, cfg)))
    if not state.get("maedo") and rec.get("listing_date"):
        wanted.append(("maedo", maedo_event(rec, verdict, fc, cfg)))
    if not wanted:
        return []

    token = _access_token(cfg)
    if not token:
        return []

    created = []
    for slot, ev in wanted:
        eid = _insert(ev, cfg, token)
        if eid:
            state[slot] = eid
            created.append(slot)
    return created


# --- one-time authorisation ------------------------------------------------

def authorize(client_id, client_secret, cfg, port=8765):
    """Loopback OAuth flow; stores the refresh token in the keychain.

    Google retired the out-of-band redirect, so this runs a one-request local
    server to catch the code. Everything here is stdlib (http.server, webbrowser).
    """
    import http.server
    import webbrowser

    redirect = "http://localhost:%d/" % port
    params = urllib.parse.urlencode({
        "client_id": client_id, "redirect_uri": redirect, "response_type": "code",
        "scope": SCOPE, "access_type": "offline", "prompt": "consent",
    })
    url = AUTH_URL + "?" + params
    holder = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            holder["code"] = (q.get("code") or [None])[0]
            holder["error"] = (q.get("error") or [None])[0]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            msg = "인증 완료 — 터미널로 돌아가세요." if holder.get("code") else "인증 실패"
            self.wfile.write(("<html><body><h2>%s</h2></body></html>" % msg).encode("utf-8"))

        def log_message(self, *a):
            pass

    print("브라우저에서 Google 계정 인증을 진행하세요:\n  %s\n" % url)
    try:
        webbrowser.open(url)
    except Exception:
        pass
    srv = http.server.HTTPServer(("localhost", port), Handler)
    srv.timeout = 300
    srv.handle_request()
    srv.server_close()

    if not holder.get("code"):
        return False, "authorisation was not completed (%s)" % (holder.get("error") or "no code")

    body = urllib.parse.urlencode({
        "client_id": client_id, "client_secret": client_secret,
        "code": holder["code"], "grant_type": "authorization_code",
        "redirect_uri": redirect,
    }).encode("utf-8")
    out, err = _post(TOKEN_URL, body,
                     {"Content-Type": "application/x-www-form-urlencoded"})
    if err:
        return False, "code exchange failed -- %s" % err
    if not out.get("refresh_token"):
        return False, ("Google returned no refresh token. Revoke the app at "
                       "myaccount.google.com/permissions and run this again.")

    svc = cfg.get("gcal_keychain_service", "ipo-radar-gcal")
    blob = {"client_id": client_id, "client_secret": client_secret,
            "refresh_token": out["refresh_token"]}
    if not _keychain_write(svc, cfg.get("email_from") or "ipo-radar", blob):
        return False, "could not write to keychain service '%s'" % svc
    return True, svc
