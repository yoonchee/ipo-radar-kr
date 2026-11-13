"""OpenDART: the 증권발행실적보고서 every public offering files after its 청약.

It is the official record of how the 일반청약 actually went, and it carries
what 38.co.kr does not publish at all: the number of applications (청약건수),
the 균등/비례 split, and a per-brokerage table of every 청약 amount with the
균등 and 비례 shares each application received. 동원파츠 checked out exactly:
통합 1219.37 against 38's 1219.37, and 32,500주 applied -> 균등 1 + 비례 13,
which is what the user's account received.

It is filed AFTER the 청약 closes, so it is history only -- the backtest, the
실질손익 figures and the forecast's analogues.

The 투자설명서 is filed BEFORE the 청약, and it is the one live input here:
``live_limits`` reads each brokerage's 청약한도 tiers from it for the daily
screen, since 38's copy of them is not one consistent tier.

The API key is in the login keychain (service ``ipo-radar-dart``), never on
disk. Responses are cached under ``state/cache/dart/``; a filing does not
change once filed, except by a 정정, and the newest one is taken.
"""

import datetime
import html
import io
import json
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
import zipfile
from typing import Dict, List, Optional, Tuple

API = "https://opendart.fss.or.kr/api/"
KEYCHAIN_SERVICE = "ipo-radar-dart"
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE = os.path.join(_ROOT, "state", "cache", "dart")
DELAY_S = 1.0          # same courtesy as fetch.py, though OpenDART allows 20,000/day
_last = [0.0]
_refreshed = [False]   # the code list is re-downloaded at most once per process
LAST_ERROR = None      # type: Optional[str]
_codes_memo = {}       # type: Dict


def api_key() -> Optional[str]:
    try:
        out = subprocess.run(["security", "find-generic-password", "-s", KEYCHAIN_SERVICE,
                              "-a", "dart", "-w"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    key = out.stdout.strip()
    return key if re.fullmatch(r"[0-9a-f]{40}", key) else None


def _get(path: str, key: str, **params) -> bytes:
    wait = DELAY_S - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    params["crtfc_key"] = key
    url = API + path + "?" + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return r.read()
    finally:
        _last[0] = time.time()


def _corp_codes(key: str) -> Dict[str, str]:
    """stock_code -> corp_code, plus "name:<normalised name>" -> corp_code.

    KRX codes can contain letters (디티에스 0015M0). A company that only just
    listed has no stock_code in the file yet (넥스아이, 바로팜 the week they
    listed), so names are indexed too, for the fallback.
    """
    path = os.path.join(CACHE, "corpCode.xml")
    if os.path.exists(path) and _codes_memo.get("mtime") == os.path.getmtime(path):
        return _codes_memo["codes"]
    if not os.path.exists(path):
        os.makedirs(CACHE, exist_ok=True)
        z = zipfile.ZipFile(io.BytesIO(_get("corpCode.xml", key)))
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(z.read(z.namelist()[0]).decode("utf-8"))
    with open(path, encoding="utf-8") as fh:
        xml = fh.read()
    out = {}
    names = {}  # type: Dict[str, List[str]]
    for item in re.findall(r"<list>(.*?)</list>", xml, re.S):
        corp = re.search(r"<corp_code>(.*?)</corp_code>", item).group(1).strip()
        s = re.search(r"<stock_code>\s*([0-9A-Z]{6})\s*</stock_code>", item)
        if s:
            out[s.group(1)] = corp
        nm = re.search(r"<corp_name>(.*?)</corp_name>", item)
        if nm:
            names.setdefault(norm_name(nm.group(1)), []).append(corp)
    for n, corps in names.items():
        out["name:" + n] = ",".join(corps)
    _codes_memo.update(mtime=os.path.getmtime(path), codes=out)   # 116,000 entries: parse once
    return out


def norm_name(name: Optional[str]) -> str:
    """'디티에스(구.동양티에스)' / '산일전기 (유가)' / '㈜동원파츠' -> bare company name."""
    n = re.sub(r"\(.*?\)|㈜|주식회사", "", name or "")
    return re.sub(r"\s+", "", n)


REPORT = "증권발행실적보고서"
PROSPECTUS = "투자설명서"
# The 증권신고서 carries the same 청약한도 section as the 투자설명서 and is filed
# weeks earlier. DART's list does not index the 예비투자설명서 at all, so this
# is what the live screen reads until the 투자설명서 is filed.
REGISTRATION = "증권신고서(지분증권)"


def _is_kind(report_nm: str, kind: str) -> bool:
    """'투자설명서', '[기재정정]증권신고서(지분증권)', '[발행조건확정]…' -- the
    filing itself, with an optional bracketed tag. Not an attachment-only
    filing ('[첨부추가]…', '[첨부정정]…'), a 예비 version, or a notice that
    names one ('효력발생안내( 2026.10.2. 제출 증권신고서(지분증권) )').
    """
    m = re.match(r"^(?:\[([^\]]*)\])?\s*(.*)$", report_nm.strip())
    tag, rest = m.group(1) or "", m.group(2)
    if "첨부" in tag:
        return False
    if kind == REPORT:
        return rest.startswith(REPORT)
    return rest == kind


def report_xml(ticker: str, bgn: str, end: str, key: str, name: Optional[str] = None,
               kind: str = REPORT) -> Optional[str]:
    """The newest filing of ``kind`` made between ``bgn`` and ``end`` (ISO dates).

    ``kind`` is the 증권발행실적보고서 (the allocation record, filed after the
    청약) or the 투자설명서 (the offering terms, filed before it; the 예비
    version is not the final one). The company is found by 종목코드, else by
    name -- trying every company of that name and keeping the one that filed
    in the window.
    """
    global LAST_ERROR
    LAST_ERROR = None
    codes = _corp_codes(key)
    corps = [codes[ticker]] if ticker in codes else []
    code_path = os.path.join(CACHE, "corpCode.xml")
    # Listed since the code file was cached? Re-download it, but at most once
    # a day: a live deal has a ticker DART does not list until it lists, and
    # would otherwise re-fetch the whole file on every run.
    if not corps and not _refreshed[0] and _saved_before_today(code_path):
        _refreshed[0] = True
        try:
            os.remove(code_path)
        except OSError:
            pass
        codes = _corp_codes(key)
        corps = [codes[ticker]] if ticker in codes else []
    if not corps and name:
        corps = [c for c in codes.get("name:" + norm_name(name), "").split(",") if c]
    if not corps:
        LAST_ERROR = "종목코드 %s / 이름 %s 가 DART 고유번호 목록에 없음" % (ticker, name)
        return None
    for corp in corps:
        xml = _report_for_corp(corp, bgn, end, key, kind)
        if xml:
            return xml
    return None


def _saved_before_today(path: str) -> bool:
    if not os.path.exists(path):
        return True
    return time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(path))) < time.strftime("%Y-%m-%d")


