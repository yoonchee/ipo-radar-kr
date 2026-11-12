# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A daily screen for Korean IPO subscriptions (공모청약). It scrapes 38.co.kr, scores each
offering on institutional demand, and raises a macOS notification for the good ones.

**It is decision support and must stay that way.** It never logs into a brokerage, moves
funds, or places an order — that boundary is a deliberate product decision, not a missing
feature. Do not add order placement, account linking, or fund transfer.

## Hard constraints

**Standard library only.** No `requests`, no `beautifulsoup4`, no virtualenv. The scheduled
launchd job runs under stock macOS `/usr/bin/python3` (currently 3.9), and the whole point is
that it cannot break from dependency drift or a venv that moved. Python 3.9 also means no
`X | Y` type unions at runtime and no `match` statements. If a change seems to need a
dependency, it almost certainly doesn't.

**Parsers key off Korean label text, never cell position.** `parse.py` finds the cell whose
text ends with `주간사` / `기관경쟁률` / `의무보유확약` and takes the *next* cell. This survives
column reshuffles, which positional indexing would not. Do not "simplify" it to indexes.

**Never route data through an LLM summarizer.** During development, WebFetch's summarizer
returned plausible but wrong company names for these pages. Parse the HTML directly.

## Commands

```bash
python3 run.py --all --no-notify   # the dev loop: score everything, no notifications
python3 run.py                     # normal run: lead-time window only, notifies new GOs
python3 run.py --force             # re-notify deals already alerted
python3 backtest.py                # re-calibrate thresholds against realised outcomes
python3 net_returns.py --rate 3.0  # 실질손익 + rebuilds state/cache/dataset.json
python3 evaluate.py                # walk-forward check on the forecast model
python3 -m unittest discover tests # parser regression tests (stdlib, no fetches)
cat state/latest.md                # most recent report
```

`dataset.json` is what `forecast.py` learns from and `evaluate.py` validates, and
`net_returns.py` is its only writer — run `backtest.py` first, since it reads that
output. `state/` is gitignored, so on a fresh checkout the file is absent and the
daily report silently omits its 기대손익 section until you rebuild it.

`tests/test_parse.py` holds one case per parser shape that has ever failed on the live
site. The inputs are synthetic cell sequences in 38's layout, never copies of its pages:
the repo is public. A new shape found in the wild gets a test before the fix. The tests
don't replace running against the live site — `--all --no-notify` is the safe way to do
that — and when changing `parse.py`, diff old against new `parse_detail()` over every
page in `state/cache/detail/` (see below).

### Failures must be loud

Every parser returns None both for "not published yet" and for "published in a form we
can't read", and downstream reads None as the first: a PENDING verdict, a skipped
backtest row. That is how one day turned up six silent failures at once:
- a bare `187.10` with no `:1` (엠비디 sat at PENDING on its 청약일)
- `106.30 : 1` with spaces around the colon
- 3-cell underwriter rows
- tables listing only the junior 인수회사
- DB금융투자 rows dropped for lacking "증권"
- dot-separated summary lines

None raised an error. The guard:

- **Digits in a cell that didn't parse are a parse warning** (`parse._unreadable`), in
  `parse_detail()`'s `parse_warnings` and `parse_demand_results(page, warnings)`.
- **Invariants:** a 확정공모가 with no 기관경쟁률 after the backfill (they publish
  together), and a summary-line brokerage missing from the parsed list.
- **Warnings are surfaced:** at the top of the report, in the GO email, on stderr, and as
  one banner per new message (`state/warned.json`).
- **`net_returns.py` names every skip and its exact cause.** It used to print 6 of 12
  under one "incomplete data" label.

Over all 302 pages on disk the warnings fire zero times. Keep it that way: a warning that
fires on legitimate data teaches you to ignore it.

## Architecture

The screen is a three-source join, which is why no single page gives you the answer:

1. `?o=k` (공모청약일정) — upcoming subscription windows. Gives each deal 38.co.kr's internal
   `no`, which is the join key for everything else.
