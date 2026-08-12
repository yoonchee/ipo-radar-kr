"""Render backtest results as a self-contained HTML report.

Hand-rolled inline SVG -- no charting library, consistent with the stdlib-only
rule in CLAUDE.md. The page is self-contained (no external CSS/JS/fonts) so it
opens straight from disk.

Palette: categorical slots 1-3 for the GO/WATCH/PASS series. The intuitive
green/amber/red triad was rejected on measurement, not taste -- green vs red
scores OKLab dE 4.1 under simulated deuteranopia, far under the 8.0 target,
so red-green colorblind readers could not separate GO from PASS. The slots
below validate all-pairs in both light and dark. Verdict text labels accompany
every mark regardless, so colour never carries meaning alone.
"""

import html
import json
import math
from typing import Dict, List, Optional, Sequence, Tuple

VERDICTS = ("GO", "WATCH", "PASS")
SERIES_VAR = {"GO": "--series-1", "WATCH": "--series-2", "PASS": "--series-3"}


def _esc(s) -> str:
    return html.escape(str(s), quote=True)


def _fmt_pct(v: Optional[float], places: int = 1, sign: bool = True) -> str:
    if v is None:
        return "-"
    return "%+.*f%%" % (places, v) if sign else "%.*f%%" % (places, v)


def _hbar_path(x0: float, y: float, w: float, h: float, r: float = 4.0) -> str:
    """Horizontal bar, square at the baseline, rounded at the data end."""
    r = max(0.0, min(r, abs(w), h / 2.0))
    if w >= 0:
        return ("M%.2f %.2f H%.2f a%.2f %.2f 0 0 1 %.2f %.2f V%.2f "
                "a%.2f %.2f 0 0 1 %.2f %.2f H%.2f Z") % (
            x0, y, x0 + w - r, r, r, r, r, y + h - r, r, r, -r, r, x0)
    w = abs(w)
    return ("M%.2f %.2f H%.2f a%.2f %.2f 0 0 0 %.2f %.2f V%.2f "
            "a%.2f %.2f 0 0 0 %.2f %.2f H%.2f Z") % (
        x0, y, x0 - w + r, r, r, -r, r, y + h - r, r, r, r, r, x0)


def _vbar_path(x: float, y_top: float, w: float, h: float, r: float = 4.0) -> str:
    """Vertical bar, square at the baseline, rounded at the top."""
    r = max(0.0, min(r, w / 2.0, abs(h)))
    return ("M%.2f %.2f V%.2f a%.2f %.2f 0 0 1 %.2f %.2f H%.2f "
            "a%.2f %.2f 0 0 1 %.2f %.2f V%.2f Z") % (
        x, y_top + h, y_top + r, r, r, r, -r, x + w - r, r, r, r, r, y_top + h)


# --------------------------------------------------------------------------
# Charts
# --------------------------------------------------------------------------

def _stat_tiles(agg: Dict) -> str:
    go = agg["by_verdict"].get("GO", {})
    base = agg["baseline"]
    tiles = [
        ("GO win rate", _fmt_pct(go.get("win_rate"), 0, sign=False),
         "%d deals rated GO" % go.get("n", 0)),
        ("GO median return", _fmt_pct(go.get("median")),
         "vs %s subscribing to everything" % _fmt_pct(base.get("median"))),
        ("Deals screened", str(agg["n"]),
         "%s to %s" % (agg["date_min"], agg["date_max"])),
    ]
    out = ['<div class="tiles">']
    for label, value, sub in tiles:
        out.append(
            '<div class="tile"><div class="tile-label">%s</div>'
            '<div class="tile-value">%s</div>'
            '<div class="tile-sub">%s</div></div>' % (_esc(label), _esc(value), _esc(sub))
        )
    out.append("</div>")
    return "".join(out)


