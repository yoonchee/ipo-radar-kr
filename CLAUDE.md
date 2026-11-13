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
- **Invariants:**
  - a 확정공모가 with no 기관경쟁률 after the backfill (they publish together);
  - a summary-line brokerage missing from the parsed list;
  - a 청약한도 at or above the brokerage's own 주식수. That one is dropped, not kept.
- **Warnings are surfaced:** at the top of the report, in the GO email, on stderr, and as
  one banner per new message (`state/warned.json`).
- **`net_returns.py` names every skip and its exact cause.** It used to print 6 of 12
  under one "incomplete data" label.

Over all 273 detail pages on disk the warnings fire on exactly two, both genuine 38
errors: 파로스아이바이오 shows its 주식수 as its 청약한도, and 산일전기 gives 삼성 미래's
limit. Keep it that way: a warning that fires on legitimate data teaches you to ignore it.

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
- **An impossible limit counts as none.** A 청약한도 at or above the brokerage's own
  주식수 is a 38 error. 파로스아이바이오's "420,000주" was its share count, and taken as
  read it put 29.4억 of 증거금 into the backtest.

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

**우대 tiers are personal and 38 never shows them.** Nor is 38's low end one tier.
The 투자설명서 audit (2026-11-12, 255 deals) found it the 50% tier at 한투 on 14 of
33 deals and the 100% tier on the rest, the 200% tier at 대신 on some deals
(토모큐브: 50,000 = 우대 200%; 일반 100% is 25,000) though not on 멜콘, 100–300% at
신영 from deal to deal, and another brokerage's limit outright on several. A fixed
multiple of 38's low end is therefore wrong on some deals at every brokerage.

So the live screen reads the limit from the deal's final 투자설명서 too
(`dart.live_limits`, for GO/WATCH deals only, in `attach_forecasts`). It is filed
before the 청약: a median 7 days before its last day, 2 at the latest in the sample.
`run.py:my_limit()` then takes, per brokerage:
- **Your tier** from `my_tier_pct` in `config.local.json` (`{"키움증권": 150}`), a
  percentage of that brokerage's own 100% base. The figure the prospectus prints for
  that tier is used as is; only an unprinted tier is base × pct. A brokerage with no
  entry gets its 100% base.
- **A pre-pricing range** gives its low end, flagged `ranged` in every output. There is
  no 실적보고서 yet to settle it, and 22 of the 27 ranged deals in the backtest ran low.
- **A brokerage the prospectus didn't read** falls back to 38, and to a different key,
  `my_limit_multiplier`, a multiple of 38's low end (`{"키움증권": 1.5}`, measured on
  멜콘). Without a 38 entry, the top of 38's range.
  The two keys never share numbers: a multiple set where 38 showed a brokerage's
  50% tier is double the real tier when applied to the 100% base.