2. `?o=r1` (수요예측결과) — 기관경쟁률 and 의무보유확약, published *after* the deal appears on the
   schedule page. Used as a backfill when the detail page is sparse.
3. `?o=v&no=<N>` (detail) — the real record: per-brokerage 청약한도, the 확약 tenor breakdown,
   and the full calendar.

`run.py` orchestrates that join, then `score.py` reduces it to GO / WATCH / PASS / PENDING /
EXCLUDED. A deal with no 수요예측 result yet is PENDING, not PASS — it will be re-evaluated on
a later run once the numbers publish.

### The underwriter block has several shapes

This is the one piece of parsing that is genuinely non-obvious, and it was found empirically:

- **Multi-underwriter deals** usually render an `인수회사 | 주식수 | 청약한도 | 기타` table, one
  row per brokerage with its own 청약한도 and role (대표주관/공동주관/인수). The summary line
  above it shows `청약한도: -`, so reading the summary alone loses the per-brokerage limits.
- **Single-underwriter deals omit that table entirely** and only have the summary line
  (`주식수: N 주 / 청약한도: A~B 주`).
- **Sometimes the table rows read `- 주`** and the one range sits on the summary line
  (에이치엘지노믹스), or **the table lists only the junior 인수회사** and the lead appears on
  the summary line alone (1912: 유진 7,000 in the table, 신영 27,000~36,000 above it;
  두산로보틱스).
- **Rows are not always 4 cells.** An empty 기타 is dropped, so rows are split at each
  brokerage name. A fixed 4-cell stride read the next brokerage's name as the role and
  silently lost every underwriter after the first on 13 cached deals — on three of them
  the dropped one had the higher limit (NH 30,000 vs 한투 17,000).
- **Not every brokerage name contains 증권** (DB금융투자), and the summary line is
  sometimes dot-separated with abbreviated names (`KB증권,신한.하나.대신.삼성증권`), so
  names are matched with `_same_broker()`, not by equality.

`_parse_underwriters()` therefore reads both and merges: table rows first, then the
summary range for any summary-line brokerage the table left without a limit. Breaking
any path is silent — you get an empty or wrong broker list, not an exception. When
changing it, diff old vs new `parse_detail()` over every page in `state/cache/detail/`:
only the pages you meant to change should move.

### A missing 청약한도 is estimated, never a reason to drop a deal

When 38 publishes no limit at all, the parser can't produce one:
- **세나테크놀로지** shows `청약한도: -` and has no table.
- **A live deal** can still have `- 주` in every row when it goes GO, as 에스에프씨 did.

So `parse.estimate_limit()` takes 4% of the largest brokerage's own 일반 allotment. Over
the sample, 청약한도 as a share of that allotment has a median of 4.0% either way. The
p10–p90 spread is 3.2–6.2% across 191 single-underwriter deals, and 1.9–6.0% across the
lead rows of 39 multi-underwriter deals. The range shown matches the deal's shape.

Two limits of the estimate are printed with it, never left implicit:
- **It ignores your 우대 tier (`우대 미적용`).** The tier multiplies the 일반 base, which is
  exactly what's unknown, so the figure is 38-standard while every other live number is
  your own.
- **A summary-line-only multi-underwriter deal is flagged `과대추정 가능`.** There every
  brokerage carries the deal's combined 주식수, so the estimate assumes the lead holds
  all of it.

The estimate is labelled everywhere it lands:
- **live:** `my_limit["estimated"]` and `my_limit["note"]`, `⚠추정한도` in the report, and
  a line in both the GO email and the calendar event, with the range
- **backtest:** `limit_source` in `net_returns.py`
- **forecast data:** `estimated` in `dataset.json`

An estimated deal stays in the forecast as an analogue, since that only needs `mult` and
`ret`. `evaluate.py` doesn't score it as a target, because its realised net rests on a
guessed limit.

