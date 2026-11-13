"""Markdown report rendering."""

from typing import Dict, List, Optional

from .fetch import detail_url

_ORDER = {"GO": 0, "WATCH": 1, "PENDING": 2, "PASS": 3, "EXCLUDED": 4}
_BADGE = {"GO": "🟢 GO", "WATCH": "🟡 WATCH", "PENDING": "⚪ PENDING", "PASS": "🔴 PASS", "EXCLUDED": "⚫ EXCLUDED"}


def _won(n: Optional[int]) -> str:
    return "{:,}".format(n) if n else "-"


def _man(v, sign: bool = False) -> str:
    """Won in 만원 — the scale a person reasons in. For big figures only."""
    if v is None:
        return "-"
    return ("{:+,.0f}만" if sign else "{:,.0f}만").format(v / 10000.0)


def _won_signed(v) -> str:
    """Raw 원 for small figures: rounding an EV of -5,456 to '-1만' destroys it."""
    if v is None:
        return "-"
    return "{:+,.0f}원".format(v)


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


def my_limit_lines(ml: Optional[Dict], label: str = "내 청약한도  ") -> List[str]:
    """Where your 청약한도 came from, for the email and the calendar.

    Not for an estimated limit, which its callers word themselves. A limit
    read from the 투자설명서 says so, and so does one that fell back to 38,
    with why: 38's low end is a different tier from deal to deal.
    """
    ml = ml or {}
    if not ml or ml.get("estimated"):
        return []
    fmt = "{:,}".format
    L = []
    if ml.get("source") == "prospectus":
        pct = round((ml.get("multiplier") or 1.0) * 100)
        tier = "100% 기준" if pct == 100 else "우대 %d%%" % pct
        L.append("%s%s %s주 (%s, 투자설명서)" % (label, ml["broker"], fmt(ml["shares"]), tier))
        if ml.get("ranged"):
            L.append("  ⚠ 투자설명서 청약한도가 아직 범위 %s~%s주 — 하한 기준, 증권사 공고 확인"
                     % (fmt(ml["ranged"][0]), fmt(ml["ranged"][1])))
        l38 = ml.get("l38")
        if l38 and l38[0] and l38[0] != ml.get("base"):
            L.append("  38 표기 %s~%s주 — 투자설명서 100%% 기준은 %s주" % (
                fmt(l38[0]), fmt(l38[1] or l38[0]), fmt(ml["base"])))
    else:
        if ml.get("multiplier"):
            L.append("%s%s %s주 (38 하한 × %g)" % (label, ml["broker"], fmt(ml["shares"]), ml["multiplier"]))
        if ml.get("limits_note"):
            L.append("  투자설명서 미확인 (%s) — 38 기준이라 한도가 다를 수 있음" % ml["limits_note"])
    return L


def _fc_name(r: Dict) -> str:
    n = r["rec"].get("name") or "?"
    return n + " ⚠추정한도" if (r.get("forecast") or {}).get("limit_estimated") else n