def _listing_fresh(path: str, end: str) -> bool:
    """A cached filing list counts only if it was saved after its window closed.

    A list saved mid-window misses whatever is filed later -- the post-pricing
    정정 투자설명서 above all -- and the live screen queries the same window
    the backtest later reads, so a mid-deal listing would otherwise pin both.
    The filings themselves (``<rcept_no>.xml``) never change and stay cached.
    """
    if not os.path.exists(path):
        return False
    saved = time.strftime("%Y-%m-%d", time.localtime(os.path.getmtime(path)))
    return saved > end


def _report_for_corp(corp: str, bgn: str, end: str, key: str, kind: str = REPORT) -> Optional[str]:
    global LAST_ERROR

    list_path = os.path.join(CACHE, "list-%s-%s.json" % (corp, bgn) if kind == REPORT
                             else "list-%s-%s-%s.json" % (corp, bgn, end))
    if _listing_fresh(list_path, end):
        with open(list_path, encoding="utf-8") as fh:
            listing = json.load(fh)
    else:
        listing = json.loads(_get("list.json", key, corp_code=corp, bgn_de=bgn.replace("-", ""),
                                  end_de=end.replace("-", ""), page_count="100"))
        if listing.get("status") not in ("000", "013"):   # 013 = no results
            LAST_ERROR = "OpenDART list: %s %s" % (listing.get("status"), listing.get("message"))
            return None
        with open(list_path, "w", encoding="utf-8") as fh:
            json.dump(listing, fh, ensure_ascii=False)
    filings = [f for f in listing.get("list", []) if _is_kind(f.get("report_nm", ""), kind)]
    if not filings:
        LAST_ERROR = "%s~%s 에 %s 없음" % (bgn, end, kind)
        return None
    rcept = max(filings, key=lambda f: f["rcept_no"])["rcept_no"]   # newest, so a 정정 wins

    doc_path = os.path.join(CACHE, "%s.xml" % rcept)
    if not os.path.exists(doc_path):
        z = zipfile.ZipFile(io.BytesIO(_get("document.xml", key, rcept_no=rcept)))
        main = min(z.namelist(), key=len)
        with open(doc_path, "w", encoding="utf-8") as fh:
            fh.write(z.read(main).decode("utf-8", "replace"))
    with open(doc_path, encoding="utf-8") as fh:
        return fh.read()


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
def _text(s: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s))).strip()


def _tokens(xml: str) -> List[str]:
    """Every text run in document order -- table cells and the headings between them."""
    parts = re.split(r"<[^>]+>", xml)
    out = []
    for p in parts:
        t = re.sub(r"\s+", " ", html.unescape(p)).strip()
        if t:
            out.append(t)
    return out


def _n(s: Optional[str]) -> float:
    s = (s or "").replace(",", "").replace("주", "").strip()
    try:
        return float(s)
    except ValueError:
        return 0.0     # '-' or blank means none


def _is_num(s: Optional[str]) -> bool:
    return bool(re.fullmatch(r"\d[\d,]*(\.\d+)?\s*주?", (s or "").strip()))


_CELL = re.compile(r"<(T[DEHU])\b([^>]*)>(.*?)</\1>", re.S)


