"""Parsers for 38.co.kr pages.

The detail page is a flat sequence of table cells in ``label, value`` order, so
extraction keys off the *label text* rather than cell position. That survives
layout shifts that would break positional indexing.
"""

import html
import re
from typing import Dict, List, Optional

_TAG_RE = re.compile(r"<[^>]+>")
_CELL_RE = re.compile(r"<t[dh][^>]*>(.*?)</t[dh]>", re.S)
_ROW_RE = re.compile(r"<tr[^>]*>(.*?)</tr>", re.S)
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
_NO_RE = re.compile(r"no=(\d+)")
_DATE_RANGE_RE = re.compile(r"(\d{4})\.(\d{2})\.(\d{2})\s*~\s*(?:(\d{4})\.)?(\d{2})\.(\d{2})")
_DATE_RE = re.compile(r"(\d{4})\.(\d{2})\.(\d{2})")


def _norm(s: str) -> str:
    """Strip tags/entities and collapse all whitespace (incl. nbsp, tabs)."""
    s = _TAG_RE.sub(" ", s)
    s = html.unescape(s).replace("\xa0", " ")
    return re.sub(r"\s+", " ", s).strip()


def _cells(page: str) -> List[str]:
    return [c for c in (_norm(c) for c in _CELL_RE.findall(page)) if c]


class _Fields(object):
    """Label -> next-cell lookup over the flat cell sequence."""

    def __init__(self, cells: List[str]):
        self.cells = cells

    def get(self, label: str, default: Optional[str] = None) -> Optional[str]:
        """Value cell following the first cell whose text *ends with* ``label``.

        Labels on this page are often prefixed by a section heading that got
        merged into the same cell (e.g. ``"수요예측결과 기관경쟁률"``), so an
        endswith match is more reliable than equality.
        """
        for i, c in enumerate(self.cells[:-1]):
            if c == label or c.endswith(" " + label) or c.endswith(label):
                return self.cells[i + 1]
        return default

    def index_of(self, label: str) -> int:
        for i, c in enumerate(self.cells):
            if c == label or c.endswith(" " + label) or c.endswith(label):
                return i
        return -1