A missing or impossible 비례경쟁률 is filled from 2 × 통합:
- **Why it holds:** across all 246 deals that publish both, 비례 / 통합 sat at 1.97–2.02
  (p2–p98), since 균등 takes half the pool.
- **When it's used:** REITs that publish 통합 only, or 져스텍's truncated `비례 4:1`.
- **REITs still stay skipped:** no REIT in the sample publishes both figures, so the rule
  is unverified for them.

This is a deliberate exception to "don't invent a 청약건수" below, and it doesn't license
that. Both estimates above were measured, and each has a known spread. 청약건수 isn't
published anywhere, so there's nothing to measure it against.

### Scoring rationale

Three signals, all knowable *before* the subscription window opens: 기관경쟁률, 확정공모가
relative to the 희망공모가밴드, and 의무보유확약 weighted toward long tenors. The tenor weighting
matters because 15일 확약 unlocks before listing-day selling pressure is relevant while 3/6개월
확약 genuinely removes supply — the headline 확약 percentage alone is misleading.

But the tenor share is a share *of* 확약, so the +1 needs 확약 >= `watch_lockup_pct` too.
Ungated, 80% of a 0.87% 확약 earned it and promoted 39 historical deals to GO; together
they lost 264만 net, and GO's win rate fell from 99% to 93%. Note that `backtest.py`
scores with an empty `lockup_breakdown` (its list pages carry no tenor), so it never
sees this bonus at all. Checking a tenor rule means scoring the detail-page cache
`net_returns.py` builds (`tenor3` in dataset.json), which is how this one was found.

Thresholds live in `config.json` and were set from `backtest.py`, not from intuition.

`calendar_sync.py` creates two Google Calendar events per GO — 청약 on the last
subscription day at 10:00 alerting at 10:00 (brokerages may refuse an earlier
청약), and 매도 on 상장일 at 09:00 alerting at 08:30, when 장전 동시호가 opens and
the 시초가 can still be set. Do not "tidy" the 매도 alert to fire at 09:00: the
시초가 is already fixed by then, and 시초/공모 is the strategy being screened for.
`state/calendar.json` tracks which events exist per deal so none is created
twice, and lets a 매도 event be added on a later run once 상장일 publishes. This
stays decision support — it writes reminders, never orders.

The 매도 event carries a second, EMAIL alarm at the same offset. That is not a
belt-and-braces flourish — it is what pins the popup. Google normalises every
alarm to a relative popup and discards it when the set equals the calendar's
default, storing `useDefaultReminders: true` instead; measured, a lone -PT30M
against a 30-minute default collapsed, and so did an absolute
`TRIGGER;VALUE=DATE-TIME`. Only a set that differs survives. Remove the EMAIL
alarm and the 08:30 alert silently starts tracking whatever the user's default
reminder happens to be.

It reaches Google over **CalDAV** (`www.google.com/calendar/dav`, not
`apps.google.com`, which 405s on PROPFIND) using the **same Gmail app password as
SMTP**. That is deliberate and was chosen after the REST API: the REST route
needs an OAuth client, hence a Cloud project, consent screen and publishing an
app with a sensitive scope — verification paperwork for a one-user script. Do not
"upgrade" this to the REST API. Event UIDs are deterministic, so a repeat write
replaces rather than duplicates even when calendar.json is missing.

**우대 tiers are personal and 38 never shows them.** 38 publishes one 청약한도
range per brokerage (멜콘: 대신증권 20,000~25,000주). The LOW end is the 일반 base
a brokerage's 우대 tier multiplies — measured on 멜콘, where a 우대 tier's limit
was exactly 38's low end times the tier. `run.py:my_limit()` applies
`my_limit_multiplier` from `config.local.json` (`{"키움증권": 1.5}`) to the live
forecast, alerts and calendar; a brokerage with no entry falls back to the top of
38's range, as before. `backtest.py` and `net_returns.py` deliberately stay on
38's standard limit — the screen is judged on the market, not on one account.
So the report's EV is *your* EV, and is not comparable to the backtest's.