def _grid(table: str) -> List[List[Tuple[Optional[int], str]]]:
    """A <TABLE> as rows of (cell id, text), ROWSPAN and COLSPAN resolved.

    A spanned cell appears at every position it covers under one id, so a
    value is counted once however many rows or columns it spans. DART uses
    <TE> as well as <TD> for cells; reading <TD> alone drops columns.
    """
    grid = []
    below = {}   # column -> (rows still covered, id, text)
    cid = 0
    for tr in re.findall(r"<TR\b.*?</TR>", table, re.S):
        cells = _CELL.findall(tr)
        out, col, ci = [], 0, 0
        while True:
            if col in below:
                left, i, txt = below[col]
                out.append((i, txt))
                if left > 1:
                    below[col] = (left - 1, i, txt)
                else:
                    del below[col]
                col += 1
            elif ci < len(cells):
                _tag, attrs, body = cells[ci]
                ci += 1
                rs = re.search(r'ROWSPAN="(\d+)"', attrs)
                cs = re.search(r'COLSPAN="(\d+)"', attrs)
                rs, cs = int(rs.group(1)) if rs else 1, int(cs.group(1)) if cs else 1
                cid += 1
                txt = _text(body)
                for _ in range(cs):
                    out.append((cid, txt))
                    if rs > 1:
                        below[col] = (rs - 1, cid, txt)
                    col += 1
            elif any(c > col for c in below):
                out.append((None, ""))
                col += 1
            else:
                break
        grid.append(out)
    return grid


# A brokerage name inside a heading: '[한국투자증권]', '1) 대신증권(주)- 일반투자자
# 세부 배정 현황', '2) (주)아이엠증권', '- (주)BNK투자증권 일반투자자 세부 배정 현황'.
_BROKER_NAME = re.compile(r"((?:[A-Z]{2,4}|[가-힣]{2,8})(?:금융투자|투자증권|증권))")


def _heading_broker(t: List[str]) -> Optional[str]:
    """The brokerage named by the closest heading in ``t``, read from the end.

    Prose notes ('주1) 한국투자증권(주)는 일반배정물량 ...') name a brokerage
    too, so only short heading-like lines count.
    """
    for x in reversed(t[-40:]):
        # '[KB증권]' is a heading wherever it sits, even at the end of a line
        # that also says 배정방식별 (세나테크놀로지).
        for br in re.findall(r"\[([^\]]{2,20})\]", x):
            m = _BROKER_NAME.search(br.replace("㈜", "").replace("(주)", "").replace(" ", ""))
            if m:
                return m.group(1)
        if len(x) > 45 or "는 " in x or "의 " in x or "배정방식별" in x:
            continue
        m = _BROKER_NAME.search(x.replace("㈜", "").replace("(주)", "").replace(" ", ""))
        if m and not m.group(1).startswith("일반"):
            return m.group(1)
    return None


def _labels(header: List[List[Tuple[Optional[int], str]]], width: int) -> List[Tuple[str, str]]:
    """(group, label) per column: the header cells above it, top to bottom.

    The group is the spanning cell that says 균등 or 비례 -- it decides what an
    addend is, because leaf labels lie (데이원 calls its 비례 column
    '균등배정수량(D)' under a '비례방식' group).
    """
    out = []
    for j in range(width):
        parts, seen = [], set()
        for row in header:
            if j < len(row) and row[j][0] not in seen and row[j][1]:
                seen.add(row[j][0])
                parts.append(row[j][1].replace(" ", ""))
        group = next((p for p in parts[:-1] if "균등" in p or "비례" in p), "")
        out.append((group, "/".join(parts)))
    return out


def _has_total(labels: List[Tuple[str, str]]) -> bool:
    return any("총배정" in lab or re.search(r"\(\s*[A-H]\s*=\s*[A-H]\s*\+", lab) for _g, lab in labels)


_CLOSERS = ("합계", "소계", "총계", "계")
_CIRCLED = {"B": "ⓑ", "C": "ⓒ", "D": "ⓓ"}


def _roles(labels: List[Tuple[str, str]]) -> Optional[Dict]:
    """Which column is which, from the header text -- never from position.

    The total column names its own addends: '(D=B+C)', or '(E=B+C+D)' where
    균등 is printed as two groups (데이원, 닷밀). Each addend is the column
    labelled with its letter, else the last column of its group (큐리옥스's
    비례 total is '최종비례배정수량(g=e+f)', no C). Every row is then proven by
    total = sum of addends, exactly.
    """
    def find(pred):
        # A label can break over header rows ('청약' / '건수', 채비's KB table),
        # so it is matched with the breaks removed.
        return next((j for j, (_g, lab) in enumerate(labels) if pred(lab.replace("/", ""))), None)

    unit = find(lambda s: "청약단위" in s)
    # '쳥악건수' (비아이매트릭스): the first 건수 that is not a 배정건수.
    apps = find(lambda s: "청약건수" in s) or find(lambda s: s.endswith("건수") and "배정" not in s)
    qty = find(lambda s: "청약수량" in s) or find(lambda s: "(A)" in s)
    if unit is None or apps is None:
        return None
    total = find(lambda s: re.search(r"\(\s*[A-H]\s*=\s*[A-H](\s*\+\s*[A-H])+\s*\)", s) is not None) \
        or find(lambda s: "총배정" in s)
    ratio = find(lambda s: "배정비율" in s or "/A)" in s)
    letters = []
    if total is not None:
        m = re.search(r"\(\s*[A-H]\s*=\s*([A-H](?:\s*\+\s*[A-H])+)\s*\)", labels[total][1])
        if m:
            letters = re.findall(r"[A-H]", m.group(1))
    addends = []   # (column, '균등' | '비례')
    for x in letters or ["B", "C"]:
        # '(C)', '(B=c-d)', 'ⓒ', or 'C)' with its '(' missing (전진건설로봇).
        j = find(lambda s, x=x: re.search(r"\(\s*%s\s*[)=]|(?<![A-Za-z(])%s\)" % (x, x), s) is not None
                 or _CIRCLED.get(x, "#") in s)
        if j is not None and j != total:
            g = labels[j][0]
            kind = "균등" if "균등" in g else "비례" if "비례" in g else ("균등" if x == "B" else "비례")
        else:
            # Unlabelled: the last column of the group the letter stands for.
            kind = "균등" if x == "B" else "비례"
            cols = [k for k, (g, _l) in enumerate(labels) if kind in g and k != total]
            if not cols:
                return None
            j = cols[-1]
        addends.append((j, kind))
    return {"unit": unit, "apps": apps, "qty": qty, "total": total, "ratio": ratio, "addends": addends}


