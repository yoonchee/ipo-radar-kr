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
    """'743.49:1' -> 743.49; a bare '187.10' -> 187.10.

    38 sometimes drops the ':1' (엠비디, 2026-11: '187.10'). A bare number is
    accepted only when it is the whole cell, so stray digits in surrounding
    text are never mistaken for a ratio.
    """
    if not s:
        return None
    m = (re.search(r"([\d,]+(?:\.\d+)?)\s*:\s*1", s)
         or re.fullmatch(r"\s*([\d,]+(?:\.\d+)?)\s*", s))
    if not m:
        return None
    return float(m.group(1).replace(",", ""))


_SUB_RATIO_RE = re.compile(
    r"청약경쟁률\s*([\d,.]+)\s*:\s*1(?:\s*\(\s*비례\s*([\d,.]+)\s*:\s*1\s*\))?")


def subscription_ratios(text: str) -> Dict[str, Optional[float]]:
    """'청약경쟁률 666.78:1 (비례 1334:1)' -> {"total": 666.78, "prop": 1334.0}.

    Spacing around the colon varies ('106.30 : 1' on 동방메디컬, 와이즈넛,
    산일전기), and REITs often publish the 통합 figure with no 비례 at all.
    Either half may come back None.
    """
    m = _SUB_RATIO_RE.search(text or "")
    if not m:
        return {"total": None, "prop": None}
    num = lambda g: float(g.replace(",", "")) if g and g.strip(",.") else None
    return {"total": num(m.group(1)), "prop": num(m.group(2))}


# 청약한도 as a share of the brokerage's own 일반 allotment (its 주식수 high
# end), measured over the 2023-06.. sample. Used ONLY when 38 publishes no limit
# at all -- 세나테크놀로지, or a live deal before its limits are filled in -- and
# every figure built on it is labelled 추정. The median is 4.0% either way:
#   single underwriter                  p10 3.2%  p90 6.2%  (n=191)
#   multi, largest-allotment row        p10 1.9%  p90 6.0%  (n=39)
# The estimate is taken at the largest allotment, since junior 인수회사 rows
# spread far wider (down to token limits).
LIMIT_SHARE = 0.040
LIMIT_SHARE_RANGE = {"single": (0.032, 0.062), "multi": (0.019, 0.060)}


def estimate_limit(underwriters: List[Dict]) -> Optional[Dict]:
    """A labelled stand-in 청약한도 when 38 publishes none, else None."""
    rows = [(u.get("name"), (u.get("shares") or {}).get("high")) for u in underwriters or []]
    rows = [(n, sh) for n, sh in rows if sh]
    if not rows:
        return None
    name, sh = max(rows, key=lambda r: r[1])
    basis = "single" if len(rows) == 1 else "multi"
    notes = ["우대 미적용"]
    if len(rows) > 1 and len({s for _, s in rows}) == 1:
        # Summary line only (세나: KB, 신한 both "140,000"): every brokerage got
        # the deal's combined 주식수, so this assumes the lead holds all of it.
        notes.append("주간사 합산 물량 기준 — 과대추정 가능")
    lo, hi = LIMIT_SHARE_RANGE[basis]
    return {"broker": name, "shares": int(round(sh * LIMIT_SHARE)),
            "low": int(round(sh * lo)), "high": int(round(sh * hi)),
            "basis_shares": sh, "note": ", ".join(notes)}


def _unreadable(label: str, raw: Optional[str], parsed, warnings: List[str]) -> None:
    """Record a cell that carries digits but did not parse.

    The parsers return None both for "not published yet" and for "published in
    a form we can't read", and downstream treats None as the former -- a
    PENDING deal, a skipped backtest row. Digits in the raw cell are what tell
    the two apart, so that is the test.
    """
    if parsed is None and raw and re.search(r"\d", raw):
        warnings.append("%s '%s' 해석 실패" % (label, raw[:40]))


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


def parse_demand_results(page: str, warnings: Optional[List[str]] = None) -> List[Dict]:
    """수요예측결과 (?o=r1) -> institutional demand-forecast results.

    A row whose 기관경쟁률 is blank or '-' has no result yet and is dropped. One
    that carries digits we cannot read is dropped too, but named in
    ``warnings`` -- that is how 엠비디's bare '187.10' went unseen.
    """
    out = []
    for row in _ROW_RE.findall(page):
        tds = _TD_RE.findall(row)
        if len(tds) < 8:
            continue
        c = [_norm(t) for t in tds]
        ratio = _ratio(c[5])
        if ratio is None:
            if warnings is not None and re.match(r"\d{4}\.\d\d\.\d\d", c[1]):
                tmp = []  # type: List[str]
                _unreadable("기관경쟁률", c[5], None, tmp)
                warnings.extend("%s: %s" % (c[0], w) for w in tmp)
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


def _is_broker(cell: str) -> bool:
    """A brokerage name, as opposed to a share count or a role (대표주관/인수).

    Not just '증권': DB금융투자 has none, and its rows were dropped outright.
    """
    return (not cell.endswith("주")
            and any(k in cell for k in ("증권", "투자", "금융"))
            and not re.match(r"[\d,~\s-]+$", cell))


