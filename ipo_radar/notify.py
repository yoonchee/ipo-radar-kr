"""macOS notifications.

Two delivery paths, because the built-in one cannot do what you'd expect:

``osascript -e 'display notification'`` posts a banner attributed to Script
Editor (the app that owns the AppleScript runtime), and such banners carry NO
click action. Clicking one merely activates Script Editor, which — with no open
document — shows its document browser pointing at
``~/Library/Mobile Documents/com~apple~ScriptEditor2``. That is why a click used
to land in an iCloud folder. It is a limitation of the API, not a bug we can fix
from the AppleScript side, so the osascript path never advertises a path or
implies the banner is actionable.

If ``terminal-notifier`` is installed it is used instead, which does support a
click action, so the banner can open the report or the deal page. It is entirely
optional -- the screen degrades to plain banners without it and the scheduled job
never depends on it:

    brew install terminal-notifier
"""

import email.message
import os
import shlex
import shutil
import smtplib
import ssl
import subprocess
from typing import Dict, Optional


def _find_terminal_notifier():
    """Locate terminal-notifier without relying on PATH.

    launchd starts jobs with a minimal PATH (/usr/bin:/bin:/usr/sbin:/sbin) that
    excludes both Homebrew prefixes, so a bare shutil.which() finds it in an
    interactive shell and silently misses it in the scheduled run -- which is
    exactly the run that needs to work.
    """
    found = shutil.which("terminal-notifier")
    if found:
        return found
    for candidate in ("/opt/homebrew/bin/terminal-notifier",   # Apple silicon
                      "/usr/local/bin/terminal-notifier"):      # Intel
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


# Resolved once; None when terminal-notifier isn't installed.
_TN = _find_terminal_notifier()


