"""GO / WATCH / PASS scoring for IPO subscription decisions.

The thesis this scores against: subscribe at the offer price, sell into the
opening bell. What predicts a strong open is (a) institutions competing hard in
수요예측, (b) the price being pushed to or above the top of the band, and
(c) a large share of institutional allocation being locked up (의무보유확약), which
removes supply from day-one selling.

Confinement tenor matters more than the headline percentage: 15일 확약 unlocks
well before you'd care, while 3/6개월 확약 keeps that stock off the tape entirely.
"""

from typing import Dict, List, Optional

DEFAULT_CONFIG = {
    # 기관경쟁률 (institutional demand-forecast competition ratio)
    "go_institutional_ratio": 800.0,
    "watch_institutional_ratio": 300.0,
    # 의무보유확약 (share of institutional demand voluntarily locked up)
    "go_lockup_pct": 10.0,
    "watch_lockup_pct": 5.0,
    # Share of locked-up stock at 3-month or longer tenor.
    "strong_long_tenor_share": 0.30,
    # Pricing above the indicative band is the single cleanest demand signal.
    "require_price_at_band_top": True,
    # Universe
    "include_spac": False,
    "include_konex": False,
    # Only alert this many days before the subscription window opens.
    "alert_lead_days": 10,
}


def long_tenor_share(breakdown: Dict[str, Optional[int]]) -> Optional[float]:
    """Fraction of locked-up shares committed for 3 months or longer."""
    total = breakdown.get("total")
    if not total:
        return None
    long_ = (breakdown.get("m6") or 0) + (breakdown.get("m3") or 0)
    return long_ / float(total)


def _price_position(rec: Dict) -> Optional[str]:
    """Where the final price landed relative to the indicative band."""
    final, low, high = rec.get("final_price"), rec.get("band_low"), rec.get("band_high")
    if not final or not high or not low:
        return None
    if final > high:
        return "above"
    if final == high:
        return "top"
    if final == low:
        return "bottom"
    return "within"


def evaluate(rec: Dict, config: Optional[Dict] = None) -> Dict:
    """Score one IPO record. Returns verdict + human-readable reasons."""
    cfg = dict(DEFAULT_CONFIG)
    if config:
        cfg.update(config)

    reasons: List[str] = []
    blockers: List[str] = []

    name = rec.get("name") or ""
    is_spac = "스팩" in name or "기업인수목적" in name
    if is_spac and not cfg["include_spac"]:
        return _verdict("EXCLUDED", 0, ["스팩 (SPAC) — excluded from universe"], rec, cfg)
    if (rec.get("market") or "") == "코넥스" and not cfg["include_konex"]:
        return _verdict("EXCLUDED", 0, ["코넥스 — excluded from universe"], rec, cfg)

    ratio = rec.get("institutional_ratio")
    if ratio is None:
        return _verdict("PENDING", 0, ["수요예측 결과 미발표 (no 기관경쟁률 yet)"], rec, cfg)

    score = 0

    # --- 기관경쟁률 -------------------------------------------------------
    if ratio >= cfg["go_institutional_ratio"]:
        score += 3
        reasons.append("기관경쟁률 %.2f:1 — strong (>= %g)" % (ratio, cfg["go_institutional_ratio"]))
    elif ratio >= cfg["watch_institutional_ratio"]:
        score += 1
        reasons.append("기관경쟁률 %.2f:1 — moderate" % ratio)
    else:
        blockers.append("기관경쟁률 %.2f:1 — weak (< %g)" % (ratio, cfg["watch_institutional_ratio"]))

    # --- pricing vs band -------------------------------------------------
    pos = _price_position(rec)
    if pos == "above":
        score += 2
        reasons.append("확정공모가 %s — above band top" % _fmt(rec.get("final_price")))
    elif pos == "top":
        score += 2
        reasons.append("확정공모가 %s — at band top" % _fmt(rec.get("final_price")))
    elif pos == "within":
        reasons.append("확정공모가 %s — inside band" % _fmt(rec.get("final_price")))
    elif pos == "bottom":
        blockers.append("확정공모가 %s — at band bottom (weak demand)" % _fmt(rec.get("final_price")))
    if cfg["require_price_at_band_top"] and pos in ("within", "bottom"):
        blockers.append("price did not reach band top")

    # --- 의무보유확약 ------------------------------------------------------
    lockup = rec.get("lockup_pct")
    if lockup is None:
        reasons.append("의무보유확약 미공개")
    elif lockup >= cfg["go_lockup_pct"]:
        score += 2
        reasons.append("의무보유확약 %.2f%% — strong" % lockup)
    elif lockup >= cfg["watch_lockup_pct"]:
        score += 1
        reasons.append("의무보유확약 %.2f%% — moderate" % lockup)
    else:
        reasons.append("의무보유확약 %.2f%% — low, expect day-one supply" % lockup)

    lts = long_tenor_share(rec.get("lockup_breakdown") or {})
    if lts is not None and (rec.get("lockup_pct") or 0) > 0:
        pct = lts * 100
        if lts >= cfg["strong_long_tenor_share"]:
            score += 1
            reasons.append("확약 중 3개월 이상 %.1f%% — supply held back" % pct)
        else:
            reasons.append("확약 중 3개월 이상 %.1f%% — mostly short tenor" % pct)

    # --- verdict ---------------------------------------------------------
    if blockers:
        verdict = "PASS"
    elif score >= 6:
        verdict = "GO"
    elif score >= 3:
        verdict = "WATCH"
    else:
        verdict = "PASS"

    return _verdict(verdict, score, reasons + ["⚠ " + b for b in blockers], rec, cfg)


def _verdict(verdict: str, score: int, reasons: List[str], rec: Dict, cfg: Dict) -> Dict:
    return {
        "verdict": verdict,
        "score": score,
        "reasons": reasons,
        "name": rec.get("name"),
        "no": rec.get("no"),
    }


def _fmt(n: Optional[int]) -> str:
    return "{:,}원".format(n) if n else "-"