def _same_broker(a: str, b: str) -> bool:
    """38 sometimes truncates a name ('신한투자' vs '신한투자증권')."""
    strip = lambda n: n[:-2] if n.endswith("증권") else n
    a, b = strip(a.strip()), strip(b.strip())
    return bool(a) and bool(b) and (a.startswith(b) or b.startswith(a))


def _parse_underwriters(f: "_Fields", warnings: Optional[List[str]] = None) -> List[Dict]:
    """Per-brokerage subscription terms.

    The block has a summary line (``주식수: N 주 / 청약한도: A~B 주``) and, on
    multi-underwriter deals, an ``인수회사 | 주식수 | 청약한도 | 기타`` table.
    Which of the two carries the limits varies, so both are read and merged:

    - Table rows with their own 청약한도, summary ``청약한도: -``: the usual
      multi-underwriter shape. The table is the whole answer.
    - No table: single-underwriter deals. The summary line is the whole answer.
    - Table rows reading ``- 주``, the range on the summary (에이치엘지노믹스).
    - Table listing only the junior 인수회사, the lead on the summary line alone
      (신영증권 27,000~36,000 with 유진 7,000 in the table; 두산로보틱스).

    So: take the table rows, then give the summary range to every summary-line
    brokerage the table left without a limit. Breaking any of these is silent --
    a short or limit-less broker list, never an exception.
    """
    start = f.index_of("주간사")
    if start < 0:
        return []
    end = next(
        (i for i, c in enumerate(f.cells[start:], start) if c.startswith("주요일정")),
        len(f.cells),
    )
    block = f.cells[start:end]
    none = {"low": None, "high": None}

    out = []
    header = next((i for i, c in enumerate(block) if c == "인수회사"), -1)
    if header >= 0:
        # Split rows at each brokerage name, not every 4 cells: an empty 기타
        # is dropped, leaving 3-cell rows, and a fixed stride then reads the next
        # brokerage's name as this one's role -- 13 cached deals lost every
        # underwriter after the first that way.
        body = block[header + 4:]
        starts = [i for i, c in enumerate(body) if _is_broker(c)]
        for k, i in enumerate(starts):
            row = body[i:starts[k + 1] if k + 1 < len(starts) else len(body)]
            row += [""] * (4 - len(row))
            limit = _num_range(row[2])
            out.append({
                "name": row[0],
                "shares": _num_range(row[1]),
                "limit_low": limit["low"],
                "limit_high": limit["high"],
                "role": row[3] or None,
            })

    if len(block) < 3:
        return out
    summary = block[2]
    shares_m = re.search(r"주식수\s*:\s*([\d,~\s]*\d)", summary)
    limit_m = re.search(r"청약한도\s*:\s*([\d,~\s]*\d)", summary)
    limit = _num_range(limit_m.group(1)) if limit_m else none
    # Comma-separated, except when 38 types dots (HD현대마린솔루션:
    # "KB증권,신한.하나.대신.삼성증권").
    named = [n.strip() for n in re.split(r"[,.·]", block[1]) if n.strip()]
    if out and limit["high"] is None:
        # The usual multi-underwriter shape. Every brokerage the summary line
        # names should have a table row; one that doesn't means rows were lost.
        if warnings is not None:
            have = [u["name"] for u in out]
            for name in named:
                if not any(_same_broker(name, h) for h in have):
                    warnings.append("주간사 %s 가 인수회사 표에 없음 — 행 파싱 확인" % name)
        return out
    for name in named:
        u = next((x for x in out if _same_broker(name, x["name"])), None)
        if u is None:
            out.append({
                "name": name,
                "shares": _num_range(shares_m.group(1)) if shares_m else none,
                "limit_low": limit["low"],
                "limit_high": limit["high"],
                "role": None,
            })
        elif u["limit_high"] is None:
            u["limit_low"], u["limit_high"] = limit["low"], limit["high"]
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

    warnings = []  # type: List[str]
    underwriters = _parse_underwriters(f, warnings)
    final_price = _int(f.get("확정공모가"))
    inst = _ratio(f.get("기관경쟁률"))
    lockup_pct = _float(f.get("의무보유확약"))
    _unreadable("확정공모가", f.get("확정공모가"), final_price, warnings)
    _unreadable("기관경쟁률", f.get("기관경쟁률"), inst, warnings)
    _unreadable("의무보유확약", f.get("의무보유확약"), lockup_pct, warnings)

    return {
        "no": no,
        "name": f.get("종목명"),
        "market": f.get("시장구분"),
        "ticker": f.get("종목코드"),
        "sector": f.get("업종"),
        "status": f.get("진행상황"),
        "band_low": band["low"],
        "band_high": band["high"],
        "final_price": final_price,
        "total_shares": _int(f.get("총공모주식수")),
        "offer_amount_mn": _int(f.get("공모금액")),
        "demand_start": demand["start"],
        "demand_end": demand["end"],
        "subscription_start": subs["start"],
        "subscription_end": subs["end"],
        "refund_date": _date(f.get("환불일")),
        "listing_date": _date(f.get("상장일")) or _date(f.get("신규상장일")),
        "institutional_ratio": inst,
        "lockup_pct": lockup_pct,
        "lockup_breakdown": lockup,
        "underwriters": underwriters,
        "retail_allocation": f.get("일반청약자"),
        "parse_warnings": warnings,
    }


def is_spac(name: Optional[str]) -> bool:
    if not name:
        return False
    return "스팩" in name or "기업인수목적" in name