def _alloc_tables(xml: str) -> List[Dict]:
    """Every 일반투자자 세부 배정현황 table in the filing body, in order.

    Found by its header labels wherever it sits. Filings put these tables in
    section 4, after section 5 (엠앤씨솔루션, HD현대마린솔루션, 두산로보틱스) or
    inside section 3 (케이엔알시스템). Only the body counts: a 정정 filing
    opens with 정정 전/후 copies of its tables, before the <COVER> (유라클 had
    three of everything).
    """
    cover = xml.find("<COVER>")
    body = xml[cover:] if cover >= 0 else xml
    out, pos = [], 0
    for m in re.finditer(r"<TABLE\b.*?</TABLE>", body, re.S):
        # Heading context runs back to the previous allocation table, small
        # tables included: a '(단위: 주)' caption table sits between
        # '[삼성증권]' and its table (엠앤씨솔루션).
        before = _tokens(body[pos:m.start()])
        grid = _grid(m.group(0))
        first = next((i for i, r in enumerate(grid)
                      if r and _is_num(r[0][1]) and any("청약단위" in c[1].replace(" ", "") for h in grid[:i] for c in h)),
                     None)
        if first is None:
            continue
        pos = m.end()
        width = max(len(r) for r in grid)
        out.append({"before": before, "grid": grid, "first": first,
                    "labels": _labels(grid[:first], width)})
    # A brokerage can split its table in two -- 균등 columns in one, 비례 and
    # the total in the next, same 청약단위 rows (큐리옥스) -- so a table with
    # no total column is joined to the one after it, row for row.
    merged = []
    for t in out:
        prev = merged[-1] if merged else None
        if (prev and not prev.get("joined") and not _has_total(prev["labels"])
                and not "".join(t["before"]).strip()):
            a, b = prev["grid"][prev["first"]:], t["grid"][t["first"]:]
            if [r[0][1] for r in a] == [r[0][1] for r in b]:
                # Rows padded to the first half's width so the second half's
                # cells stay under their own labels; its ids are kept distinct.
                w = len(prev["labels"])
                prev["labels"] = prev["labels"] + t["labels"]
                prev["grid"] = prev["grid"][:prev["first"]] + [
                    ra + [(None, "")] * (w - len(ra)) + [(None if i is None else i + 10 ** 6, x) for i, x in rb]
                    for ra, rb in zip(a, b)]
                prev["joined"] = True
                continue
        merged.append(t)
    return merged


def _table_rows(t: Dict) -> Tuple[List[Dict], Dict]:
    """Logical rows of one allocation table, each proven by its own arithmetic.

    A unit can run over several grid rows: cells spanning down (ROWSPAN) are
    the same cell, and the extra lines carry further groups -- 닷밀 prints 313
    비례 shares on a second line under a 20주 row. Addends are summed over the
    unit's lines; the main line alone is tried first (래블업's sub-lines split
    a 균등 total the main line already carries).
    """
    roles = _roles(t["labels"])
    stats = {"rejected": 0, "qty_forms": {}, "closer": None}
    if roles is None or roles["total"] is None:
        return [], stats
    groups = []   # [[grid rows of one unit], ...]
    last_unit_id = None
    for r in t["grid"][t["first"]:]:
        if roles["unit"] >= len(r):
            continue
        uid, utext = r[roles["unit"]]
        key = utext.replace(" ", "")
        if key in _CLOSERS:
            stats["closer"] = {"apps": _n(r[roles["apps"]][1]) if roles["apps"] < len(r) else None}
            break
        if uid == last_unit_id or not utext:
            if groups:
                groups[-1].append(r)
            continue
        last_unit_id = uid
        groups.append([r] if _is_num(utext) else [])
    rows = []
    for g in groups:
        if not g:
            continue
        main = g[0]
        cell = lambda r, j: r[j] if j is not None and j < len(r) else (None, "")
        unit, apps = _n(cell(main, roles["unit"])[1]), _n(cell(main, roles["apps"])[1])
        total = _n(cell(main, roles["total"])[1])
        if unit <= 0 or apps <= 0:
            continue

        def sums(lines):
            s = {"균등": 0.0, "비례": 0.0}
            for j, kind in roles["addends"]:
                seen = set()
                for r in lines:
                    i, x = cell(r, j)
                    if i is not None and i not in seen:
                        seen.add(i)
                        s[kind] += _n(x)
            return s
        s = sums(g[:1])
        if s["균등"] + s["비례"] != total:
            s = sums(g)
        if s["균등"] + s["비례"] != total:
            stats["rejected"] += 1
            continue
        qty = unit * apps     # by definition; the printed 청약수량 is only checked
        printed = _n(cell(main, roles["qty"])[1])
        form = ("full" if printed == qty
                # NH prints the 증거금 basis net of 균등: 단위 x 건수 / 2 - B
                # (루미르, 유진테크놀로지, 코스모로보틱스's NH table).
                else "half_net" if abs(2 * printed - (qty - 2 * s["균등"])) <= 1
                # A typo, which the summary reconciliation must absorb (위너스:
                # 50,472,000 printed for 42,000 x 10,187 = 427,854,000).
                else "typo")
        stats["qty_forms"][form] = stats["qty_forms"].get(form, 0) + 1
        rows.append({"unit": int(unit), "apps": int(apps), "qty": int(qty), "qty_printed": printed, "total": total,
                     "eq_total": s["균등"], "prop_total": s["비례"], "qty_form": form})
    return rows, stats


