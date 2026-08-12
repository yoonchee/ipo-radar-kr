"""Render the net-won backtest as a self-contained HTML report.

Shares the palette, CSS and SVG primitives with render_backtest so the two
reports read as one system.
"""

import math
from typing import Dict, List, Optional

from .render_backtest import (SERIES_VAR, VERDICTS, _CSS, _JS, _esc, _fmt_pct,
                              _hbar_path, _vbar_path)


def _won(v: Optional[float], sign: bool = False) -> str:
    if v is None:
        return "-"
    return ("{:+,.0f}" if sign else "{:,.0f}").format(v)


def _man(v: Optional[float], sign: bool = True) -> str:
    """Won in 만원 units -- the scale a person actually reasons in."""
    if v is None:
        return "-"
    return ("{:+,.0f}만" if sign else "{:,.0f}만").format(v / 10000.0)


def _tiles(agg: Dict) -> str:
    go = agg["by_verdict"].get("GO", {})
    allv = agg["all"]
    tiles = [
        ("GO 순손익", _man(go.get("net")),
         "%d건 · 건당 %s" % (go.get("n", 0), _man(go.get("net", 0) / max(go.get("n", 1), 1)))),
        ("GO 초과수익률", "%+.2f%%" % go.get("excess_return", 0),
         "3%%/yr 대비 · 자본 %s 기준" % _man(go.get("capital_years", 0), sign=False)),
        ("전 종목 청약 시", _man(allv.get("net")),
         "%d건 · 초과수익률 %+.2f%%" % (allv.get("n", 0), allv.get("excess_return", 0))),
    ]
    out = ['<div class="tiles">']
    for label, value, sub in tiles:
        cls = " neg" if value.startswith("-") else ""
        out.append('<div class="tile"><div class="tile-label">%s</div>'
                   '<div class="tile-value%s">%s</div><div class="tile-sub">%s</div></div>'
                   % (_esc(label), cls, _esc(value), _esc(sub)))
    out.append("</div>")
    return "".join(out)


def _diverging_bars(agg: Dict, key: str, fmt, title: str) -> str:
    """Zero-anchored horizontal bars; values may be negative."""
    rows = [(v, agg["by_verdict"][v]) for v in VERDICTS if v in agg["by_verdict"]]
    rows.append(("전 종목", agg["all"]))
    if not rows:
        return ""
    W, rowh, pad_l, pad_t = 720, 46, 92, 18
    H = pad_t + rowh * len(rows) + 26
    vals = [d[key] for _, d in rows]
    lo, hi = min(min(vals), 0.0), max(max(vals), 0.0)
    span = (hi - lo) or 1.0
    plot_l, plot_w = pad_l, W - pad_l - 150
    zero_x = plot_l + (0.0 - lo) / span * plot_w

    svg = ['<svg viewBox="0 0 %d %d" role="img" aria-label="%s" class="chart">' % (W, H, _esc(title))]
    svg.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%.1f" class="zero"/>'
               % (zero_x, pad_t - 4, zero_x, pad_t + rowh * len(rows)))
    for i, (label, d) in enumerate(rows):
        y = pad_t + i * rowh + 7
        val = d[key]
        x_end = plot_l + (val - lo) / span * plot_w
        w = x_end - zero_x
        var = SERIES_VAR.get(label, "--muted-series")
        svg.append('<text x="%d" y="%.1f" class="cat-label" text-anchor="end">%s</text>'
                   % (pad_l - 12, y + 16, _esc(label)))
        svg.append('<path d="%s" fill="var(%s)" class="mark" tabindex="0" data-tip="%s: %s (n=%d)">'
                   '<title>%s: %s</title></path>'
                   % (_hbar_path(zero_x, y, w, 22), var, _esc(label), _esc(fmt(val)), d["n"],
                      _esc(label), _esc(fmt(val))))
        # Value labels sit in a fixed right-hand column rather than chasing the
        # bar end: a negative bar's label would otherwise run off the left edge.
        svg.append('<text x="%.1f" y="%.1f" class="val-label %s">%s'
                   '<tspan class="val-sub"> · n=%d</tspan></text>'
                   % (W - 142, y + 16, "neg" if val < 0 else "pos", _esc(fmt(val)), d["n"]))
    svg.append("</svg>")
    return "".join(svg)


