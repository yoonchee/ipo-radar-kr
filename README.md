# 공모주 레이더 (ipo-radar-kr)

Daily screen for Korean IPO subscriptions (공모청약). It checks which offerings are
open or coming up, pulls the institutional demand-forecast result (수요예측 →
기관경쟁률, 의무보유확약), scores each one, and raises a macOS notification when
something clears the bar.

**Decision support only.** It never logs into a brokerage, moves funds, or places an
order. You decide how much to subscribe for and where.

> **Not investment advice.** This is a personal tool published as-is. The backtest
> numbers below are historical and describe a specific market regime that may not
> persist; past results do not predict future ones. Nothing here is a recommendation
> to buy or sell any security, and 공모주 subscriptions can and do lose money. If you
> use it, you own the outcome — read [the caveats](#read-this-before-trusting-those-numbers)
> before trusting any figure in this README.

## Why scraping

There is no API — official or commercial — that exposes 기관경쟁률.

| Source | 기관경쟁률 | Notes |
|---|:--:|---|
| DART OpenDART `증권신고서(지분증권)` (apiId 2020054) | ✗ | Official and free. Has 공모가격, 청약기일, 인수인, 일반청약자 배정 — but 수요예측 결과 is not in the response. |
| KSD SEIBro / 공공데이터포털 | ✗ | 주식·예탁 statistics only; no IPO demand-forecast endpoint. |
| KIS Developers, 키움 OpenAPI+ (broker APIs) | ✗ | Quotes, orders, balances. No Korean broker exposes IPO subscription over API. |
| **38커뮤니케이션 (38.co.kr)** | **✓** | The de-facto retail source. Server-rendered EUC-KR HTML, `robots.txt` is `Disallow:` (empty). |

So 38.co.kr it is. The scraper is stdlib-only (no `requests`/`bs4`) so the scheduled
job cannot break from a virtualenv or dependency drift.

## Quick start

```bash
python3 run.py --all --no-notify   # score everything upcoming, stay quiet
cat state/latest.md                # read the report
python3 run.py                     # normal run, notifies on new GO
```

Requires nothing but the system Python 3 that ships with macOS.

### Scheduling

The plist ships as a template with the install path as a placeholder, so substitute
your checkout directory when copying it:

```bash
sed "s|__RADAR_DIR__|$PWD|g" launchd/com.ipo-radar.plist.template \
  > ~/Library/LaunchAgents/com.ipo-radar.plist
launchctl load ~/Library/LaunchAgents/com.ipo-radar.plist
```

Runs at 09:00 and 18:30. 확정공모가 and 기관경쟁률 are published the evening after
수요예측 closes — a few business days before 청약 opens — so the evening run is the
one that usually fires first.

To stop: `launchctl unload ~/Library/LaunchAgents/com.ipo-radar.plist`

### Clickable notifications (optional)

By default the banner is posted with `osascript`, which attributes it to Script
Editor. Those banners **carry no click action** — clicking one just activates Script
Editor, which opens its iCloud document folder. That is an API limitation, not a
setting, so the built-in path never implies the banner is actionable.

Installing `terminal-notifier` gives banners a real click target:

```bash
brew install terminal-notifier
```

With it, clicking a GO alert opens that deal's 38.co.kr page. It is entirely optional
— the screen detects it at runtime and falls back to plain banners when absent.

`notify.py` looks for it at the Homebrew prefixes directly rather than trusting
`PATH`: launchd starts jobs with a minimal `PATH` that excludes both prefixes, so a
plain `shutil.which()` would find it interactively and silently miss it in the
scheduled run — the run that matters.

## How it scores

Three signals, all knowable *before* the subscription window opens:

1. **기관경쟁률** — how hard institutions competed in 수요예측.
2. **확정공모가 vs 희망공모가밴드** — pricing at or above the band top means demand
   overwhelmed the range.
3. **의무보유확약** — the share of institutional allocation voluntarily locked up,
   weighted toward 3/6-month tenors. 15일 확약 unlocks before you'd care; 3개월+
   keeps that stock off the tape on listing day.

Verdicts: **GO** (score ≥ 6, no blockers) · **WATCH** (≥ 3) · **PASS** · **PENDING**
(수요예측 not published yet) · **EXCLUDED** (스팩/코넥스).

**Only a new GO raises a notification.** WATCH appears in the report but never
interrupts — see [실질손익](#실질손익-net-won) for why.

A deal is blocked outright — never GO — if 기관경쟁률 is under 300, or if the price
failed to reach the band top.

Thresholds live in `config.json`.

## Calibration

`backtest.py` joins 수요예측결과 (knowable before subscribing) against 신규상장
(what actually happened), measuring **시초/공모** — subscribe at the offer price,
sell into the opening auction.

```bash
python3 backtest.py --pages 18            # writes JSON + HTML, prints the summary
python3 backtest.py --pages 18 --no-write  # stdout only
open state/backtest/latest.html            # the visual report
```

It persists results to `state/backtest/`, dated plus a `latest` copy of each:

| File | What it's for |
|---|---|
| `latest.json` | Full dataset — every deal with its verdict, score, and the reasons behind it, plus all aggregates and the config used. Machine-readable, for re-analysis without re-scraping. |
| `latest.html` | Self-contained visual report — verdict comparison, a 기관경쟁률-vs-return scatter of every deal, win rate by bucket, and the full sortable table. Opens straight from disk, no server or network needed. |

`state/` is gitignored, so these are local artifacts. Commit one deliberately if you
want a versioned record of what justified a threshold change.

Over **243 non-SPAC deals, 2023-03 → 2026-08**:

| Verdict | n | win rate | mean | median |
|---|---:|---:|---:|---:|
| GO | 117 | 99% | +131% | +110% |
| WATCH | 70 | 80% | +59% | +38% |
| PASS | 56 | 64% | +23% | +8% |
| *(subscribe to everything)* | 243 | 86% | +85% | +66% |

Each signal is monotonic on its own:

| 기관경쟁률 | n | win rate | median |
|---|---:|---:|---:|
| 0–100 | 18 | 72% | +9% |
| 100–300 | 28 | 64% | +8% |
| 300–600 | 25 | 72% | +30% |
| 600–800 | 28 | 96% | +73% |
| 800–1200 | 96 | 89% | +81% |
| 1200+ | 48 | 98% | +101% |

| 확정공모가 vs 밴드 | n | win rate | median |
|---|---:|---:|---:|
| above band | 88 | 90% | +75% |
| at band top | 113 | 89% | +78% |
| within band | 24 | 79% | +15% |
| **at band bottom** | 18 | **50%** | **+1%** |

`go_institutional_ratio` is set to **600** rather than 800: at 600 the screen takes
117 deals at a 99% win rate instead of 94 at 100%, with near-identical median return.
Since the binding constraint is 청약한도 per deal rather than the number of deals,
more shots on goal is the better trade.

### Read this before trusting those numbers

- **Regime dependence.** In June 2023 Korea widened the listing-day opening range to
  60–400% of 공모가. Every deal in this sample listed under those rules, and the
  +85% baseline reflects that. If the rules tighten, the base rate moves and the
  thresholds should be re-derived.
- **A high baseline flatters everything.** 86% of *all* deals were profitable at the
  open. The screen's value is avoiding the bad tail, not finding rare winners.
- **These percentages are not money.** 배정 is pro-rata, so a +150% deal where you were
  filled for three shares can be worth less in cash than a mediocre one where you were
  filled heavily — and the 증거금 earns nothing while it is tied up. See
  [실질손익](#실질손익-net-won), which models the actual cash outcome and reverses the
  headline: subscribing to everything *loses* money.
- Returns are before fees and tax, and assume you actually sell at the open.

## 실질손익 (net won)

시초/공모 measures the price move, not the trade. Subscribing means committing 증거금
(50% of 청약한도 × 확정공모가) from the 청약일 to the 환불일, earning nothing meanwhile —
and on a hot deal an 억-won commitment buys a handful of shares.

```bash
python3 net_returns.py --rate 3.0                  # writes net-latest.{json,html}
python3 net_returns.py --rate 3.0 --subscribers 150000   # add an assumed 균등
open state/backtest/net-latest.html
```

Over the same 234 deals, at maximum 청약한도 and a 3%/yr opportunity cost:

| Bucket | n | 순손익 | 초과수익률¹ | 평균 배정 |
|---|---:|---:|---:|---:|
| **GO** | 114 | **+1,581만** | **+10.01%** | 16주 |
| WATCH | 69 | +4만 | +0.03% | 39주 |
| PASS | 51 | −3,442만 | −22.80% | 884주 |
| *전 종목* | 234 | **−1,857만** | −4.33% | 212주 |

¹ Won per won-year of capital committed, *on top of* the 3% it would have earned idle.

**Subscribing to everything loses money** — 86% of deals rose at the open, and the
strategy still ends 1,857만 down. The losses concentrate where competition was weak:
nobody wanted those deals, so you were filled with hundreds of shares, and they fell.
노머스 (비례 5:1) returned 3,300 shares on 24,915만 committed at −29% — a single
2,875만 loss.

This is also why WATCH does not notify: **+0.03% excess return is zero**, before the
friction of moving funds.

### What is measured vs assumed

배정 is **비례 only**: `floor(청약한도 / 비례경쟁률)`, both published per deal. 균등 is
excluded because 청약건수 (how many people subscribed) is published nowhere. The
50/50 pool split *is* derivable as `1 − 통합률/비례률` and holds for 235 of 237 deals,
but pool size cannot give shares per person. Every figure above is therefore a
**floor**; a plausible 균등 adds roughly 10–15% to the GO net and changes no
conclusion. Before fees and tax; assumes one broker, no capital reuse across
overlapping deals.

## Layout

```
run.py               entry point — screen, report, notify
backtest.py          threshold calibration against realised 시초/공모
net_returns.py       실질손익 — won earned net of opportunity cost
config.json          thresholds
ipo_radar/
  fetch.py           HTTP + EUC-KR decode, polite 1s delay, retries
  parse.py           38.co.kr parsers (label-keyed, not position-keyed)
  score.py           GO/WATCH/PASS rules
  notify.py          macOS notifications via osascript
  report.py          markdown rendering (daily screen)
  render_backtest.py HTML rendering (시초/공모 backtest)
  render_net.py      HTML rendering (실질손익 backtest)
state/
  latest.md          most recent report
  reports/           dated archive
  seen.json          dedupe — each deal alerts once
```

## Maintenance

The parsers key off Korean label text (`주간사`, `기관경쟁률`, `의무보유확약`) rather
than cell positions, so column reshuffles are survivable but label renames are not.
If a run reports 0 rows, that is the thing to check first.

`state/seen.json` is what stops repeat alerts. Delete an entry to be re-notified.

## Source

Data from [38커뮤니케이션](https://www.38.co.kr/html/fund/). Please keep the request
rate polite — `fetch.py` enforces a 1-second delay and the daily run makes roughly a
dozen requests.
