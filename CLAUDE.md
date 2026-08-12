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
python3 backtest.py --pages 18     # re-calibrate thresholds against realised outcomes
cat state/latest.md                # most recent report
```

There is **no test suite**. Verification is by running against the live site and eyeballing
the parse — `--all --no-notify` is the safe way to do that. When changing `parse.py`, check
the output against both a single-underwriter and a multi-underwriter deal (see below), since
they take different code paths.

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

### The underwriter block has two shapes

This is the one piece of parsing that is genuinely non-obvious, and it was found empirically:

- **Multi-underwriter deals** render an `인수회사 | 주식수 | 청약한도 | 기타` table, one row per
  brokerage with its own 청약한도 and role (대표주관/공동주관/인수). The summary line above it
  shows `청약한도: -`, so reading the summary alone loses the per-brokerage limits.
- **Single-underwriter deals omit that table entirely** and only have the summary line
  (`주식수: N 주 / 청약한도: A~B 주`).

`_parse_underwriters()` handles both. Breaking either path is silent — you get an empty or
wrong broker list, not an exception.

### Scoring rationale

Three signals, all knowable *before* the subscription window opens: 기관경쟁률, 확정공모가
relative to the 희망공모가밴드, and 의무보유확약 weighted toward long tenors. The tenor weighting
matters because 15일 확약 unlocks before listing-day selling pressure is relevant while 3/6개월
확약 genuinely removes supply — the headline 확약 percentage alone is misleading.

Thresholds live in `config.json` and were set from `backtest.py`, not from intuition.

## Working with the backtest

`backtest.py` joins 수요예측결과 against 신규상장 and measures **시초/공모** — subscribe at the offer
price, sell at the opening auction. That is the strategy being screened for; 첫날종가 is a
different strategy and is not what the thresholds are tuned against.

Two things to keep in mind before re-fitting anything:

- **The sample is regime-dependent.** Every deal in it listed after June 2023, when Korea
  widened the listing-day opening range to 60–400% of 공모가. The ~86% baseline win rate is a
  property of that regime. If the rules change, the thresholds need re-deriving, and the
  historical numbers stop being a guide.
- **A high baseline flatters any screen.** Most deals in the sample were profitable at the
  open, so the screen's job is avoiding the bad tail, not finding rare winners. Judge changes
  by what they do to the PASS bucket, not just the GO bucket.

There is no allocation modelling — 배정 is pro-rata (균등/비례), so a high-scoring deal where
you got three shares can be worth less in cash than a mediocre one where you got filled. The
tool ranks quality, not expected won. Don't present its output as expected profit.

## Etiquette toward the source

38.co.kr is a small site run for retail investors, and its `robots.txt` permits crawling.
`fetch.py` enforces a 1-second inter-request delay; a daily run makes roughly a dozen
requests. Keep it that way — don't parallelise the fetches or drop the delay.

## State

`state/seen.json` is what stops the same deal alerting every day. Delete an entry to be
re-notified for that deal. `state/` is machine-local output, not source.