def _scatter_pct_vs_net(deals: List[Dict]) -> str:
    """시초/공모 % against realised net won -- shows where the percentage lies."""
    pts = [d for d in deals if d.get("ret") is not None]
    if not pts:
        return ""
    W, H = 720, 420
    pad_l, pad_r, pad_t, pad_b = 76, 24, 20, 50
    pw, ph = W - pad_l - pad_r, H - pad_t - pad_b
    xs = [d["ret"] for d in pts]
    x_lo, x_hi = math.floor(min(xs) / 50.0) * 50, math.ceil(max(xs) / 50.0) * 50

    # Net won spans orders of magnitude with a long negative tail, so a linear
    # axis would collapse everything near zero into one line. Symmetric log
    # keeps both the -10M outliers and the +-50k cluster legible.
    def sym(v):
        return math.copysign(math.log10(1 + abs(v) / 10000.0), v)

    ys = [sym(d["net"]) for d in pts]
    y_lo, y_hi = min(min(ys), 0), max(max(ys), 0)
    pad = (y_hi - y_lo) * 0.08 or 1
    y_lo, y_hi = y_lo - pad, y_hi + pad

    def X(v):
        return pad_l + (v - x_lo) / float(x_hi - x_lo) * pw

    def Y(v):
        return pad_t + ph - (sym(v) - y_lo) / (y_hi - y_lo) * ph

    svg = ['<svg viewBox="0 0 %d %d" role="img" aria-label="Opening return versus realised net won" class="chart">' % (W, H)]
    for tick in (-10000000, -1000000, -100000, 0, 100000, 1000000):
        if not (y_lo <= sym(tick) <= y_hi):
            continue
        yy = Y(tick)
        svg.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" class="%s"/>'
                   % (pad_l, yy, W - pad_r, yy, "zero" if tick == 0 else "grid"))
        svg.append('<text x="%d" y="%.1f" class="tick" text-anchor="end">%s</text>'
                   % (pad_l - 8, yy + 4, "0" if tick == 0 else _man(tick)))
    for v in range(int(x_lo), int(x_hi) + 1, 100):
        svg.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%.1f" class="grid"/>' % (X(v), pad_t, X(v), pad_t + ph))
        svg.append('<text x="%.1f" y="%d" class="tick" text-anchor="middle">%s</text>'
                   % (X(v), pad_t + ph + 20, _fmt_pct(v, 0)))
    svg.append('<text x="%.1f" y="%d" class="axis-title" text-anchor="middle">시초/공모 (%%)</text>'
               % (pad_l + pw / 2.0, H - 8))
    svg.append('<text x="14" y="%.1f" class="axis-title" text-anchor="middle" transform="rotate(-90 14 %.1f)">순손익 (만원, symlog)</text>'
               % (pad_t + ph / 2.0, pad_t + ph / 2.0))
    for d in pts:
        svg.append('<circle cx="%.1f" cy="%.1f" r="4.5" fill="var(%s)" class="dot" tabindex="0" '
                   'data-tip="%s · %s · 시초/공모 %s · 배정 %d주 · 투입 %s · 순손익 %s">'
                   '<title>%s: net %s</title></circle>'
                   % (X(d["ret"]), Y(d["net"]), SERIES_VAR.get(d["verdict"], "--series-1"),
                      _esc(d["name"]), _esc(d["verdict"]), _fmt_pct(d["ret"], 0), d["shares"],
                      _man(d["capital"], sign=False), _man(d["net"]),
                      _esc(d["name"]), _man(d["net"])))
    svg.append("</svg>")
    return "".join(svg)