def parse_report(xml: str) -> Dict:
    """Per-brokerage 일반투자자 allocation tables from one filing, plus its summary.

    Tables are read as tables: rows and columns from the XML, column roles
    from the header labels, every row proven by total = 균등 + 비례 exactly.
    The filing is then checked against its own summary by the caller.
    """
    tables = []
    broker = None
    for t in _alloc_tables(xml):
        if t["grid"] is None:
            continue
        rows, stats = _table_rows(t)
        if not rows:
            continue
        b = _heading_broker(t["before"])
        # A table that only continues the previous one (no heading between
        # them) belongs to the same brokerage.
        broker = b or (broker if not "".join(t["before"]).strip() else None)
        tables.append({"broker": broker, "rows": rows, "rejected": stats["rejected"],
                       "qty_forms": stats["qty_forms"], "closer": stats["closer"],
                       "allotted": sum(x["total"] for x in rows),
                       "apps": sum(x["apps"] for x in rows), "qty": sum(x["qty"] for x in rows),
                       "qty_printed": sum(x["qty_printed"] for x in rows),
                       "eq_pool": sum(x["eq_total"] for x in rows),
                       "prop_pool": sum(x["prop_total"] for x in rows)})
    return {
        "tables": tables,
        "apps": sum(x["apps"] for x in tables), "qty": sum(x["qty"] for x in tables),
        # What the filer's own 합계 adds up: NH's half-net A where NH prints it.
        "qty_printed": sum(x["qty_printed"] for x in tables),
        "eq_pool": sum(x["eq_pool"] for x in tables), "prop_pool": sum(x["prop_pool"] for x in tables),
        "allotted": sum(x["allotted"] for x in tables),
        "summary": _summary(xml),
    }


def _summary(xml: str) -> Optional[Dict]:
    """The filing's own 일반투자자 totals, from the '3. 청약 및 배정현황' table.

    Columns by header label: 최초 배정 수량; 청약 현황 건수, 수량, 금액; 최종 배정
    현황 건수, 수량, 금액. This is what the per-brokerage tables must add up
    to -- the filing checked against itself. 38's 통합경쟁률 is a different
    number: the ratio announced at close, before 중복·부적격 청약 were voided
    (빅웨이브로보틱스: 38 1375.34, filing 1307.78).
    """
    cover = xml.find("<COVER>")
    body = xml[cover:] if cover >= 0 else xml
    for m in re.finditer(r"<TABLE\b.*?</TABLE>", body, re.S):
        grid = _grid(m.group(0))
        flat = "".join(c[1] for r in grid[:3] for c in r).replace(" ", "")
        if not ("최초" in flat and "청약현황" in flat and "최종" in flat):
            continue
        first = next((i for i, r in enumerate(grid) if r and r[0][1].replace(" ", "") in
                      ("일반투자자", "일반청약자", "우리사주조합", "기관투자자")), None)
        if first is None:
            continue
        labels = _labels(grid[:first], max(len(r) for r in grid))
        col = {}
        for j, (_g, lab) in enumerate(labels):
            sec = "alloc" if "최초" in lab else "final" if "최종" in lab else "sub" if "청약" in lab else None
            fld = "qty" if lab.endswith("수량") else "apps" if lab.endswith("건수") else \
                "amount" if lab.endswith("금액") else None
            if sec and fld and (sec, fld) not in col:
                col[(sec, fld)] = j
        for r in grid[first:]:
            if r[0][1].replace(" ", "") in ("일반투자자", "일반청약자"):
                v = lambda k: _n(r[col[k]][1]) if k in col and col[k] < len(r) else None
                return {"alloc": v(("alloc", "qty")), "apps": v(("sub", "apps")), "qty": v(("sub", "qty")),
                        "amount": v(("sub", "amount")), "final": v(("final", "qty")),
                        "final_amount": v(("final", "amount"))}
    return None