def _verdict_chart(agg: Dict) -> str:
    """Median return by verdict -- the headline comparison."""
    rows = [(v, agg["by_verdict"][v]) for v in VERDICTS if v in agg["by_verdict"]]
    if not rows:
        return ""
    W, rowh, pad_l, pad_t = 720, 46, 92, 16
    H = pad_t + rowh * len(rows) + 34
    vmax = max([r[1]["median"] for r in rows] + [agg["baseline"]["median"], 1.0]) * 1.18
    # Right reserve holds the longest value label ("+109.6% · 99% win · n=117"),
    # which is drawn start-anchored past the bar end.
    plot_w = W - pad_l - 210

    def x(v):
        return pad_l + (v / vmax) * plot_w

    svg = ['<svg viewBox="0 0 %d %d" role="img" aria-label="Median opening-auction return by verdict" class="chart">' % (W, H)]
    # baseline reference (subscribe to everything)
    bx = x(agg["baseline"]["median"])
    svg.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" class="ref"/>' % (bx, pad_t - 4, bx, pad_t + rowh * len(rows)))
    svg.append('<text x="%.1f" y="%d" class="ref-label" text-anchor="middle">baseline %s</text>'
               % (bx, pad_t + rowh * len(rows) + 20, _fmt_pct(agg["baseline"]["median"], 0)))

    for i, (v, d) in enumerate(rows):
        y = pad_t + i * rowh + 7
        h = 22
        w = x(d["median"]) - pad_l
        svg.append('<text x="%d" y="%.1f" class="cat-label" text-anchor="end">%s</text>' % (pad_l - 12, y + 16, _esc(v)))
        svg.append(
            '<path d="%s" fill="var(%s)" class="mark" tabindex="0" '
            'data-tip="%s: median %s, win rate %s, n=%d"><title>%s: median %s, win rate %s, n=%d</title></path>'
            % (_hbar_path(pad_l, y, w, h), SERIES_VAR[v],
               _esc(v), _fmt_pct(d["median"]), _fmt_pct(d["win_rate"], 0, sign=False), d["n"],
               _esc(v), _fmt_pct(d["median"]), _fmt_pct(d["win_rate"], 0, sign=False), d["n"]))
        svg.append('<text x="%.1f" y="%.1f" class="val-label">%s <tspan class="val-sub">· %s win · n=%d</tspan></text>'
                   % (x(d["median"]) + 10, y + 16, _fmt_pct(d["median"]),
                      _fmt_pct(d["win_rate"], 0, sign=False), d["n"]))
    svg.append("</svg>")
    return "".join(svg)