def _alloc_chart(agg: Dict) -> str:
    """Average shares allocated per verdict -- the mechanism behind the losses."""
    rows = [(v, agg["by_verdict"][v]) for v in VERDICTS if v in agg["by_verdict"]]
    if not rows:
        return ""
    W, H = 720, 250
    pad_l, pad_r, pad_t, pad_b = 56, 16, 26, 54
    pw, ph = W - pad_l - pad_r, H - pad_t - pad_b
    vmax = max(d["avg_shares"] for _, d in rows) * 1.18 or 1
    slot = pw / float(len(rows))
    bw = min(64.0, slot - 20)
    svg = ['<svg viewBox="0 0 %d %d" role="img" aria-label="Average shares allocated by verdict" class="chart">' % (W, H)]
    for frac in (0.0, 0.5, 1.0):
        yy = pad_t + ph - frac * ph
        svg.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" class="grid"/>' % (pad_l, yy, W - pad_r, yy))
        svg.append('<text x="%d" y="%.1f" class="tick" text-anchor="end">%s</text>'
                   % (pad_l - 8, yy + 4, "{:,.0f}".format(frac * vmax)))
    for i, (v, d) in enumerate(rows):
        cx = pad_l + i * slot + slot / 2.0
        h = (d["avg_shares"] / vmax) * ph
        svg.append('<path d="%s" fill="var(%s)" class="mark" tabindex="0" data-tip="%s: 평균 %.1f주 배정, 건당 순손익 %s">'
                   '<title>%s: %.1f shares</title></path>'
                   % (_vbar_path(cx - bw / 2.0, pad_t + ph - h, bw, h), SERIES_VAR[v],
                      _esc(v), d["avg_shares"], _man(d["net"] / max(d["n"], 1)), _esc(v), d["avg_shares"]))
        svg.append('<text x="%.1f" y="%.1f" class="val-label-sm" text-anchor="middle">%.0f주</text>'
                   % (cx, pad_t + ph - h - 7, d["avg_shares"]))
        svg.append('<text x="%.1f" y="%d" class="tick" text-anchor="middle">%s</text>' % (cx, pad_t + ph + 20, _esc(v)))
        svg.append('<text x="%.1f" y="%d" class="tick-sub" text-anchor="middle">건당 %s</text>'
                   % (cx, pad_t + ph + 36, _man(d["net"] / max(d["n"], 1))))
    svg.append("</svg>")
    return "".join(svg)


def _legend() -> str:
    return '<div class="legend">%s</div>' % "".join(
        '<span class="lg"><span class="sw" style="background:var(%s)"></span>%s</span>' % (SERIES_VAR[v], v)
        for v in VERDICTS)


def _table(deals: List[Dict]) -> str:
    deals = sorted(deals, key=lambda d: d["net"])
    head = ("종목", "상장일", "판정", "비례경쟁률", "청약한도", "투입증거금", "배정", "시초/공모", "매매이익", "기회비용", "순손익")
    out = ['<div class="tablewrap"><table><thead><tr>']
    out += ["<th>%s</th>" % _esc(h) for h in head]
    out.append("</tr></thead><tbody>")
    for d in deals:
        out.append(
            "<tr><td>%s</td><td class=\"num nowrap\">%s</td><td class=\"nowrap\">%s</td>"
            "<td class=\"num nowrap\">%s</td><td class=\"num nowrap\">%s</td><td class=\"num nowrap\">%s</td>"
            "<td class=\"num nowrap\">%d주</td><td class=\"num nowrap %s\">%s</td>"
            "<td class=\"num nowrap\">%s</td><td class=\"num nowrap\">%s</td><td class=\"num nowrap %s\">%s</td></tr>"
            % (_esc(d["name"]), _esc(d["listing_date"]), _esc(d["verdict"]),
               "{:,.0f}:1".format(d["prop_ratio"]), "{:,}".format(d["limit"]),
               _man(d["capital"], sign=False), d["shares"],
               "pos" if d["ret"] > 0 else "neg", _fmt_pct(d["ret"], 0),
               _man(d["gain"]), _man(-d["cost"]),
               "pos" if d["net"] > 0 else "neg", _man(d["net"])))
    out.append("</tbody></table></div>")
    return "".join(out)