def summary_qty(summ: Dict, price: Optional[float]) -> List[float]:
    """Every 청약수량 the summary supports: the printed one, and the one its 금액
    implies. Filers print 금액 at the full price or at the 50% 증거금, and the
    수량 cell carries typos the 금액 does not (바이오비쥬: 884,413,080 printed,
    금액 = 884,143,080 x 9,100 / 2 -- which is what its tables add up to), or
    shift a whole row one cell (이지스: 청약수량 under 건수, 배정 under 수량)."""
    out = [summ["qty"]] if summ.get("qty") else []
    if summ.get("qty") and summ["qty"] in (summ.get("final"), summ.get("alloc")):
        # 청약 = 배정: the filer copied a column (신성에스티, 필에너지,
        # 엑셀세라퓨틱스), and a 금액 computed from the copy is no better.
        out = []
    explained = price and summ.get("qty") and any(
        abs(summ["amount"] - summ["qty"] * p) < 0.5 for p in (price, price / 2.0)) if summ.get("amount") else True
    if price and summ.get("amount") and not explained:
        # Only a 금액 the printed 수량 does not explain says something new: one
        # computed from a copied 수량 (신성에스티: 500,000 x 26,000) would
        # otherwise offer 1,000,000 at half price.
        for p in (price, price / 2.0):
            q = summ["amount"] / p
            if abs(q - round(q)) < 1e-6 and round(q) not in (summ.get("final"), summ.get("alloc")):
                out.append(round(q))
    return out


# Filings spell some brokerages out in Hangul where 38 uses the Latin form.
_ALIASES = [("케이비", "KB"), ("엔에이치", "NH"), ("에스케이", "SK"), ("디비", "DB"),
            ("아이비케이", "IBK"), ("비엔케이", "BNK"), ("아이엠", "IM")]


def _same_broker(a: Optional[str], b: Optional[str]) -> bool:
    def strip(n):
        n = re.sub(r"(투자증권|금융투자|증권|㈜|\(주\)|\s)", "", n or "").upper()
        for ko, en in _ALIASES:
            n = n.replace(ko, en)
        return n
    a, b = strip(a), strip(b)
    return bool(a) and bool(b) and (a.startswith(b) or b.startswith(a))

def allocation_at(report: Dict, broker: Optional[str], limit: int,
                  interpolate: bool = True) -> Optional[Dict]:
    """Expected shares for one application of ``limit`` at ``broker``.

    Read from the row for that 청약단위: the unit's 균등 total and 비례 total
    over its 청약건수. That is exact in expectation whatever the filing's
    sub-row layout, lottery top-ups included.

    When nobody applied at exactly ``limit`` -- undersubscribed deals list only
    the amounts people chose (동인기연: 16,000 and 20,000, no 18,000) -- the
    two rows either side are interpolated, if they agree on the 비례 rate per
    share. Nothing is read from a single neighbour: a nearby smaller unit once
    gave 44주 for a 420,000주 "limit" that was really 38's share count.
    """
    table = next((x for x in report["tables"] if _same_broker(x["broker"], broker)), None)
    if table is None and len(report["tables"]) == 1 and report["tables"][0]["broker"] is None:
        # One unnamed table: a single-underwriter filing with no heading.
        table = report["tables"][0]
    if table is None:
        return None
    rows = [x for x in table["rows"] if x["apps"]]
    base = {"broker": table["broker"], "applications": table["apps"]}
    r = next((x for x in rows if x["unit"] == limit), None)
    if r is not None:
        base.update(unit=r["unit"], exact=True, interpolated=None,
                    shares_equal=r["eq_total"] / r["apps"], shares_prop=r["prop_total"] / r["apps"],
                    shares_total=r["total"] / r["apps"])
        return base
    if not interpolate:
        return None
    lo = max((x for x in rows if x["unit"] < limit), key=lambda x: x["unit"], default=None)
    hi = min((x for x in rows if x["unit"] > limit), key=lambda x: x["unit"], default=None)
    if lo is None or hi is None:
        return None
    p_lo, p_hi = lo["prop_total"] / lo["apps"], hi["prop_total"] / hi["apps"]
    # The same rate per share on both sides, to rounding: within a share or 5%.
    if abs(p_lo / lo["unit"] * hi["unit"] - p_hi) > max(1.0, 0.05 * p_hi):
        return None
    w = (limit - lo["unit"]) / float(hi["unit"] - lo["unit"])
    e_lo, e_hi = lo["eq_total"] / lo["apps"], hi["eq_total"] / hi["apps"]
    base.update(unit=limit, exact=False, interpolated=(lo["unit"], hi["unit"]),
                shares_equal=e_lo + w * (e_hi - e_lo), shares_prop=p_lo + w * (p_hi - p_lo))
    base["shares_total"] = base["shares_equal"] + base["shares_prop"]
    return base


# --------------------------------------------------------------------------
# 투자설명서: the 청약한도 each brokerage actually offered
# --------------------------------------------------------------------------

_QTY = r"([\d,]+)\s*(?:주|DR|좌)"
_RANGE = re.compile(_QTY + r"(?:\s*~\s*" + _QTY + r")?")


def _range(s: str) -> Optional[Tuple[int, int]]:
    m = _RANGE.search(s or "")
    if not m:
        return None
    lo = int(m.group(1).replace(",", ""))
    hi = int(m.group(2).replace(",", "")) if m.group(2) else lo
    return (lo, hi) if lo > 0 else None