def _escape_applescript(s: str) -> str:
    """Escape for an AppleScript double-quoted string literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def _notify_osascript(title, subtitle, message, sound):
    script = 'display notification "%s" with title "%s" subtitle "%s"' % (
        _escape_applescript(message),
        _escape_applescript(title),
        _escape_applescript(subtitle),
    )
    if sound:
        script += ' sound name "%s"' % _escape_applescript(sound)
    try:
        subprocess.run(["osascript", "-e", script], check=True, capture_output=True, timeout=15)
        return True
    except (subprocess.SubprocessError, OSError):
        return False


def _notify_terminal_notifier(title, subtitle, message, sound, open_target, group):
    # The group id must be UNIQUE per deal: terminal-notifier REPLACES any
    # existing notification sharing a group, so a single "ipo-radar" group would
    # silently collapse a three-GO morning down to one surviving banner.
    cmd = [_TN, "-title", title, "-subtitle", subtitle, "-message", message,
           "-group", group or "ipo-radar"]
    if sound:
        cmd += ["-sound", sound]
    if open_target:
        # -execute keeps this working for both local paths and http(s) URLs;
        # shlex.quote guards a path containing spaces.
        cmd += ["-execute", "open %s" % shlex.quote(open_target)]
    try:
        subprocess.run(cmd, check=True, capture_output=True, timeout=15)
        return True
    except (subprocess.SubprocessError, OSError):
        return False


def notify(title, subtitle, message, sound="Glass", open_target=None, group=None):
    # type: (str, str, str, Optional[str], Optional[str], Optional[str]) -> bool
    """Post a banner. ``open_target`` and ``group`` need terminal-notifier.

    When terminal-notifier is installed but fails, fall back to osascript rather
    than giving up. The fallback banner loses its click action, which is a much
    smaller loss than losing the alert outright -- a GO is worth interrupting
    for even when the banner cannot be clicked.
    """
    if _TN and _notify_terminal_notifier(title, subtitle, message, sound, open_target, group):
        return True
    return _notify_osascript(title, subtitle, message, sound)


def click_hint():
    """Whether banners are actually clickable on this machine."""
    return bool(_TN)


# --- email -----------------------------------------------------------------
#
# Banners are easy to miss: they vanish after a few seconds and a GO window can
# close the same day. Email reaches the phone. smtplib/ssl/email are all stdlib,
# so this holds the no-dependency rule.

EMAIL_DEFAULTS = {
    "notify_email": False,          # off until email_to + a keychain password exist
    "notify_banner": True,          # stays on alongside email; see notify_go()
    "smtp_host": "smtp.gmail.com",
    "smtp_port": 465,
    "keychain_service": "ipo-radar-smtp",
}


def _keychain_password(service, account):
    """Read the SMTP password from the login keychain.

    Keeping the app password here rather than in config.local.json or the
    launchd plist means it never lands on disk in plaintext at all.
    ``security`` ships with macOS, so this adds no dependency. A LaunchAgent
    runs as the logged-in user, so the login keychain is already unlocked.
    """
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


# Why the last send_email() failed, for --test-email to report. The failure mode
# of an email alert is silence, so "FAILED" on its own is not good enough: an
# empty keychain item and a password Gmail rejects need different fixes.
LAST_ERROR = None


def send_email(subject, body, cfg):
    # type: (str, str, Dict) -> bool
    """Send one plain-text alert. False on any failure, never raises."""
    global LAST_ERROR
    LAST_ERROR = None
    to = cfg.get("email_to")
    sender = cfg.get("email_from") or to
    if not (to and sender):
        LAST_ERROR = ("email_to/email_from not set -- they live in the gitignored "
                      "config.local.json, which a fresh checkout will not have")
        return False
    svc = cfg.get("keychain_service", "ipo-radar-smtp")
    pw = _keychain_password(svc, sender)
    if pw is None:
        LAST_ERROR = ("no keychain item for service '%s' account '%s' -- or it holds "
                      "an empty password (a -w prompt that could not read input "
                      "stores one silently)" % (svc, sender))
        return False
    msg = email.message.EmailMessage()
    msg["Subject"] = subject
    msg["From"] = sender
    msg["To"] = to
    msg.set_content(body)
    try:
        ctx = ssl.create_default_context()
        with smtplib.SMTP_SSL(cfg.get("smtp_host", "smtp.gmail.com"),
                              int(cfg.get("smtp_port", 465)),
                              timeout=20, context=ctx) as s:
            s.login(sender, pw)
            s.send_message(msg)
        return True
    except smtplib.SMTPAuthenticationError as e:
        LAST_ERROR = ("SMTP rejected the credentials (%s) -- Gmail needs an app "
                      "password, not the account password" % (e.smtp_code,))
        return False
    except (smtplib.SMTPException, ssl.SSLError, OSError) as e:
        LAST_ERROR = "%s: %s" % (type(e).__name__, e)
        return False


def _won(n):
    return "{:+,.0f}원".format(n)


def _go_email_body(rec, verdict, fc, url):
    """The numbers you'd actually want before deciding, in one screen."""
    L = []
    L.append("판정: GO (score %s)" % (verdict or {}).get("score", "?"))
    L.append("")
    L.append("종목        %s" % (rec.get("name") or "?"))
    L.append("확정공모가  %s원 (밴드 %s~%s)" % (
        "{:,}".format(rec["final_price"]) if rec.get("final_price") else "?",
        "{:,}".format(rec["band_low"]) if rec.get("band_low") else "?",
        "{:,}".format(rec["band_high"]) if rec.get("band_high") else "?"))
    L.append("기관경쟁률  %s:1" % (
        ("%.2f" % rec["institutional_ratio"]) if rec.get("institutional_ratio") else "?"))
    L.append("의무보유확약  %s%%" % (
        ("%.2f" % rec["lockup_pct"]) if rec.get("lockup_pct") is not None else "?"))
    L.append("청약        %s ~ %s" % (rec.get("subscription_start") or "?",
                                      rec.get("subscription_end") or "?"))
    L.append("환불/상장   %s / %s" % (rec.get("refund_date") or "?",
                                       rec.get("listing_date") or "?"))
    for u in rec.get("underwriters", []):
        L.append("증권사      %s%s (한도 %s~%s주)" % (
            u.get("name") or "?",
            (" [%s]" % u["role"]) if u.get("role") else "",
            "{:,}".format(u["limit_low"]) if u.get("limit_low") else "?",
            "{:,}".format(u["limit_high"]) if u.get("limit_high") else "?"))
    if fc:
        L.append("")
        L.append("기대손익 (실질)")
        L.append("  투입증거금  %s원" % "{:,.0f}".format(fc.get("capital") or 0))
        L.append("  기대손익  %s" % _won(fc["ev"]))
        if fc.get("ev_lo") is not None:
            L.append("  90%% 신뢰구간  %s ~ %s%s" % (
                _won(fc["ev_lo"]), _won(fc["ev_hi"]),
                "   [0을 포함 — 평균을 양(+)으로 단정할 수 없음]"
                if fc["ev_lo"] < 0 else ""))
        L.append("  흑자확률  %.0f%%" % fc.get("win_prob", 0))
        if not fc.get("calibrated", True):
            L.append("  ⚠ 기관경쟁률이 모형 검증 범위 밖 — EV 신뢰도 낮음")
    if verdict and verdict.get("reasons"):
        L.append("")
        L.append("판정 근거")
        for r in verdict["reasons"]:
            L.append("  - %s" % r)
    L.append("")
    L.append(url or "")
    L.append("")
    L.append("투자 판단과 책임은 본인에게 있습니다.")
    return "\n".join(L)