`config.local.json` is merged over `config.json` and is gitignored — the alert
address lives there, because `config.json` is published. A fresh checkout has no
such file, so email alerts stay off until one is created; `--test-email` says so
rather than failing silently. The SMTP app password is in the login keychain
(`security`, service `ipo-radar-smtp`), never on disk in plaintext.

## Working with the backtest

`backtest.py` joins 수요예측결과 against 신규상장 and measures **시초/공모** — subscribe at the offer
price, sell at the opening auction. That is the strategy being screened for; 첫날종가 is a
different strategy and is not what the thresholds are tuned against.

Two things to keep in mind before re-fitting anything:

- **The sample is regime-dependent.** Every deal in it listed on or after 2023-06-26,
  when Korea widened the listing-day opening range to 60–400% of 공모가.
  `backtest.py` cuts the sample by that date (`REGIME_START`), not by page count: the
  old `--pages 18` was a sliding window that had let 15 pre-regime deals in, and would
  have started dropping valid ones as new listings pushed them off the last page.
  The ~86% baseline win rate is a property of that regime. If the rules change, the
  thresholds need re-deriving, and the historical numbers stop being a guide.
- **A high baseline flatters any screen.** Most deals in the sample were profitable at the
  open, so the screen's job is avoiding the bad tail, not finding rare winners. Judge changes
  by what they do to the PASS bucket, not just the GO bucket.

## 시초/공모 percentages are not money — use net_returns.py

`backtest.py` measures the price move. `net_returns.py` measures the trade, and the two
disagree about the headline. Subscribing at max 청약한도 to *every* deal loses 3,132만 won
over the sample (n=255, rebuilt 2026-11-12) despite 84% of deals rising at the open,
because 증거금 (50% of 청약한도 × 확정공모가) sits idle from 청약일 to 환불일, and because
weak deals — the ones nobody wants — fill you with hundreds of shares right before they
fall. GO earns +12.17% excess return; PASS earns −30.79%.

Two consequences, both already encoded:

- **WATCH does not notify.** It measured +1.74% over 78 deals, but one lucky deal carries
  much of that: 이에스티 (630:1, 확약 0.24%) drew 380주 when the forecast expected 16 and
  happened to open +28%. Without it WATCH is +0.98% over 77 deals — noise next to GO's +12%.
  `run.py` raises a banner only for a newly-GO deal. Don't "fix" this by restoring a summary
  notification.
- **배정 is 비례 only** — `floor(청약한도 / 비례경쟁률)`. 균등 is excluded because 청약건수 is
  published nowhere on 38.co.kr. The 50/50 pool split *is* derivable as `1 − 통합률/비례률`
  (holds for 235/237 deals), but pool size cannot give shares per person, so every net
  figure is a floor. Don't invent a 청약건수 to "complete" it; the sensitivity is ±10–15%
  and changes no conclusion.

`net_returns.py` caches detail pages under `state/cache/` — it needs one fetch per deal, so
the first full run makes ~240 requests. Never drop the cache or the 1s delay. A cached
page counts only if it was saved **after** the deal's 상장일, so a page fetched mid-deal is
re-fetched once. Before that rule, three pages cached on their 청약 day kept an empty
청약경쟁률 for three months and were reported as "38 never published it" (니어스랩, 해치텍,
기도산업 — 38 had the figures all along). A listed deal always had a 청약, so an empty
청약경쟁률 there is a data gap, and the skip line says so.

## The forecast model — and why it stays this simple

`forecast.py` estimates expected net won for a live deal by kernel-weighting past
deals in (log 기관경쟁률, 의무보유확약) and drawing each analogue's retail-enthusiasm
multiple and opening move **jointly** — never averaging them separately, since their
correlation is the signal.

`evaluate.py` is the reason to trust any of it. Walk-forward, time-ordered, and the
leakage rule is subtle: an analogue counts only if its **listing date** precedes the
target's *subscription* date. A deal that had subscribed but not yet listed has no
outcome to learn from. Getting this wrong invalidates everything downstream.