def _scatter(rows: List[Dict], agg: Dict) -> str:
    """기관경쟁률 (log x) vs realised opening return (y), coloured by verdict."""
    pts = [r for r in rows if r.get("ratio") and r.get("ret") is not None]
    if not pts:
        return ""
    W, H = 720, 400
    pad_l, pad_r, pad_t, pad_b = 56, 30, 18, 46
    pw, ph = W - pad_l - pad_r, H - pad_t - pad_b

    xs = [max(1.0, p["ratio"]) for p in pts]
    ys = [p["ret"] for p in pts]
    # Floor the low end to a decade for a clean first tick, but keep the high end
    # at the data max (+2% headroom) rather than rounding up: ceiling to the next
    # decade would stretch the axis to 10,000 for a ~3,000 max and strand a tick
    # past the right edge.
    x_lo = math.floor(math.log10(max(1.0, min(xs))))
    x_hi = math.log10(max(xs)) + 0.02
    y_lo = min(min(ys), 0.0)
    y_hi = max(ys)
    y_lo = math.floor(y_lo / 50.0) * 50
    y_hi = math.ceil(y_hi / 50.0) * 50

    def X(v):
        return pad_l + (math.log10(max(1.0, v)) - x_lo) / (x_hi - x_lo) * pw

    def Y(v):
        return pad_t + ph - (v - y_lo) / float(y_hi - y_lo) * ph

    svg = ['<svg viewBox="0 0 %d %d" role="img" aria-label="Institutional competition ratio versus opening-auction return" class="chart">' % (W, H)]
    # y gridlines
    step = 100 if (y_hi - y_lo) > 400 else 50
    v = y_lo
    while v <= y_hi:
        svg.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" class="%s"/>'
                   % (pad_l, Y(v), W - pad_r, Y(v), "zero" if v == 0 else "grid"))
        svg.append('<text x="%d" y="%.1f" class="tick" text-anchor="end">%s</text>' % (pad_l - 8, Y(v) + 4, _fmt_pct(v, 0)))
        v += step
    # x ticks: decades that actually fall inside the plotted range
    for d in range(int(x_lo), int(math.floor(x_hi)) + 1):
        val = 10 ** d
        if math.log10(val) > x_hi:
            break
        svg.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%.1f" class="grid"/>' % (X(val), pad_t, X(val), pad_t + ph))
        svg.append('<text x="%.1f" y="%d" class="tick" text-anchor="middle">%s</text>'
                   % (X(val), pad_t + ph + 20, "{:,}".format(val)))
    svg.append('<text x="%.1f" y="%d" class="axis-title" text-anchor="middle">기관경쟁률 (log scale)</text>'
               % (pad_l + pw / 2.0, H - 8))

    # GO threshold marker
    thr = agg.get("config", {}).get("go_institutional_ratio")
    if thr:
        svg.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%.1f" class="ref"/>' % (X(thr), pad_t, X(thr), pad_t + ph))
        svg.append('<text x="%.1f" y="%d" class="ref-label" text-anchor="middle">GO ≥ %g</text>' % (X(thr), pad_t - 4, thr))

    for p in pts:
        svg.append(
            '<circle cx="%.1f" cy="%.1f" r="4.5" fill="var(%s)" class="dot" tabindex="0" '
            'data-tip="%s · %s · 기관 %.0f:1 · 확약 %s · %s"><title>%s — %s at open, 기관경쟁률 %.0f:1</title></circle>'
            % (X(p["ratio"]), Y(p["ret"]), SERIES_VAR.get(p["verdict"], "--series-1"),
               _esc(p["name"]), _esc(p["verdict"]), p["ratio"],
               _fmt_pct(p.get("lockup"), 1, sign=False), _fmt_pct(p["ret"]),
               _esc(p["name"]), _fmt_pct(p["ret"]), p["ratio"]))
    svg.append("</svg>")
    return "".join(svg)


def _bucket_chart(buckets: Sequence[Dict], title_id: str, value_key: str, label: str) -> str:
    """Single-series vertical bars over ordered buckets.

    One hue on purpose: bucket identity is carried by x-position, so colouring
    each bar differently would encode nothing.
    """
    bs = [b for b in buckets if b.get("n")]
    if not bs:
        return ""
    W, H = 720, 260
    pad_l, pad_r, pad_t, pad_b = 46, 16, 22, 54
    pw, ph = W - pad_l - pad_r, H - pad_t - pad_b
    vmax = max(b[value_key] for b in bs)
    vmax = max(vmax * 1.15, 1.0)
    slot = pw / float(len(bs))
    bw = min(56.0, slot - 14)

    svg = ['<svg viewBox="0 0 %d %d" role="img" aria-label="%s" class="chart">' % (W, H, _esc(label))]
    for frac in (0.0, 0.5, 1.0):
        yy = pad_t + ph - frac * ph
        svg.append('<line x1="%d" y1="%.1f" x2="%d" y2="%.1f" class="grid"/>' % (pad_l, yy, W - pad_r, yy))
        svg.append('<text x="%d" y="%.1f" class="tick" text-anchor="end">%s</text>'
                   % (pad_l - 8, yy + 4, ("%.0f%%" % (frac * vmax))))
    for i, b in enumerate(bs):
        cx = pad_l + i * slot + slot / 2.0
        h = (b[value_key] / vmax) * ph
        svg.append(
            '<path d="%s" fill="var(--series-1)" class="mark" tabindex="0" data-tip="%s: %s, n=%d, median %s">'
            '<title>%s: %s over %d deals</title></path>'
            % (_vbar_path(cx - bw / 2.0, pad_t + ph - h, bw, h), _esc(b["label"]),
               _fmt_pct(b[value_key], 0, sign=False), b["n"], _fmt_pct(b.get("median")),
               _esc(b["label"]), _fmt_pct(b[value_key], 0, sign=False), b["n"]))
        svg.append('<text x="%.1f" y="%.1f" class="val-label-sm" text-anchor="middle">%s</text>'
                   % (cx, pad_t + ph - h - 7, _fmt_pct(b[value_key], 0, sign=False)))
        svg.append('<text x="%.1f" y="%d" class="tick" text-anchor="middle">%s</text>'
                   % (cx, pad_t + ph + 20, _esc(b["label"])))
        svg.append('<text x="%.1f" y="%d" class="tick-sub" text-anchor="middle">n=%d</text>'
                   % (cx, pad_t + ph + 36, b["n"]))
    svg.append("</svg>")
    return "".join(svg)