def _int(s: Optional[str]) -> Optional[int]:
    if not s:
        return None
    m = re.search(r"-?[\d,]+", s.replace(" ", ""))
    if not m:
        return None
    try:
        return int(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _float(s: Optional[str]) -> Optional[float]:
    if not s:
        return None
    m = re.search(r"-?[\d,]+(?:\.\d+)?", s.replace(" ", ""))
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _ratio(s: Optional[str]) -> Optional[float]:
    """'743.49:1' -> 743.49"""
    if not s:
        return None
    m = re.search(r"([\d,]+(?:\.\d+)?)\s*:\s*1", s)
    if not m:
        return None
    return float(m.group(1).replace(",", ""))


def _date(s: Optional[str]) -> Optional[str]:
    if not s:
        return None
    m = _DATE_RE.search(s)
    return "%s-%s-%s" % m.groups() if m else None


def _date_range(s: Optional[str]) -> Dict[str, Optional[str]]:
    """'2026.08.12 ~ 2026.08.13' or '2026.08.12~08.13' -> start/end ISO dates."""
    if not s:
        return {"start": None, "end": None}
    m = _DATE_RANGE_RE.search(s)
    if m:
        y1, m1, d1, y2, m2, d2 = m.groups()
        return {"start": "%s-%s-%s" % (y1, m1, d1), "end": "%s-%s-%s" % (y2 or y1, m2, d2)}
    d = _date(s)
    return {"start": d, "end": d}


def _band(s: Optional[str]) -> Dict[str, Optional[int]]:
    """'30,000 ~ 41,200 원' -> low/high"""
    if not s:
        return {"low": None, "high": None}
    nums = re.findall(r"[\d,]+", s)
    vals = [int(n.replace(",", "")) for n in nums if n.strip(",")]
    if len(vals) >= 2:
        return {"low": vals[0], "high": vals[1]}
    if len(vals) == 1:
        return {"low": vals[0], "high": vals[0]}
    return {"low": None, "high": None}


# --------------------------------------------------------------------------
# List pages
# --------------------------------------------------------------------------

def parse_schedule_list(page: str) -> List[Dict]:
    """공모청약일정 (?o=k) -> upcoming subscription windows."""
    out = []
    for row in _ROW_RE.findall(page):
        tds = _TD_RE.findall(row)
        if len(tds) < 5:
            continue
        c = [_norm(t) for t in tds]
        joined = " ".join(c)
        if not _DATE_RANGE_RE.search(joined) and not _DATE_RE.search(joined):
            continue
        no_m = _NO_RE.search(row)
        if not no_m or not c[0]:
            continue
        rng = _date_range(c[1])
        out.append(
            {
                "no": no_m.group(1),
                "name": c[0],
                "subscription_start": rng["start"],
                "subscription_end": rng["end"],
                "final_price": _int(c[2]) if len(c) > 2 else None,
                "band": _band(c[3]) if len(c) > 3 else {"low": None, "high": None},
                "underwriters_raw": c[5] if len(c) > 5 else "",
            }
        )
    return out


def parse_new_listings(page: str) -> List[Dict]:
    """신규상장 (?o=nw) -> realised listing-day outcomes.

    ``시초/공모(%)`` is the return on the strategy this tool screens for:
    subscribe at the offer price, sell at the opening auction.
    """
    out = []
    for row in _ROW_RE.findall(page):
        tds = _TD_RE.findall(row)
        if len(tds) < 8:
            continue
        c = [_norm(t) for t in tds]
        if not re.search(r"\d{4}[/.]\d{2}[/.]\d{2}", " ".join(c)):
            continue
        if not c[0]:
            continue
        no_m = _NO_RE.search(row)
        out.append(
            {
                "no": no_m.group(1) if no_m else None,
                "name": c[0],
                "listing_date": c[1].replace("/", "-"),
                "offer_price": _int(c[4]),
                "open_price": _int(c[6]),
                "open_vs_offer_pct": _float(c[7]) if "%" in c[7] else None,
                "first_close": _int(c[8]) if len(c) > 8 else None,
            }
        )
    return out


def parse_demand_results(page: str) -> List[Dict]:
    """수요예측결과 (?o=r1) -> institutional demand-forecast results."""
    out = []
    for row in _ROW_RE.findall(page):
        tds = _TD_RE.findall(row)
        if len(tds) < 8:
            continue
        c = [_norm(t) for t in tds]
        ratio = _ratio(c[5])
        if ratio is None:
            continue
        no_m = _NO_RE.search(row)
        out.append(
            {
                "no": no_m.group(1) if no_m else None,
                "name": c[0],
                "band": _band(c[2]),
                "final_price": _int(c[3]),
                "institutional_ratio": ratio,
                "lockup_pct": _float(c[6]),
                "underwriters_raw": c[7],
            }
        )
    return out


# --------------------------------------------------------------------------
# Detail page
# --------------------------------------------------------------------------

_TENORS = [("6개월 확약", "m6"), ("3개월 확약", "m3"), ("1개월 확약", "m1"), ("15일 확약", "d15")]


def _num_range(s: Optional[str]) -> Dict[str, Optional[int]]:
    """'242,250 ~ 290,700 주' or '227,500 주' -> low/high."""
    if not s:
        return {"low": None, "high": None}
    vals = [int(n.replace(",", "")) for n in re.findall(r"[\d,]*\d", s)]
    if not vals:
        return {"low": None, "high": None}
    return {"low": vals[0], "high": vals[1] if len(vals) > 1 else vals[0]}


def _parse_underwriters(f: "_Fields") -> List[Dict]:
    """Per-brokerage subscription terms.

    Multi-underwriter deals render an ``인수회사 | 주식수 | 청약한도 | 기타``
    table, where 기타 carries the role (대표주관/공동주관/인수) and each row has
    its own 청약한도 -- the summary line above it shows ``청약한도: -``. Single
    -underwriter deals omit that table, so fall back to the summary line.
    """
    start = f.index_of("주간사")
    if start < 0:
        return []
    end = next(
        (i for i, c in enumerate(f.cells[start:], start) if c.startswith("주요일정")),
        len(f.cells),
    )
    block = f.cells[start:end]

    header = next((i for i, c in enumerate(block) if c == "인수회사"), -1)
    out = []
    if header >= 0:
        # 4-column rows following the 인수회사/주식수/청약한도/기타 header.
        for i in range(header + 4, len(block) - 3, 4):
            name = block[i]
            if not name or "증권" not in name:
                continue
            out.append(
                {
                    "name": name,
                    "shares": _num_range(block[i + 1]),
                    "limit_low": _num_range(block[i + 2])["low"],
                    "limit_high": _num_range(block[i + 2])["high"],
                    "role": block[i + 3] or None,
                }
            )
        if out:
            return out

    # Fallback: "주식수: 227,500 주 / 청약한도: 11,000~13,000 주"
    if len(block) >= 3:
        summary = block[2]
        shares_m = re.search(r"주식수\s*:\s*([\d,~\s]*\d)", summary)
        limit_m = re.search(r"청약한도\s*:\s*([\d,~\s]*\d)", summary)
        for name in [n.strip() for n in block[1].split(",") if n.strip()]:
            limit = _num_range(limit_m.group(1)) if limit_m else {"low": None, "high": None}
            out.append(
                {
                    "name": name,
                    "shares": _num_range(shares_m.group(1)) if shares_m else {"low": None, "high": None},
                    "limit_low": limit["low"],
                    "limit_high": limit["high"],
                    "role": None,
                }
            )
    return out


def parse_detail(page: str, no: str) -> Dict:
    """Per-stock detail page (?o=v&no=N) -> the full record we score on."""
    f = _Fields(_cells(page))

    band = _band(f.get("희망공모가액"))
    subs = _date_range(f.get("공모청약일"))
    demand = _date_range(f.get("수요예측일"))

    lockup = {}
    for label, key in _TENORS:
        lockup[key] = _int(f.get(label))
    lockup["total"] = _int(f.get("합계"))

    underwriters = _parse_underwriters(f)

    return {
        "no": no,
        "name": f.get("종목명"),
        "market": f.get("시장구분"),
        "ticker": f.get("종목코드"),
        "sector": f.get("업종"),
        "status": f.get("진행상황"),
        "band_low": band["low"],
        "band_high": band["high"],
        "final_price": _int(f.get("확정공모가")),
        "total_shares": _int(f.get("총공모주식수")),
        "offer_amount_mn": _int(f.get("공모금액")),
        "demand_start": demand["start"],
        "demand_end": demand["end"],
        "subscription_start": subs["start"],
        "subscription_end": subs["end"],
        "refund_date": _date(f.get("환불일")),
        "listing_date": _date(f.get("상장일")) or _date(f.get("신규상장일")),
        "institutional_ratio": _ratio(f.get("기관경쟁률")),
        "lockup_pct": _float(f.get("의무보유확약")),
        "lockup_breakdown": lockup,
        "underwriters": underwriters,
        "retail_allocation": f.get("일반청약자"),
    }


def is_spac(name: Optional[str]) -> bool:
    if not name:
        return False
    return "스팩" in name or "기업인수목적" in name