def _notify_go_banner(rec, report_path, url):
    brokers = ", ".join(u["name"] for u in rec.get("underwriters", [])) or "미확인"
    ratio = rec.get("institutional_ratio")
    window = "%s~%s" % (
        (rec.get("subscription_start") or "?")[5:],
        (rec.get("subscription_end") or "?")[5:],
    )
    return notify(
        title="🔔 공모주 GO — %s" % (rec.get("name") or "?"),
        subtitle="기관 %s:1 · 확약 %s%% · 청약 %s" % (
            ("%.0f" % ratio) if ratio else "?",
            ("%.1f" % rec["lockup_pct"]) if rec.get("lockup_pct") is not None else "?",
            window,
        ),
        message="증권사: %s" % brokers,
        open_target=url or report_path,
        group="ipo-radar-%s" % (rec.get("no") or rec.get("name") or "?"),
    )


def notify_go(rec, verdict=None, report_path=None, cfg=None, fc=None):
    """Alert for a single GO-rated IPO across every configured channel.

    Returns True if AT LEAST ONE channel delivered -- run.py records the deal as
    notified on that basis, so a partial success still counts as reaching you.

    The banner is NOT conditional on email failing. With notify_banner on (the
    default) it is raised on every GO, email delivered or not: the two channels
    fail in unrelated ways -- SMTP can be down, a filter can bury the mail, the
    phone can be face-down -- and a duplicate alert costs nothing next to a
    missed 청약 window that closes the same day.

    It is additionally forced when notify_banner is off but email was meant to
    carry the alert and could not, so turning the banner off can never be the
    thing that produces silence.
    """
    conf = dict(EMAIL_DEFAULTS)
    if cfg:
        conf.update(cfg)
    url = None
    if rec.get("no"):
        url = "https://www.38.co.kr/html/fund/?o=v&no=%s" % rec["no"]

    emailed = False
    if conf.get("notify_email"):
        emailed = send_email(
            "[공모주 GO] %s — 청약 %s~%s" % (
                rec.get("name") or "?",
                (rec.get("subscription_start") or "?")[5:],
                (rec.get("subscription_end") or "?")[5:]),
            _go_email_body(rec, verdict, fc, url),
            conf,
        )

    # Always when enabled (regardless of `emailed`), plus forced as a last
    # resort when email was the intended channel and did not deliver.
    banner = False
    if conf.get("notify_banner") or (conf.get("notify_email") and not emailed):
        banner = _notify_go_banner(rec, report_path, url)

    return bool(emailed or banner)