def render(results: List[Dict], run_date: str, parse_warnings: Optional[List] = None) -> str:
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

    if parse_warnings:
        # First, above the verdicts: a value we could not read can turn a GO
        # into PENDING, so none of the verdicts below is safe until it is checked.
        L.append("## ⚠ 파싱 경고 %d건" % len(parse_warnings))
        L.append("")
        L.append("38 페이지에 값이 있는데 읽지 못했거나 형식이 예상과 다릅니다. "
                 "아래 판정이 틀렸을 수 있으니 원문을 확인하세요.")
        L.append("")
        for name, w in parse_warnings:
            L.append("- **%s** — %s" % (name or "?", w))
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

        if any(r.get("forecast") for r in go + watch):
            L.append("### 기대손익 (실질)")
            L.append("")
            L.append("최대 청약한도로 청약했을 때의 예상 손익. 배정은 비례만 계산한 하한값이며, "
                     "기회비용은 연 3% 기준입니다.")
            L.append("")
            L.append("결과 범위는 개별 결과의 10~90% 구간, 평균 추정오차는 기대손익(평균) 자체의 "
                     "90% 부트스트랩 구간입니다.")
            L.append("")
            L.append("| 종목 | 투입증거금 | 예상배정 | 기대손익 | 결과 범위 (10~90%) | 평균 추정오차 | 흑자확률 |")
            L.append("|---|---:|---:|---:|---|---|---:|")
            for r in go + watch:
                f = r.get("forecast")
                if not f:
                    continue
                spread = "%s ~ %s" % (_won_signed(f["p10"]), _won_signed(f["p90"]))
                if not f.get("calibrated"):
                    # Out of the validated range: the spread only, no point estimate.
                    L.append("| %s | %s | %d주 | ⚠ 생략 | %s | - | - |"
                             % (_fc_name(r), _man(f["capital"]), f["shares_med"], spread))
                    continue
                L.append("| %s | %s | %d주 | %s | %s | %s ~ %s | %.0f%% |"
                         % (_fc_name(r), _man(f["capital"]), f["shares_med"],
                            _won_signed(f["ev"]), spread, _won_signed(f["ev_lo"]),
                            _won_signed(f["ev_hi"]), f["win_prob"]))
            L.append("")
            reasons = {r["forecast"].get("uncalibrated_reason") or "기관경쟁률 600:1 미만"
                       for r in go + watch
                       if r.get("forecast") and not r["forecast"].get("calibrated")}
            if "기관경쟁률 600:1 미만" in reasons:
                L.append("> ⚠ 기관경쟁률 600:1 미만인 종목은 모형 검증 범위 밖입니다. walk-forward에서 결과 범위가 "
                         "80% 대신 61%만 맞췄고 흑자확률은 과대평가됐으므로, 기대손익과 흑자확률은 생략합니다.")
                L.append("")
            if reasons - {"기관경쟁률 600:1 미만"}:
                L.append("> ⚠ 기관경쟁률 600:1 이상이어도 의무보유확약이 1% 미만이면 개인 청약이 저조해 배정이 "
                         "크게 늘 수 있습니다. walk-forward에서 결과 범위가 52%만 맞췄고 흑자확률 75% 대비 실제 "
                         "43%였으므로, 기대손익과 흑자확률은 생략합니다.")
                L.append("")

        est = [r for r in go + watch if (r.get("forecast") or {}).get("limit_estimated")]
        if est:
            L.append("> ⚠ 청약한도 추정: %s — 38에 청약한도가 아직 없어 증권사 배정물량의 4%%로 추정했습니다 "
                     "(괄호는 과거 10~90%% 범위). 투입증거금과 기대손익은 실제 한도에 비례해 달라지니 증권사 공고를 확인하세요."
                     % ", ".join("%s %s주 (%s~%s; %s)" % (
                         r["rec"].get("name"), "{:,}".format(r["rec"]["my_limit"]["shares"]),
                         "{:,}".format(r["rec"]["my_limit"]["low"]), "{:,}".format(r["rec"]["my_limit"]["high"]),
                         r["rec"]["my_limit"].get("note") or "")
                         for r in est))
            L.append("")

        fmt = "{:,}".format
        mls = [(r["rec"].get("name"), r["rec"].get("my_limit") or {}) for r in go + watch]
        moved = ["%s %s 38 %s~%s주 → 투자설명서 %s주" % (n, m["broker"], fmt(m["l38"][0]), fmt(m["l38"][1] or m["l38"][0]),
                                                  fmt(m["base"]))
                 for n, m in mls if m.get("source") == "prospectus" and m.get("l38") and m["l38"][0]
                 and m["l38"][0] != m.get("base")]
        if moved:
            L.append("> 청약한도 (100%% 기준)가 38과 다름 — 투자설명서를 따랐습니다: %s" % ", ".join(moved))
            L.append("")
        ranged = ["%s %s %s~%s주" % (n, m["broker"], fmt(m["ranged"][0]), fmt(m["ranged"][1]))
                  for n, m in mls if m.get("ranged")]
        if ranged:
            L.append("> ⚠ 투자설명서 청약한도가 아직 범위로만 공시됨 — 하한 기준: %s" % ", ".join(ranged))
            L.append("")
        unread = ["%s (%s)" % (n, m["limits_note"]) for n, m in mls if m.get("limits_note")]
        if unread:
            L.append("> 투자설명서 미확인 — 청약한도는 38 기준이며, 38의 하한은 증권사마다 다른 등급일 수 있습니다: %s"
                     % ", ".join(unread))
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
