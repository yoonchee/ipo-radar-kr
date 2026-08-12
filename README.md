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

A deal is blocked outright — never GO — if 기관경쟁률 is under 300, or if the price
failed to reach the band top.

Thresholds live in `config.json`.

## Calibration

`backtest.py` joins 수요예측결과 (knowable before subscribing) against 신규상장
(what actually happened), measuring **시초/공모** — subscribe at the offer price,
sell into the opening auction.

```bash
python3 backtest.py --pages 18
```

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
- **No allocation modelling.** 배정 is pro-rata against retail demand (균등/비례), so
  a hot deal returning +150% on a handful of allocated shares may be worth less in
  cash than a lukewarm one where you got filled. This tool ranks *quality*, not
  expected won.
- Returns are before fees and tax, and assume you actually sell at the open.

## Layout

```
run.py               entry point — screen, report, notify
backtest.py          threshold calibration against realised outcomes
config.json          thresholds
ipo_radar/
  fetch.py           HTTP + EUC-KR decode, polite 1s delay, retries
  parse.py           38.co.kr parsers (label-keyed, not position-keyed)
  score.py           GO/WATCH/PASS rules
  notify.py          macOS notifications via osascript
  report.py          markdown rendering
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