- **A brokerage only on 38** is dropped when the prospectus read every brokerage it
  lists (산일전기: 38 shows 삼성 with 미래's limit).

`my_limit["source"]` is `prospectus`, `38` or `estimate`. Every output says which:
- the email and calendar print `투자설명서` or `38 하한 × m`;
- they print 38's figure when it differs from the 100% base;
- they print why the prospectus wasn't used (`limits_note`: no key, not filed yet).

Disagreeing with 38 is not a parse warning: it happens on a fifth of deals.
`backtest.py` and `net_returns.py` deliberately stay on the 100% tier (below), since
the screen is judged on the market, not on one account. So the report's EV is *your*
EV, and is not comparable to the backtest's.

**A cached DART filing list counts only if it was saved after its window closed**
(`dart._listing_fresh`). The live screen queries the same window the backtest later
reads (60 days before the 청약 to its last day), so a list cached mid-deal would
otherwise hide the post-pricing 정정 from both. The filings themselves never change
and stay cached. `corpCode.xml` is re-downloaded at most once a day for a ticker it
lacks: a company DART does not list yet is found by name.

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
disagree about the headline. Subscribing at the 100% 청약한도 to *every* deal loses 6,084만 won
over the sample (n=255, rebuilt 2026-11-12) despite 84% of deals rising at the open,
because 증거금 (50% of 청약한도 × 확정공모가) sits idle from 청약일 to 환불일, and because
weak deals — the ones nobody wants — fill you with hundreds of shares right before they
fall. GO earns +13.08% excess return; PASS earns −55.05%.

Two consequences, both already encoded:

- **WATCH does not notify.** It measured +3.05% over 78 deals, but one lucky deal carries
  much of that: 이에스티 (630:1, 확약 0.24%) drew 321주 비례 at 18,000 when the forecast
  expected 16, and happened to open +28%. Without it WATCH is +2.30% over 77 deals,
  small next to GO's +13%.
  `run.py` raises a banner only for a newly-GO deal. Don't "fix" this by restoring a summary
  notification.
- **배정 is read from DART, computed only where it can't be.** 38 publishes no
  청약건수, but the 증권발행실적보고서 on DART does. It also has one table per brokerage
  with every 청약 amount and the 균등 and 비례 shares each application got. See
  `ipo_radar/dart.py` and the section below. Elsewhere `net_returns.py` falls back to
  `floor(청약한도 / 비례경쟁률)` plus a 균등 at an assumed 150,000 subscribers. Every row
  carries `alloc_source` (`dart`, `dart_interp` or `38`). The forecast dataset stays 비례-only either
  way. Don't invent a 청약건수 for a fallback deal; read it from DART or leave the
  assumption labelled.

### DART 증권발행실적보고서 — the allocation record, after the fact

`ipo_radar/dart.py` reads the filing that every offering submits after its 청약.
- **Filed after close:** it's history only — backtest, 실질손익 and forecast analogues.
  The live input from DART is the 투자설명서 (above), not this.
- **Key:** in the login keychain, service `ipo-radar-dart`; without it `net_returns.py`
  says so and uses the formula.
- **Caching and pace:** responses are cached under `state/cache/dart/`, at 1s between
  calls.
- **Company lookup:** by 종목코드, falling back to name. Tickers can contain letters
  (`0015M0`), and a company that only just listed has no code in DART's list yet.

What it established:
- **Exact for 동원파츠:** 통합 1219.37 against 38's 1219.37, and 32,500주 → 균등 1 + 비례 13,
  the allocation actually received.
- **Weak deals hand out far more than the formula says.** Leftover and unpaid 균등 goes
  to 비례: 엠앤씨솔루션 (2.4:1) gave a 20,000주 KB application 7,559 shares against the
  formula's 4,002. Over the sample PASS went from −30.79% to −55.05% excess return
  (−13.47% without 엠앤씨 and 인제니아).

**How a filing is read.** Tables are read as tables, not as a stream of text: `<TR>`
rows and `<TD>`/`<TE>` cells with ROWSPAN and COLSPAN resolved into a grid (`_grid`).
A token-stream parser came first, and reading 230 of 255 deals was its ceiling. It
dropped empty cells, so continuation lines passed for rows, a missing 합계 merged two
brokerages, and `X | 1 | X` triples became one-application rows. Every rule below was
earned by a filing:
- **Columns are found by their header labels** (`_roles`), wherever they sit:
  - 청약단위, 청약건수, 청약수량;
  - the total, whose label names its own addends: `(D=B+C)`, `(E=B+C+D)` where 균등 is
    printed in two groups (데이원, 닷밀), `(F=C+E)` (엠앤씨 키움);
  - each addend by its letter, else the last column of its group. 큐리옥스's 비례 total
    is `최종비례배정수량(g=e+f)`.

  Whether an addend is 균등 or 비례 comes from the **group header** above it, not the
  leaf: 데이원 labels its 비례 column `균등배정수량(D)` under `비례방식`. Labels survive
  line breaks (`청약` / `건수`, 채비 KB), misspellings (`쳥악건수`, 비아이매트릭스) and a
  missing parenthesis (`균등배정수량C)`, 전진건설로봇).
- **A row is proven by its own arithmetic:** total = Σ addends, **exactly**. A unit can
  span several grid lines (닷밀 puts 313 비례 shares on a second line); spanned cells are
  one cell, so nothing counts twice. The main line alone is tried first (래블업's
  sub-lines split a 균등 total the main line already carries). A row that doesn't add up
  is dropped and counted (`rejected`), never guessed.
- **청약수량 is 단위 × 건수, by definition.** The printed figure is only classified:
  - `full`;
  - `half_net`: NH prints 단위 × 건수 ÷ 2 − 균등, the 증거금 basis left after 균등 (루미르,
    유진테크놀로지, 코스모 NH, LGCNS NH);
  - `typo`: 위너스 printed 50,472,000 for 42,000 × 10,187, exactly the gap to its
    summary.
- **Only the body counts.** A 정정 filing opens with 정정 전/후 copies of its tables
  before the `<COVER>`; 유라클 had three of everything.
- **A table can come in two halves.** 큐리옥스 prints 균등 in one table and 비례 plus the
  total in the next, with the same rows. A table with no total column is joined to the
  next one, row for row.
- **Brokerage headings.** A table's brokerage is the closest heading since the
  previous allocation table, caption tables included: `[삼성증권]`, then `(단위: 주)`,
  then the table (엠앤씨). Forms seen:
  - `[한국투자증권]`, which wins anywhere on a line (`※ …배정방식별 배정현황[KB증권]`,
    세나);
  - `1) 대신증권(주)- …`;
  - `2) (주)아이엠증권`.

  Prose notes like `주1) …(주)는` are skipped. Hangul spellings map to 38's Latin ones
  (케이비→KB, 엔에이치→NH, 디비→DB …). Only an unnamed lone table is assumed to be
  38's brokerage.
- **The filing must reconcile with itself** (`net_returns.reconcile`):
  - **Against the summary.** The tables' 청약수량 must come to 80–100.1% of the
    summary's in "3. 청약 및 배정현황". The summary's columns are found by label too.
    - Under 100%: a table left unread, which costs only its own rows.
    - Over: rows that shouldn't exist.
    - The filer's own 합계 may count NH at its half-net figure (LGCNS: exact that way),
      so either total may match.
  - **Allotted shares** may fall short of 최종배정, because 잔여주 and 실권주 go to the
    인수인, but must never exceed it.
  - **The summary's 금액 is a second witness** (`summary_qty`), priced at the full or
    the 50% 증거금 price:
    - 바이오비쥬: printed 884,413,080, 금액 = 884,143,080 × 9,100 ÷ 2, which is what its
      tables add up to;
    - 이지스: the whole row shifted one cell.
  - **A summary whose 청약 equals its 배정 is a copied column** (신성에스티, 필에너지,
    엑셀세라퓨틱스). So is a 금액 computed from it. Those deals are checked by 청약건수
    instead, plus 38's ratio within 15%.
- **The 청약한도 comes from the 투자설명서, not from 38** (`parse_limits`,
  `prospectus_limit`; `limit_source` `prospectus`). The backtest uses each
  brokerage's 100% tier, at the brokerage offering the most. See below for what 38
  got wrong.
- **Which row.** The exact 청약 amount at that limit; 252 of 255 deals have one.
  Where 38's figure is the fallback (no 투자설명서 read), its high end first, then
  its low end: 38's high end assumes a 20% upsizing that usually didn't happen.
- **Otherwise, interpolate** (`dart_interp`) between the rows either side, and only
  when both give the same 비례 rate per share. Nobody applied at exactly that
  amount: 인제니아 (undersubscribed 3:1, a flat 0.2635 비례 per share across the
  whole ladder), 케이뱅크 and 에코프로머티리얼즈 (large KOSPI limits).
  - Leave-one-out over 236 exact deals: median error 1.5%, within 1% on every allocation of 50 shares or more
    (about a share at most there).
  - One neighbour is never enough: a nearby unit once read 44주 for a 420,000주
    "limit" that was 38's share count.

Expected shares per application are the row's 균등 and 비례 totals ÷ 청약건수. That's
exact in expectation, lottery top-ups included.

**Why 38 and the filing disagree.** It's a difference in source, not a parse error. 23
deals differ by more than 2%, and the filing is used for all of them:
- **Announced vs final.** 38 reports the ratio announced at close; the filing reports
  the final one, after 중복·부적격 청약 were voided. Both are public: for 빅웨이브로보틱스
  dealsite reported 1375.34 (= 38), and alphasquare 1307.78 from 248,338건 (= filing).
- **KOSPI denominator.** On (유가) listings 38's ratio is exactly ×0.833 the filing's
  (티엠씨 1305.46 / 1566.56, 명인제약, 대한조선, 더본코리아, 산일전기 …). 38 divides by a
  retail pool at 30% of the offering where the actual allotment was 25%.
- **38 errors.** 로킷헬스케어: 38 80.44, filing 40.15; press reported "80 대 1" alongside
  SK 33.81 and KB 97.67. It looks like 38 published the 비례 ratio in its 통합 field.
- **Ratio comparisons use the 일반 allotment as the denominator.** The tables' own
  totals leave out 잔여주 and 실권주, which once made 아이씨티케이 read 1899.67 against a
  reported 1,107.95.

Coverage at 2026-11-12: all 255 deals, 252 exact and 3 interpolated. The two REITs stay
skipped: their filings carry no allocation table. A new layout gets a test in
`tests/test_dart.py` first, and it must leave reconciliation passing. Changing the
parser means diffing every deal's allocation before and after; only the deals you
meant to move should move.

### 투자설명서 — the 청약한도 each brokerage actually offered

`parse_limits()` reads a deal's final 투자설명서 (`kind=dart.PROSPECTUS`, the newest
non-예비 one filed in the 60 days before the 청약). It parses the '일반청약자
배정물량, 최고청약한도' table and the notes after it, and returns each brokerage's
tiers and its 100% base. 38 copies these figures by hand, and the audit found that
it is not one tier:

| 38's figure vs the 100% tier | deals |
|---|---|
| its low end (38's high end is an upsizing that did not happen) | 207 |
| half of it (38 shows the 50% tier: 한투, 삼성, IBK) | 19 |
| double or triple (38 shows 200%/300%: 대신, 신영) | 5 |
| another figure or another brokerage | 19 |

Among the last group:
- **필에너지:** 미래 was shown 삼성's 5,000~6,000; it was 29,500.
- **케이엔알:** DB was shown NH's 150% tier; DB was 26,300 for everyone.
- **LS머트리얼즈:** 키움 6,000~7,000 against a real 100,000, where the filing has 7,586
  applications.
- **엠디바이스:** 18,900 was not even a valid amount (1,000주 steps).
- **동인기연:** 38 kept the pre-정정 15,000~18,000; it was 12,000.

Wordings, each from a filing (one test per form in `tests/test_dart.py`):
- `25,000주 (100%)`, `18,000 주 (온라인, 100%)`: 대신, 미래, NH, 한투, 유안타;
- tier tables, `일반 청약자격 | 100% | 15,000주`: 삼성;
- `일반(100%)그룹의 청약한도: 7,000주`, `일반고객(100%)의 청약한도 : 15,000주`: 신영, IBK;
- no percentage, `일반고객의 청약한도 : 10,000주` or `일반청약자 최고청약한도는 20,000주
  이나`: 하나, BNK;
- one limit for everyone, only the table's figure: DB, 하이, 키움, BNK.

Rules that came from failures:
- **The table's own figure is not the base when tiers are listed.** NH prints its 300%
  there, and 신영 its 300%.
- **Inline tags split numbers** (`15, 0 00주 (10 0%)`, 에스오에스랩), so spaces inside
  digits are removed. The window is matched as one string, and a tier belongs to the
  brokerage named last before it.
- **A row labelled `일반청약자`** takes its brokerage from the caption above (로킷 SK).
- **A missing 100% line is derived** from another tier (200% halved), labelled
  `prospectus_derived`. No deal needs it today.
- **The best brokerage is chosen only when every brokerage was read.** Otherwise
  only 38's own is trusted, since the unread one may be the best.
- **Ranged limits.** 27 final prospectuses keep a pre-pricing range. The filing's rows
  decide which end applied: applicants pile up at each tier's maximum, so the end
  with more applications across its tier amounts wins.
  - 씨케이솔루션: 12,355 / 1,994 / 1,695 at the high end's 100/150/200% amounts,
    against 1,055 / 144 / 53 at the low end's.
  - 두산로보틱스: 559 at the high end's 162,000 against 11 at 136,000.

  Five deals take the high end: 씨케이, 두산, 더본코리아, 산일전기, 달바글로벌.
  Their summed allotments point to the low end, so the rows are what decides
  there; why the brokerages ran the higher limits isn't explained in either
  filing.
  - With no rows at either end, the 일반 allotment against the summed brokerage
    allotments decides.

Checks on the result (2026-11-12):
- **Independent anchors:** the six limits verified earlier on the brokerages' own
  pages (동원파츠, 래블업, 인텔리빅스, 바로팜, 티앤이코리아, 멜콘) all match exactly.
- **Top tier mistaken for the base:** checked by looking for a filing whose largest
  amount equals the base at a brokerage that has tiers. One case: 비아이매트릭스 IBK.
  Its prospectus states 100% = 30,000 and 200% = 60,000, yet nobody applied above
  30,000. It is unresolved; the prospectus figure is kept.
- **Only the base read:** where higher tiers went unread (하나, 키움), the filing's
  largest amount is a clean 1.5×, 2× or 3× of the base, as it should be.

The rows themselves are the main check: 252 of 255 deals have a row
at the chosen amount, often among the busiest. 케이엔알 26,300 had 18,333 applications,
and 시큐레터 16,000 had 21,077. The largest amount is usually the top tier at that base
(현대힘스 216,000 = 2 × 108,000).

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

- **Hard-bin comparables were the worst model tested** (MAE 61.8만 vs ~45 for
  everything else, rank corr 0.277 vs 0.397 shipped; n=177 scored, 2026-11-12,
  on DART-read allocations at 투자설명서 limits).
  Smooth kernel weighting beat it because it uses every past deal instead of a
  handful in-or-out. It ranks recent deals no worse:
  targets listed before 2026-10-01 score rho 0.398 (n=163), and the 14 since score
  0.398.
- **No third feature beat the shipped model on either metric.** 3개월+ tenor share,
  band position, and recency decay all scored worse on both MAE and rank correlation
  (2026-11-12: shipped 45.2만 / 0.397; tenor3 49.5만 / 0.312, band position 46.7만 / 0.295).
  Against their own h=1.0 base the picture has flipped between rebuilds — tenor3
  raised rho at one build and only matched it at the next — which is exactly the
  noise the "materially beats" bar exists to filter. Two features is not laziness; it is the
  measured optimum at this sample size. Adding a third needs a walk-forward win, not a
  plausible story.
- **Bandwidth 0.6 is deliberately not the argmax.** MAE was flat across the whole
  sweep and only rank correlation moved, monotonically — so the peak is the edge of a
  noisy curve. 0.6 rests on ~61 effective neighbours instead of ~31.
- **The model is only calibrated at 기관경쟁률 >= 600** (`CALIBRATED_MIN_INST`). There
  bias is −0.4만 (n=143, 2026-11-12). Below 300 bias is +262.6만 — weak demand
  means a huge allocation, and 37 examples cannot pin that tail down. Never quote an
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
  held 56% and 흑자확률 ran 73% against 52% realised (n=52). Output shows the range and a
  warning only. Do not restore the point estimate for low-demand deals.
- **The same applies at >= 600 when 확약 is below 1%** (`CALIBRATED_MIN_LOCK`). Over
  23 such deals 결과 범위 held 52% (9 outcomes below p10), and 흑자확률 read 75% against
  43% realised. Retail often stays away from them — 이에스티
  was allocated 321주 비례 against a median forecast of 16 — so analogues understate the
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