def _legend() -> str:
    items = "".join(
        '<span class="lg"><span class="sw" style="background:var(%s)"></span>%s</span>' % (SERIES_VAR[v], v)
        for v in VERDICTS
    )
    return '<div class="legend">%s</div>' % items


def _table(rows: List[Dict]) -> str:
    rows = sorted(rows, key=lambda r: r.get("listing_date") or "", reverse=True)
    head = ("종목", "상장일", "판정", "기관경쟁률", "의무보유확약", "확정공모가", "밴드", "시초/공모")
    out = ['<div class="tablewrap"><table><thead><tr>']
    out += ['<th>%s</th>' % _esc(h) for h in head]
    out.append("</tr></thead><tbody>")
    for r in rows:
        band = "%s~%s" % ("{:,}".format(r["band_low"]) if r.get("band_low") else "-",
                          "{:,}".format(r["band_high"]) if r.get("band_high") else "-")
        out.append(
            "<tr><td>%s</td><td class=\"num\">%s</td>"
            "<td><span class=\"pill\" style=\"background:var(%s)\"></span>%s</td>"
            "<td class=\"num\">%.2f:1</td><td class=\"num\">%s</td><td class=\"num\">%s</td>"
            "<td class=\"num\">%s</td><td class=\"num %s\">%s</td></tr>"
            % (_esc(r["name"]), _esc(r.get("listing_date") or "-"),
               SERIES_VAR.get(r["verdict"], "--series-1"), _esc(r["verdict"]),
               r["ratio"], _fmt_pct(r.get("lockup"), 2, sign=False),
               "{:,}".format(r["final_price"]) if r.get("final_price") else "-",
               band, "pos" if r["ret"] > 0 else "neg", _fmt_pct(r["ret"])))
    out.append("</tbody></table></div>")
    return "".join(out)


# --------------------------------------------------------------------------

