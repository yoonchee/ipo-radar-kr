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

import os
import shlex
import shutil
import subprocess
from typing import Optional


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


def _notify_terminal_notifier(title, subtitle, message, sound, open_target):
    cmd = [_TN, "-title", title, "-subtitle", subtitle, "-message", message,
           "-group", "ipo-radar"]
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


def notify(title, subtitle, message, sound="Glass", open_target=None):
    # type: (str, str, str, Optional[str], Optional[str]) -> bool
    """Post a banner. ``open_target`` is honoured only via terminal-notifier."""
    if _TN:
        return _notify_terminal_notifier(title, subtitle, message, sound, open_target)
    return _notify_osascript(title, subtitle, message, sound)


def click_hint():
    """Whether banners are actually clickable on this machine."""
    return bool(_TN)


def notify_go(rec, verdict=None, report_path=None):
    """Alert for a single GO-rated IPO.

    Clicking opens that deal's 38.co.kr page -- the thing you'd actually look at
    before subscribing -- when terminal-notifier is installed.
    """
    brokers = ", ".join(u["name"] for u in rec.get("underwriters", [])) or "미확인"
    ratio = rec.get("institutional_ratio")
    window = "%s~%s" % (
        (rec.get("subscription_start") or "?")[5:],
        (rec.get("subscription_end") or "?")[5:],
    )
    target = None
    if rec.get("no"):
        target = "https://www.38.co.kr/html/fund/?o=v&no=%s" % rec["no"]
    elif report_path:
        target = report_path
    return notify(
        title="🔔 공모주 GO — %s" % (rec.get("name") or "?"),
        subtitle="기관 %s:1 · 확약 %s%% · 청약 %s" % (
            ("%.0f" % ratio) if ratio else "?",
            ("%.1f" % rec["lockup_pct"]) if rec.get("lockup_pct") is not None else "?",
            window,
        ),
        message="증권사: %s" % brokers,
        open_target=target,
    )