What the harness established, and what you should not undo:

- **Hard-bin comparables were the worst model tested** (MAE 64.3만 vs ~45 for
  everything else, rank corr 0.285 vs 0.385 shipped; n=176 scored, 2026-11-12).
  Smooth kernel weighting beat it because it uses every past deal instead of a
  handful in-or-out. The figures were 62.2/~41 and 0.307/0.460 at an earlier
  build; that drop is not recent deals being mis-ranked — targets listed before
  2026-10-01 score 0.372 and the 14 since score 0.367.
- **No third feature beat the shipped model on either metric.** 3개월+ tenor share,
  band position, and recency decay all scored worse on both MAE and rank correlation
  (2026-11-12: shipped 44.4만 / 0.385; tenor3 49.3만 / 0.300, band position 45.9만 / 0.276).
  Against their own h=1.0 base the picture has flipped between rebuilds — tenor3
  raised rho at one build and only matched it at the next — which is exactly the
  noise the "materially beats" bar exists to filter. Two features is not laziness; it is the
  measured optimum at this sample size. Adding a third needs a walk-forward win, not a
  plausible story.
- **Bandwidth 0.6 is deliberately not the argmax.** MAE was flat across the whole
  sweep and only rank correlation moved, monotonically — so the peak is the edge of a
  noisy curve. 0.6 rests on ~61 effective neighbours instead of ~31.
- **The model is only calibrated at 기관경쟁률 >= 600** (`CALIBRATED_MIN_INST`). There
  bias is +2.5만 and terciles are monotonic. Below 300 bias is +178.9만 — weak demand
  means a huge allocation, and 28 examples cannot pin that tail down. Never quote an
  EV for a low-competition deal (see below: the outputs no longer print one).
- Always show the bootstrap CI. For 니어스랩 the EV was -5,456원 against a CI of
  ±100,000원; the point estimate alone would have been a lie of precision.
- **But the bootstrap CI is not an outcome range** — it is the uncertainty of the
  *mean*. Labelled "90% 신뢰구간" it was read as where the deal would land, and none
  of the 13 deals listed 2026-08-13..10-22 finished inside it. Every output shows it
  as `평균 추정오차` next to `결과 범위` (the analogues' weighted p10~p90), which
  walk-forward held 80% of outcomes at >= 600 against a nominal 80% — 86% where
  확약 >= 2%, but only 52% where it is below 1% (next bullet but one).
- **Below 600 the EV and 흑자확률 are withheld, not just flagged.** There 결과 범위
  held 61% and 흑자확률 ran 75% against 66% realised. Output shows the range and a
  warning only. Do not restore the point estimate for low-demand deals.
- **The same applies at >= 600 when 확약 is below 1%** (`CALIBRATED_MIN_LOCK`). Over
  23 such deals 결과 범위 held 52% (10 outcomes below p10), 흑자확률 read 75% against
  43% realised, and bias was −15.4만. Retail often stays away from them — 이에스티
  was allocated 380주 against a median forecast of 16 — so analogues understate the
  allocation. These deals cannot reach GO (GO needs 확약 >= `watch_lockup_pct`), so
  only the report's WATCH rows are affected. The 1% cut was chosen on the same
  holdout it was measured on, and 1~2% (n=6) was left alone for lack of data.

Note the caveat the numbers cannot remove: ~17 variants were scored against the same
walk-forward sequence, which is selection on the holdout. That is why the bar was
"materially beats the incumbent", not "best marginal edge".

## Etiquette toward the source

38.co.kr is a small site run for retail investors, and its `robots.txt` permits crawling.
`fetch.py` enforces a 1-second inter-request delay; a daily run makes roughly a dozen
requests. Keep it that way — don't parallelise the fetches or drop the delay.

## State

`state/seen.json` is what stops the same deal alerting every day. Delete an entry to be
re-notified for that deal. `state/` is machine-local output, not source.