_EXTRA_CSS = """
.tile-value.neg{color:var(--neg)}
.val-label.neg{fill:var(--neg)}.val-label.pos{fill:var(--pos)}
:root{--muted-series:#898781}
.keyfind{border:1px solid var(--border);border-left:3px solid var(--series-1);background:var(--surface-1);
padding:12px 14px;border-radius:8px;margin:14px 0;font-size:.9rem}
.keyfind strong{color:var(--text-primary)}
"""


def render(payload: Dict) -> str:
    agg, deals, p = payload["aggregates"], payload["deals"], payload["params"]
    go = agg["by_verdict"].get("GO", {})
    allv = agg["all"]

    parts = [
        '<meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width,initial-scale=1">',
        "<title>공모주 실질손익 백테스트</title>",
        "<style>%s%s</style>" % (_CSS, _EXTRA_CSS),
        '<div class="wrap">',
        "<h1>공모주 실질손익 백테스트</h1>",
        '<p class="sub">시초/공모 percentages against <strong>won actually earned</strong> — '
        '최대 청약한도 subscription, 증거금 tied up from 청약 종료일 to 환불일 at %.1f%%/yr opportunity cost. '
        '%d deals, %s → %s. Generated %s.</p>'
        % (p["rate"], agg["all"]["n"], _esc(agg["date_min"]), _esc(agg["date_max"]),
           _esc(payload["generated_at"])),

        '<div class="keyfind">The percentage view says %d%% of deals were profitable. '
        'In cash, subscribing to <strong>every</strong> deal at the maximum limit '
        '<strong class="neg">loses %s</strong> — worse than leaving the money alone. '
        'The screen is what turns it positive: GO alone earns <strong class="pos">%s</strong>.</div>'
        % (round(agg.get("pct_view_win_rate", 0)), _man(allv["net"]).lstrip("+-"), _man(go.get("net"))),

        _tiles(agg),

        "<h2>순손익 by verdict</h2>",
        '<p class="note">Won earned after subtracting the interest forgone on the 증거금. '
        'PASS is not merely weaker — it is a large, consistent loss.</p>',
        '<div class="card">%s</div>' % _diverging_bars(agg, "net", _man, "Net won by verdict"),

        "<h2>초과수익률</h2>",
        '<p class="note">Return per won-year of capital actually committed, <em>on top of</em> the '
        '%.1f%%/yr the money would have earned sitting still. Zero means the effort was pointless.</p>' % p["rate"],
        '<div class="card">%s</div>' % _diverging_bars(
            agg, "excess_return", lambda v: "%+.2f%%" % v, "Excess return by verdict"),

        "<h2>왜: 나쁜 딜일수록 많이 배정된다</h2>",
        '<p class="note">Low institutional demand means low retail competition means a large 비례 allocation. '
        'You get filled precisely on the deals you did not want.</p>',
        '<div class="card">%s</div>' % _alloc_chart(agg),

        "<h2>시초/공모 vs 실제 순손익</h2>",
        '<p class="note">Where the percentage lies. Points high on the x-axis but below the zero line '
        'rose sharply and still lost money — the allocation was too small to cover the interest forgone. '
        'Vertical scale is symmetric-log; hover any point for the detail.</p>',
        '<div class="card">%s</div>' % _scatter_pct_vs_net(deals),
        _legend(),

        "<h2>전체 내역</h2>",
        '<p class="note">%d deals, worst net first.</p>' % len(deals),
        _table(deals),

        "<footer>배정 = 비례 only: floor(청약한도 / 비례경쟁률). 균등 is excluded because 청약건수 "
        "(the number of retail subscribers) is not published anywhere — the 50/50 pool split is derivable "
        "from 1 − 통합률/비례률 and holds for %d of %d deals, but pool size alone cannot give shares per "
        "person. Every figure here is therefore a <strong>floor</strong>; plausible 균등 adds roughly "
        "10–15%% to the GO net. Before fees and tax. "
        "Data from <a href=\"https://www.38.co.kr/html/fund/\">38커뮤니케이션</a>.</footer>"
        % (agg.get("equal_split_5050", 0), agg.get("equal_split_checked", 0)),
        "</div>",
        '<div id="tip" role="status"></div>',
        "<script>%s</script>" % _JS,
    ]
    return "\n".join(parts)