_CSS = """
:root{color-scheme:light;--page:#f9f9f7;--surface-1:#fcfcfb;--text-primary:#0b0b0b;
--text-secondary:#52514e;--muted:#898781;--grid:#e1e0d9;--baseline:#c3c2b7;
--series-1:#2a78d6;--series-2:#eb6834;--series-3:#1baf7a;--border:rgba(11,11,11,.10);--pos:#006300;--neg:#d03b3b}
@media (prefers-color-scheme:dark){:root:not([data-theme="light"]){color-scheme:dark;
--page:#0d0d0d;--surface-1:#1a1a19;--text-primary:#fff;--text-secondary:#c3c2b7;--muted:#898781;
--grid:#2c2c2a;--baseline:#383835;--series-1:#3987e5;--series-2:#d95926;--series-3:#199e70;
--border:rgba(255,255,255,.10);--pos:#0ca30c;--neg:#e66767}}
:root[data-theme="dark"]{color-scheme:dark;--page:#0d0d0d;--surface-1:#1a1a19;--text-primary:#fff;
--text-secondary:#c3c2b7;--muted:#898781;--grid:#2c2c2a;--baseline:#383835;
--series-1:#3987e5;--series-2:#d95926;--series-3:#199e70;--border:rgba(255,255,255,.10);--pos:#0ca30c;--neg:#e66767}
*{box-sizing:border-box}
body{margin:0;background:var(--page);color:var(--text-primary);
font-family:system-ui,-apple-system,"Segoe UI",sans-serif;line-height:1.5;padding:32px 20px 64px}
.wrap{max-width:860px;margin:0 auto}
h1{font-size:1.6rem;margin:0 0 4px;letter-spacing:-.02em}
h2{font-size:1.05rem;margin:38px 0 6px;letter-spacing:-.01em}
.sub{color:var(--text-secondary);margin:0 0 8px;font-size:.9rem}
.note{color:var(--text-secondary);font-size:.85rem;margin:6px 0 0}
.disclaimer{border:1px solid var(--border);border-left:3px solid var(--series-2);background:var(--surface-1);
padding:12px 14px;border-radius:8px;font-size:.85rem;color:var(--text-secondary);margin:16px 0 8px}
.card{background:var(--surface-1);border:1px solid var(--border);border-radius:10px;padding:14px;margin-top:10px;overflow-x:auto}
.chart{display:block;width:100%;min-width:520px;height:auto}
.tiles{display:flex;gap:10px;flex-wrap:wrap;margin-top:14px}
.tile{flex:1 1 180px;background:var(--surface-1);border:1px solid var(--border);border-radius:10px;padding:14px}
.tile-label{font-size:.78rem;color:var(--text-secondary);text-transform:uppercase;letter-spacing:.04em}
.tile-value{font-size:1.9rem;font-weight:600;letter-spacing:-.02em;margin:2px 0}
.tile-sub{font-size:.8rem;color:var(--muted)}
.grid{stroke:var(--grid);stroke-width:1}
.zero{stroke:var(--baseline);stroke-width:1.5}
.ref{stroke:var(--muted);stroke-width:1.5;stroke-dasharray:4 3}
.ref-label{fill:var(--muted);font-size:11px}
.tick{fill:var(--muted);font-size:11px;font-variant-numeric:tabular-nums}
.tick-sub{fill:var(--muted);font-size:10px;font-variant-numeric:tabular-nums}
.axis-title{fill:var(--text-secondary);font-size:11px}
.cat-label{fill:var(--text-primary);font-size:13px;font-weight:600}
.val-label{fill:var(--text-primary);font-size:13px;font-weight:600;font-variant-numeric:tabular-nums}
.val-label-sm{fill:var(--text-primary);font-size:11px;font-weight:600;font-variant-numeric:tabular-nums}
.val-sub{fill:var(--text-secondary);font-weight:400}
.dot{stroke:var(--surface-1);stroke-width:2;opacity:.85}
.dot:hover,.dot:focus{opacity:1;stroke-width:2.5;outline:none}
.mark:hover,.mark:focus{opacity:.85;outline:none}
.legend{display:flex;gap:14px;flex-wrap:wrap;margin:10px 0 0;font-size:.85rem;color:var(--text-secondary)}
.lg{display:inline-flex;align-items:center;gap:6px}
.sw{width:11px;height:11px;border-radius:3px;display:inline-block}
.tablewrap{overflow-x:auto;border:1px solid var(--border);border-radius:10px;background:var(--surface-1);margin-top:10px}
table{border-collapse:collapse;width:100%;font-size:.84rem;min-width:660px}
th{text-align:left;font-weight:600;color:var(--text-secondary);padding:9px 10px;border-bottom:1px solid var(--border);
position:sticky;top:0;background:var(--surface-1)}
td{padding:8px 10px;border-bottom:1px solid var(--border)}
tr:last-child td{border-bottom:0}
.num{text-align:right;font-variant-numeric:tabular-nums}
.pos{color:var(--pos)}.neg{color:var(--neg)}
.pill{width:9px;height:9px;border-radius:3px;display:inline-block;margin-right:6px;vertical-align:middle}
#tip{position:fixed;pointer-events:none;opacity:0;transition:opacity .1s;background:var(--text-primary);
color:var(--page);padding:6px 9px;border-radius:6px;font-size:.78rem;z-index:9;max-width:280px}
footer{margin-top:40px;color:var(--muted);font-size:.8rem;border-top:1px solid var(--border);padding-top:14px}
a{color:var(--series-1)}
"""

