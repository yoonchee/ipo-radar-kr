"""macOS notifications via osascript.

``display notification`` truncates aggressively and cannot be clicked through to
arbitrary content, so the banner carries only the decision and the detail lives
in the markdown report.
"""

import subprocess
from typing import Optional


def _escape(s: str) -> str:
    """Escape for an AppleScript double-quoted string literal."""
    return s.replace("\\", "\\\\").replace('"', '\\"')


def notify(title: str, subtitle: str, message: str, sound: Optional[str] = "Glass") -> bool:
    script = 'display notification "%s" with title "%s" subtitle "%s"' % (
        _escape(message),
        _escape(title),
        _escape(subtitle),
    )
    if sound:
        script += ' sound name "%s"' % _escape(sound)
    try:
        subprocess.run(
            ["osascript", "-e", script],
            check=True,
            capture_output=True,
            timeout=15,
        )
        return True
    except (subprocess.SubprocessError, OSError):
        return False


def notify_go(rec: dict, verdict: dict) -> bool:
    """Alert for a single GO-rated IPO."""
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
    )


def notify_summary(n_go: int, n_watch: int, report_path: str) -> bool:
    return notify(
        title="공모주 레이더",
        subtitle="GO %d · WATCH %d" % (n_go, n_watch),
        message="리포트: %s" % report_path,
        sound=None if n_go == 0 else "Glass",
    )