def parse_limits(xml: str) -> Dict[str, Dict]:
    """Per-brokerage 일반청약자 limits from a 투자설명서.

    Each brokerage's row in the '일반청약자 배정물량, 최고청약한도' table gives
    its 배정물량 and a 최고청약한도 -- which is the top tier at some (NH prints
    300% there) and a pointer to a note at others ('주1)', 삼성). The base, the
    100% tier, is read from what follows the table: a tier row ('일반
    청약자격 | 100% | 15,000주'), or a note ('…최고청약한도는 8,600주 (100%)이나
    …'). A brokerage whose limit is the same for every customer (DB: '일반고객 및
    우대고객에게 동일하게 적용') has only the table's figure, and that is its
    base. Values are (low, high): before pricing a range, ~ the upsized size.
    """
    cover = xml.find("<COVER>")
    body = xml[cover:] if cover >= 0 else xml
    tables = list(re.finditer(r"<TABLE\b.*?</TABLE>", body, re.S))
    out = {}
    for n, m in enumerate(tables):
        grid = _grid(m.group(0))
        if not grid:
            continue
        head = "".join(c[1] for c in grid[0]).replace(" ", "")
        # '최고청약한도', or just '청약한도' (하이투자증권: one limit for all).
        if not ("청약한도" in head and "배정물량" in head):
            continue
        labels = [c[1].replace(" ", "") for c in grid[0]]
        pc = next((j for j, l in enumerate(labels) if "배정물량" in l), None)
        lc = next((j for j, l in enumerate(labels) if "청약한도" in l), None)
        rows = {}
        # A row labelled '일반청약자' takes the brokerage from the caption just
        # above the table ('【SK증권㈜의 일반청약자 배정물량, …】', 로킷헬스케어).
        caption = _tokens(body[tables[n - 1].start() if n else 0:m.start()])[-3:]
        cap = _BROKER_NAME.search("".join(caption).replace("㈜", "").replace("(주)", "").replace(" ", ""))
        for r in grid[1:]:
            name = _BROKER_NAME.search(r[0][1].replace("㈜", "").replace("(주)", "").replace(" ", ""))
            if not name and cap and "일반" in r[0][1]:
                name = cap
            if not name or pc is None or lc is None or max(pc, lc) >= len(r):
                continue
            rows[name.group(1)] = {"pool": _range(r[pc][1]), "table": _range(r[lc][1]),
                                   "base": None, "tiers": {}}
        if not rows:
            continue
        # What follows: the notes and tier tables, up to the next heading-like
        # paragraph is too fragile, so a fixed window of what comes next.
        after = _tokens(body[m.end():tables[n + 4].start() if n + 4 < len(tables) else m.end() + 20000])
        # Inline tags split a note across text runs ('12,000주~14,000주' |
        # '(100%)이나 …'), so the window is matched as one string; a tier
        # belongs to the brokerage named last before it.
        text = " ".join(after)
        # Tags inside numbers leave spaces in them: '15, 0 00주 (10 0%)' (에스오에스랩).
        text = re.sub(r"(?<=[\d,])\s+(?=[\d,%])", "", text)
        events = []
        for q in _BROKER_NAME.finditer(text.replace("㈜", " ").replace("(주)", "   ")):
            hit = next((b for b in rows if _same_broker(b, q.group(1))), None)
            if hit:
                events.append((q.start(), "broker", hit))
        rng = _QTY + r"(?:\s*~\s*" + _QTY + r")?"
        # In order of trust; a tier keeps the first figure found for it.
        patterns = [
            # '25,000주 (100%)', '18,000 주 (온라인, 100%)' -- 대신, 미래, NH, 한투, 유안타
            (0, rng + r"\s*\([^()%]{0,12}?(\d+)\s*%\s*\)", (3, 1, 2)),
            # tier table '일반 청약자격 | 100% | 15,000주' -- 삼성
            (1, r"(?<![\d.(])(\d+)\s*%\s+" + rng, (1, 2, 3)),
            # '청약한도의 100%인 8,000주~10,000주' -- 한투 (에스에프씨)
            # ('배정물량의 25%인 2,500,000주' is not a tier, hence the anchor)
            (1, r"청약\s*한도의\s*(\d+)\s*%\s*인\s*" + rng, (1, 2, 3)),
            # '일반(100%)그룹의 청약한도: 7,000주', '일반고객(100%)의 청약한도 : 15,000주' -- 신영, IBK
            (2, r"\(\s*(\d+)\s*%\s*\)[^\d()%]{0,20}?" + rng, (1, 2, 3)),
            # no percentage: '일반청약자 최고청약한도는 20,000주 이나 …' -- 하나, BNK
            (3, r"일반청약자\S{0,2}\s*최고\s*(?:청약\s*)?한도는\s*" + rng, (None, 1, 2)),
            # no percentage: '일반고객의 청약한도 : 10,000주' -- 하나
            (3, r"일반(?:고객|그룹|청약자격|청약자)[^\d:()%]{0,8}청약\s*한도\s*:?\s*" + rng, (None, 1, 2)),
        ]
        for rank, pat, (gp, glo, ghi) in patterns:
            for q in re.finditer(pat, text):
                pct = int(q.group(gp)) if gp else 100
                events.append((q.start(), "tier", (rank, pct, q.group(glo), q.group(ghi))))
        found = {}   # (broker, pct) -> (rank, value)
        current = next(iter(rows)) if len(rows) == 1 else None
        for _pos, kind, v in sorted(events, key=lambda e: e[0]):
            if kind == "broker":
                current = v
            elif current is not None:
                rank, pct, lo, hi = v
                lo = int(lo.replace(",", ""))
                val = (lo, int(hi.replace(",", "")) if hi else lo)
                if (current, pct) not in found or rank < found[(current, pct)][0]:
                    found[(current, pct)] = (rank, val)
        for (b, pct), (_r, val) in found.items():
            rows[b]["tiers"][pct] = val
        for b, v in rows.items():
            v["derived"] = False
            v["base"] = v["tiers"].get(100) or (v["table"] if not v["tiers"] else None)
            if v["base"] is None:
                # Tiers are multiples of the base by definition ('청약한도의
                # 200%'), so a missing 100% line is the 200% figure halved
                # (씨어스: 20,000 -> 10,000, which its table also says).
                for pct in (200, 300, 50, 150, 250, 120):
                    if pct in v["tiers"] and v["tiers"][pct][0] > 0:
                        lo, hi = v["tiers"][pct]
                        v["base"], v["derived"] = (round(lo * 100.0 / pct), round(hi * 100.0 / pct)), True
                        break
        for b, v in rows.items():
            out.setdefault(b, v)
    return out