_JS = """
(function(){var t=document.getElementById('tip');
function show(e){var el=e.target.closest('[data-tip]');if(!el)return;
t.textContent=el.getAttribute('data-tip');t.style.opacity='1';move(e);}
function move(e){var x=(e.clientX||0)+14,y=(e.clientY||0)+14;
if(x+300>window.innerWidth)x=window.innerWidth-300;t.style.left=x+'px';t.style.top=y+'px';}
function hide(){t.style.opacity='0';}
document.addEventListener('mouseover',show);document.addEventListener('mousemove',function(e){
if(t.style.opacity==='1')move(e);});document.addEventListener('mouseout',hide);
document.addEventListener('focusin',function(e){var el=e.target.closest('[data-tip]');if(!el)return;
var r=el.getBoundingClientRect();t.textContent=el.getAttribute('data-tip');t.style.opacity='1';
t.style.left=r.left+'px';t.style.top=(r.bottom+8)+'px';});
document.addEventListener('focusout',hide);})();
"""


def render(payload: Dict) -> str:
    agg, rows = payload["aggregates"], payload["deals"]
    cfg = payload.get("config", {})
    agg = dict(agg)
    agg["config"] = cfg

    parts = ['<title>공모주 백테스트 — GO/WATCH/PASS</title>',
             "<style>%s</style>" % _CSS,
             '<div class="wrap">',
             "<h1>공모주 백테스트</h1>",
             '<p class="sub">Screen verdicts against realised <strong>시초/공모</strong> returns — '
             'subscribe at the offer price, sell into the opening auction. '
             '%d non-SPAC deals, %s → %s. Generated %s.</p>'
             % (agg["n"], _esc(agg["date_min"]), _esc(agg["date_max"]), _esc(payload["generated_at"])),
             '<div class="disclaimer"><strong>Not investment advice.</strong> Historical results from a '
             'specific market regime (Korea widened the listing-day opening range to 60–400% of 공모가 in '
             'June 2023 — every deal here listed under those rules). Past results do not predict future ones. '
             'Returns are before fees and tax and assume you actually sell at the open. There is no allocation '
             'modelling: 배정 is pro-rata, so a high-scoring deal where you were filled for three shares can be '
             'worth less in cash than a mediocre one where you were not.</div>',
             _stat_tiles(agg),

             "<h2>Return by verdict</h2>",
             '<p class="note">Median opening-auction return for each verdict, against the baseline of '
             'subscribing to every deal.</p>',
             '<div class="card">%s</div>' % _verdict_chart(agg),
             _legend(),

             "<h2>기관경쟁률 vs realised return</h2>",
             '<p class="note">Every deal. The dashed line is the GO threshold in '
             '<code>config.json</code>. Hover or tab to a point for detail.</p>',
             '<div class="card">%s</div>' % _scatter(rows, agg),
             _legend(),

             "<h2>Win rate by 기관경쟁률</h2>",
             '<p class="note">Share of deals that opened above the offer price. One hue: the bucket is '
             'identified by position, so colour would encode nothing.</p>',
             '<div class="card">%s</div>' % _bucket_chart(
                 agg["by_ratio_bucket"], "ratio", "win_rate", "Win rate by institutional competition ratio"),

             "<h2>Win rate by 확정공모가 vs 밴드</h2>",
             '<p class="note">Where the final price landed relative to the indicative band. Pricing at the '
             'band bottom is the strongest negative signal in the sample.</p>',
             '<div class="card">%s</div>' % _bucket_chart(
                 agg["by_band_position"], "band", "win_rate", "Win rate by final price versus band"),

             "<h2>Win rate by 의무보유확약</h2>",
             '<div class="card">%s</div>' % _bucket_chart(
                 agg["by_lockup_bucket"], "lockup", "win_rate", "Win rate by lockup percentage"),

             "<h2>All deals</h2>",
             '<p class="note">%d deals, newest first.</p>' % len(rows),
             _table(rows),

             "<footer>Data from <a href=\"https://www.38.co.kr/html/fund/\">38커뮤니케이션</a>. "
             "Generated by <code>backtest.py</code> — thresholds in <code>config.json</code>: "
             "기관경쟁률 GO ≥ %s, WATCH ≥ %s.</footer>"
             % (_esc(cfg.get("go_institutional_ratio", "?")), _esc(cfg.get("watch_institutional_ratio", "?"))),
             "</div>",
             '<div id="tip" role="status"></div>',
             "<script>%s</script>" % _JS]
    return "\n".join(parts)
