"""Markdown report rendering."""

from typing import Dict, List, Optional

from .fetch import detail_url

_ORDER = {"GO": 0, "WATCH": 1, "PENDING": 2, "PASS": 3, "EXCLUDED": 4}
_BADGE = {"GO": "🟢 GO", "WATCH": "🟡 WATCH", "PENDING": "⚪ PENDING", "PASS": "🔴 PASS", "EXCLUDED": "⚫ EXCLUDED"}


def _won(n: Optional[int]) -> str:
    return "{:,}".format(n) if n else "-"


def _headline_reason(reasons: List[str]) -> str:
    """For a rejected deal, the blocker is the interesting line, not the first."""
    blocked = [r for r in reasons if r.startswith("⚠")]
    if blocked:
        return blocked[0].lstrip("⚠ ")
    return reasons[0] if reasons else "-"


def _brokers(rec: Dict) -> str:
    us = rec.get("underwriters") or []
    if not us:
        return "-"
    parts = []
    for u in us:
        limit = ""
        if u.get("limit_low"):
            limit = (
                " (한도 {:,}~{:,}주)".format(u["limit_low"], u["limit_high"])
                if u.get("limit_high") and u["limit_high"] != u["limit_low"]
                else " (한도 {:,}주)".format(u["limit_low"])
            )
        role = " [%s]" % u["role"] if u.get("role") else ""
        parts.append("%s%s%s" % (u["name"], role, limit))
    return "<br>".join(parts)


def render(results: List[Dict], run_date: str) -> str:
    results = sorted(
        results,
        key=lambda r: (_ORDER.get(r["verdict"]["verdict"], 9), r["rec"].get("subscription_start") or ""),
    )
    go = [r for r in results if r["verdict"]["verdict"] == "GO"]
    watch = [r for r in results if r["verdict"]["verdict"] == "WATCH"]

    L = []
    L.append("# 공모주 레이더 — %s" % run_date)
    L.append("")
    L.append("**GO %d · WATCH %d · 검토 %d건**" % (len(go), len(watch), len(results)))
    L.append("")
    L.append("> 판단 근거는 공개된 수요예측 결과이며, 투자 판단과 책임은 본인에게 있습니다.")
    L.append("")

    if go or watch:
        L.append("## 청약 대상")
        L.append("")
        L.append("| 판정 | 종목 | 청약일 | 확정공모가 (밴드) | 기관경쟁률 | 의무보유확약 | 상장일 | 증권사 |")
        L.append("|---|---|---|---|---:|---:|---|---|")
        for r in go + watch:
            rec, v = r["rec"], r["verdict"]
            band = "%s~%s" % (_won(rec.get("band_low")), _won(rec.get("band_high")))
            L.append(
                "| %s | [%s](%s) | %s~%s | %s <br><sub>%s</sub> | %s | %s | %s | %s |"
                % (
                    _BADGE.get(v["verdict"], v["verdict"]),
                    rec.get("name") or "?",
                    detail_url(rec.get("no") or ""),
                    rec.get("subscription_start") or "?",
                    (rec.get("subscription_end") or "?")[5:],
                    _won(rec.get("final_price")),
                    band,
                    ("%.2f:1" % rec["institutional_ratio"]) if rec.get("institutional_ratio") else "-",
                    ("%.2f%%" % rec["lockup_pct"]) if rec.get("lockup_pct") is not None else "-",
                    rec.get("listing_date") or "-",
                    _brokers(rec),
                )
            )
        L.append("")

        L.append("### 판정 근거")
        L.append("")
        for r in go + watch:
            rec, v = r["rec"], r["verdict"]
            L.append("**%s — %s (score %d)**" % (rec.get("name"), _BADGE.get(v["verdict"]), v["score"]))
            L.append("")
            for reason in v["reasons"]:
                L.append("- %s" % reason)
            L.append("")

    others = [r for r in results if r["verdict"]["verdict"] not in ("GO", "WATCH")]
    if others:
        L.append("## 기타")
        L.append("")
        L.append("| 판정 | 종목 | 청약일 | 기관경쟁률 | 사유 |")
        L.append("|---|---|---|---:|---|")
        for r in others:
            rec, v = r["rec"], r["verdict"]
            L.append(
                "| %s | %s | %s | %s | %s |"
                % (
                    _BADGE.get(v["verdict"], v["verdict"]),
                    rec.get("name") or "?",
                    rec.get("subscription_start") or "?",
                    ("%.2f:1" % rec["institutional_ratio"]) if rec.get("institutional_ratio") else "-",
                    _headline_reason(v["reasons"]),
                )
            )
        L.append("")

    L.append("---")
    L.append("")
    L.append("출처: [38커뮤니케이션](https://www.38.co.kr/html/fund/)")
    return "\n".join(L)