def prospectus_limit(limits: Dict[str, Dict], alloc: Optional[float],
                     prefer: Optional[str] = None, report: Optional[Dict] = None) -> Optional[Dict]:
    """The deal's standard 청약한도: the 100% tier at the brokerage offering the most.

    Most final 투자설명서 print one figure. Some keep the pre-pricing range,
    whose two ends are two possible 일반 allotments (upsizing, or a (유가)
    deal's 25-30% 일반 share). Which end applied is read from the filing's
    rows: applicants pile up at each tier's maximum, so the end with more
    applications across its tier amounts wins (씨케이솔루션: 12,355 / 1,994 /
    1,695 at the high end's 100/150/200% against 1,055 / 144 / 53 at the
    low end's; 져스텍: 11,327 at the low end's 40,000, none at 48,000). With
    no rows at either end, the filing's 일반 allotment against the
    brokerages' summed allotments decides.

    If any brokerage's base was not read, the choice is limited to
    ``prefer`` (38's brokerage), since the best may be the one missing.
    """
    based = {b: v for b, v in limits.items() if v.get("base")}
    if not based:
        return None
    lo_sum = sum((v["pool"] or (0, 0))[0] for v in limits.values())
    hi_sum = sum((v["pool"] or (0, 0))[1] for v in limits.values())
    upsized = bool(alloc and hi_sum > lo_sum and alloc >= hi_sum * 0.999)

    def end(b: str, v: Dict) -> Tuple[int, str]:
        lo, hi = v["base"]
        if lo == hi:
            return lo, "single"
        table = next((t for t in (report or {}).get("tables", []) if _same_broker(t["broker"], b)), None)
        if table is None and report and len(report["tables"]) == 1 and report["tables"][0]["broker"] is None:
            table = report["tables"][0]
        apps = {r["unit"]: r["apps"] for r in (table or {}).get("rows", [])}
        # Every tier's amount at each end, counted once: a low end's 30,000
        # can be the high end's 250% too (씨케이솔루션).
        ends_lo = {lo} | {t[0] for t in v["tiers"].values()}
        ends_hi = {hi} | {t[1] for t in v["tiers"].values()}
        score_lo = sum(apps.get(u, 0) for u in ends_lo - ends_hi)
        score_hi = sum(apps.get(u, 0) for u in ends_hi - ends_lo)
        if score_lo != score_hi:
            return (hi, "rows") if score_hi > score_lo else (lo, "rows")
        return (hi, "allotment") if upsized else (lo, "allotment")

    if len(based) < len(limits):
        based = {b: v for b, v in based.items() if _same_broker(b, prefer)}
        if not based:
            return None
    ends = {b: end(b, v) for b, v in based.items()}
    b = max(ends, key=lambda k: ends[k][0])
    return {"broker": b, "limit": ends[b][0], "derived": based[b]["derived"], "end": ends[b][1]}


def live_limits(rec: Dict, key: Optional[str]) -> Tuple[Optional[Dict[str, Dict]], Optional[str]]:
    """Per-brokerage limits for a live deal from its filings, or (None, why not).

    The newest non-예비 투자설명서 filed from 60 days before the 청약 until
    its last day -- a post-pricing 정정 replaces the first one. Until it is
    filed, the newest 증권신고서 (정정 included), which prints the same
    청약한도 section, usually as pre-pricing ranges. Each brokerage's entry
    says which in ``filing``. Never raises: the daily run must finish
    whatever DART does, and the caller falls back to 38 for every brokerage
    this leaves unread.
    """
    if not key:
        return None, "DART 키 없음"
    start = rec.get("subscription_start")
    if not start:
        return None, "청약일 미정"
    s0 = [int(x) for x in start.split("-")]
    bgn = (datetime.date(*s0) - datetime.timedelta(days=60)).isoformat()
    why = None
    for kind in (PROSPECTUS, REGISTRATION):
        try:
            xml = report_xml(rec.get("ticker") or "", bgn, rec.get("subscription_end") or start, key,
                             name=rec.get("name"), kind=kind)
        except Exception as exc:   # network, zip, JSON: never the URL, which carries the key
            return None, "DART 오류 (%s)" % type(exc).__name__
        if not xml:
            why = why or LAST_ERROR or "투자설명서 없음"
            continue
        try:
            limits = parse_limits(xml)
        except Exception as exc:   # a layout the parser has not met must not stop the run
            why = why or "%s 파싱 오류 (%s)" % (kind, type(exc).__name__)
            continue
        if not limits:
            why = why or "%s에서 청약한도 표를 못 읽음" % kind
            continue
        for v in limits.values():
            v["filing"] = kind
        return limits, None
    return None, why
