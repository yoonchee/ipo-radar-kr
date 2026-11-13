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
   keeps that stock off the tape on listing day. The long-tenor bonus counts only
   when 확약 itself is at least 5% — a long share of a near-zero 확약 holds nothing back.

Verdicts: **GO** (score ≥ 6, no blockers) · **WATCH** (≥ 3) · **PASS** · **PENDING**
(수요예측 not published yet) · **EXCLUDED** (스팩/코넥스).

**Only a new GO raises a notification.** WATCH appears in the report but never
interrupts — see [실질손익](#실질손익-net-won) for why.

A deal is blocked outright — never GO — if 기관경쟁률 is under 300, or if the price
failed to reach the band top.

Thresholds live in `config.json`.

## Alerts

A new GO raises a macOS banner and, when configured, an email. The two are
independent: the banner fires whether or not the mail sent, since they fail in
unrelated ways and a 청약 window can close the same day.

Email is stdlib `smtplib` — no dependency. To turn it on:

```bash
# 1. a Gmail app password (needs 2FA), stored in the login keychain --
#    run this in a real terminal, the prompt must be able to read input
security add-generic-password -U -s ipo-radar-smtp -a you@gmail.com -w

# 2. your address, in the gitignored local override
cat > config.local.json <<'EOF'
{ "email_to": "you@gmail.com", "email_from": "you@gmail.com" }
EOF

# 3. prove it works without waiting for a GO
python3 run.py --test-email
```

`config.local.json` is merged over `config.json` and never committed — anything
personal to one machine belongs there, since `config.json` is public. The SMTP
password is only ever in the keychain, read at send time; it is never in either
config file nor in the launchd plist.

### Your 우대 청약한도

38.co.kr shows one 청약한도 range per brokerage and nothing about 우대 tiers, and
its low end is not even one tier: against the 투자설명서 of 255 deals it was the
100% tier on 207, the 50% tier on 19 (한투, 삼성, IBK), 200–300% on 5, and another
brokerage's figure on several. So for a GO or WATCH deal the screen reads the
deal's final 투자설명서 from DART (before it is filed, the 증권신고서, which has the
same section as pre-pricing ranges), which prints every brokerage's tiers, and
applies yours as a percentage of that brokerage's own 100% base, in
`config.local.json`:

```json
{ "my_tier_pct": { "키움증권": 150 } }
```

The forecast, alert email and calendar events then use your real limit and
증거금, and say where it came from (`투자설명서`, with 38's figure when it differs).
When there is no 투자설명서 yet, or no DART key, a brokerage falls back to 38 and
to a separate key, a multiple of 38's low end:

```json
{ "my_limit_multiplier": { "키움증권": 1.5 } }
```

That fallback is labelled in every output, since 38's low end may be another tier.
If a 정정 changes your 청약한도 or the 확정공모가 after a GO alert, a follow-up email
says what moved, once per change, until the 청약 closes.
The backtest uses neither — it measures the screen at each brokerage's 100% tier,
not your account.

### Calendar events

With `calendar_sync` on, a new GO also gets two Google Calendar events:

| | when | alert |
|---|---|---|
| **청약** | last day of the subscription window, 10:00 | at 10:00 — some brokerages refuse a 청약 before then |
| **매도** | 상장일, 09:00 | 08:30 — when 장전 동시호가 opens and the 시초가 is still settable |

The 매도 timing is not cosmetic: the 시초가 is fixed at 09:00 from the orders
already in the auction, and 시초/공모 is the strategy `backtest.py` measures.

```bash
python3 run.py --test-calendar   # prove access without waiting for a GO
```

No setup beyond the Gmail app password email already uses. Google's CalDAV
endpoint accepts it, so there is no OAuth client, no Google Cloud project and no
consent screen — one `PUT` per event over stdlib `urllib`. Once `--test-calendar`
prints `OK`, set `"calendar_sync": true`.

Authenticating as the account owner matters: calendar reminders belong to
whoever sets them, so writing as you is what makes the 10:00 and 08:30 alarms
actually fire for you.

Event UIDs are deterministic (`ipo-radar-<no>-<slot>`), so a repeat write
replaces an event instead of adding a second one — duplication is impossible even
if `state/calendar.json` is lost, which a fresh checkout does.
`state/calendar.json` records which of the two events each deal already has, so
no redundant writes happen at all. A GO whose 상장일 has not published yet — common, it
arrives with the 확정공모가 — gets its 청약 event now and its 매도 event on a
later run.

## Calibration

`backtest.py` joins 수요예측결과 (knowable before subscribing) against 신규상장
(what actually happened), measuring **시초/공모** — subscribe at the offer price,
sell into the opening auction.

```bash
python3 backtest.py             # every listing since 2023-06-26; writes JSON + HTML, prints the summary
python3 backtest.py --no-write  # stdout only
open state/backtest/latest.html            # the visual report
```

It persists results to `state/backtest/`, dated plus a `latest` copy of each:

| File | What it's for |
|---|---|
| `latest.json` | Full dataset — every deal with its verdict, score, and the reasons behind it, plus all aggregates and the config used. Machine-readable, for re-analysis without re-scraping. |
| `latest.html` | Self-contained visual report — verdict comparison, a 기관경쟁률-vs-return scatter of every deal, win rate by bucket, and the full sortable table. Opens straight from disk, no server or network needed. |

`state/` is gitignored, so these are local artifacts. Commit one deliberately if you
want a versioned record of what justified a threshold change.

Over **257 non-SPAC deals, 2023-06 → 2026-11**:

| Verdict | n | win rate | mean | median |
|---|---:|---:|---:|---:|
| GO | 120 | 99% | +134% | +114% |
| WATCH | 78 | 78% | +60% | +38% |
| PASS | 59 | 58% | +23% | +4% |
| *(subscribe to everything)* | 257 | 83% | +86% | +69% |

Each signal is monotonic on its own:

| 기관경쟁률 | n | win rate | median |
|---|---:|---:|---:|
| 0–100 | 16 | 56% | +2% |
| 100–300 | 34 | 59% | +8% |
| 300–600 | 25 | 72% | +30% |
| 600–800 | 32 | 91% | +71% |
| 800–1200 | 107 | 90% | +83% |
| 1200+ | 43 | 98% | +116% |

| 확정공모가 vs 밴드 | n | win rate | median |
|---|---:|---:|---:|
| above band | 85 | 89% | +74% |
| at band top | 127 | 88% | +84% |
| **at band bottom** | 21 | **43%** | **+0%** |
| below band (repriced down) | 24 | 71% | +15% |

No deal in the sample priced strictly inside its band: every one off the top or bottom
was repriced *below* it. Exactly at the bottom is the weaker signal of the two.

`go_institutional_ratio` is set to **600** rather than 800: at 600 the screen takes
120 deals at a 99% win rate instead of 97 at 100%, at a median of +114% against +123%.
Since the binding constraint is 청약한도 per deal rather than the number of deals,
more shots on goal is the better trade.

### Read this before trusting those numbers

- **Regime dependence.** In June 2023 Korea widened the listing-day opening range to
  60–400% of 공모가. Every deal in this sample listed under those rules, and the
  +86% baseline reflects that. If the rules tighten, the base rate moves and the
  thresholds should be re-derived.
- **A high baseline flatters everything.** 83% of *all* deals were profitable at the
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

Over 255 deals (2023-06 to 2026-11), at each deal's 100% 청약한도 from its
투자설명서 and a 3%/yr opportunity cost:

| Bucket | n | 순손익 | 초과수익률¹ | 평균 배정 |
|---|---:|---:|---:|---:|
| **GO** | 120 | **+1,836만** | **+13.08%** | 17주 |
| WATCH | 78 | +438만 | +3.05% | 35주 |
| PASS | 57 | −8,357만 | −55.05% | 1,399주 |
| *전 종목* | 255 | **−6,084만** | −13.96% | 331주 |

¹ Won per won-year of capital committed, *on top of* the 3% it would have earned idle.

**Subscribing to everything loses money** — 84% of deals rose at the open, and the
strategy still ends 6,084만 down. The losses concentrate where competition was weak:
nobody wanted those deals, so you were filled with thousands of shares, and they fell.
엠앤씨솔루션 (비례 5:1) filled 6,807 shares at −11% — a 4,784만 loss on its own —
and 노머스 4,358 shares at −29%, 3,795만.

WATCH earns +3.05% a year *on top of* the 3% the cash would earn idle (+2.30% without
one lucky deal, 이에스티): better than a CMA, so worth subscribing with cash that is
idle anyway. It averages ~5.6만 a deal against GO's +13%, though, so it shows in the
report and never raises an alert.

### What is measured vs assumed

배정 is **read, not computed**, wherever DART has it. Every offering files a
증권발행실적보고서 after its 청약, with one table per brokerage giving every 청약
amount, its 청약건수, and the 균등 and 비례 shares each application received.
`ipo_radar/dart.py` reads that table at the deal's 청약한도: 252 of 255 deals exactly,
3 interpolated between neighbouring rows. Only where no filing can be read does
`net_returns.py` fall back to `floor(청약한도 / 비례경쟁률)` plus an assumed 균등, and
the row says so (`alloc_source`).

The 청약한도 is read from each deal's 투자설명서, not from 38.co.kr, whose figure is
a different tier from deal to deal (below). It needs a free OpenDART key in the login
keychain (service `ipo-radar-dart`, account `dart`); without one, both fall back to
38 and the formula, labelled. Before fees and tax; assumes one broker, no capital
reuse across overlapping deals.

## 기대손익 — the forecast

The buckets above are retrospective. For a deal that is open *right now*, the report
adds an expected net-won estimate: what this subscription is likely to pay, in won,
with an honest interval around it.

```
| 종목 | 투입증거금 | 예상배정 | 기대손익 | 결과 범위 (10~90%) | 평균 추정오차 | 흑자확률 |
| 인텔리빅스 | 27,900만 | 22주 | +46,912원 | -91,726원 ~ +268,281원 | +13,206원 ~ +78,079원 | 63% |
```

Two ranges, answering different questions. **결과 범위** is where *this deal's* result
should land 8 times in 10 — the analogues' own spread. **평균 추정오차** is the bootstrap
interval of the EV itself: how sure the average is, not where any one deal ends up.
Read as an outcome range it is badly wrong — none of the 13 deals listed 2026-08-13 to
10-22 finished inside it. Walk-forward over 143 deals at
기관경쟁률 ≥ 600, 결과 범위 held 80% of outcomes (nominal 80%), and 흑자확률 was 82%
against 84% realised.

At the moment 수요예측 publishes, two unknowns stand between a verdict and a cash
outcome: how hard *retail* will compete (which sets your allocation) and where the
stock opens. `forecast.py` estimates both by kernel-weighting past deals in
(log 기관경쟁률, 의무보유확약), and draws each analogue's retail-enthusiasm multiple
and opening move **jointly** — never averaging them separately, because their
correlation is the whole signal. A hot deal fills you with three shares; a cold one
fills you with eight hundred right before it falls.

### Rebuilding the analogue set

The forecast reads `state/cache/dataset.json`, which `net_returns.py` writes as a
by-product. `state/` is gitignored, so on a fresh checkout that file does not exist
and **the daily report simply omits the 기대손익 section** — the screen never depends
on it. To build or refresh it:

```bash
python3 backtest.py                # 1. 수요예측 features + realised 시초/공모
python3 net_returns.py --rate 3.0  # 2. joins detail pages -> dataset.json
python3 evaluate.py                # 3. optional: re-check the model walk-forward
```

Step 2 needs one detail page per deal, cached under `state/cache/detail/` — the first
run makes ~240 requests at 1s apart, later runs are nearly instant. The dataset is
**비례 only** regardless of `--subscribers`; `forecast.py` adds any 균등 assumption
itself, so baking one in would double-count it.

Worth re-running every few months. The daily screen keeps using whatever dataset is
on disk, so a stale one quietly scores new deals against an aging comparison set.

### Why it stays this simple

`evaluate.py` is the reason to trust any of it: walk-forward, time-ordered, and an
analogue counts only if its **listing** date precedes the target's *subscription*
date. A deal that had subscribed but not yet listed has no outcome to learn from.

Scored over the 177 deals every variant could predict (`python3 evaluate.py`,
2026-11-13):

| variant | MAE(만) | rank corr |
|---|---:|---:|
| hard-bin comparables *(the original)* | 61.8 | 0.277 |
| global median | 48.3 | 0.229 |
| **kernel, 2 features, h=0.6 — shipped** | **45.2** | **0.397** |
| kernel + 3개월 tenor share | 49.5 | 0.312 |
| kernel + band position | 46.7 | 0.295 |
| kernel + recency decay | 47.3 | 0.285 |

Hard bins were the worst thing tested — smooth weighting wins because it uses every
past deal rather than the handful that fall inside a bin. No third feature beat the
shipped model on either metric. Two features is not laziness, it is the measured
optimum at n=255; a third needs a walk-forward win, not a plausible story.

The bandwidth is deliberately *not* the argmax. MAE is flat across the whole sweep and
only rank correlation moves, monotonically, so the peak is the edge of a noisy curve —
0.6 rests on ~61 effective neighbours instead of ~31.

**Read this before trusting an EV:**

- **It is only calibrated at 기관경쟁률 ≥ 600.** There the bias is −0.4만 (n=143) and
  terciles are monotonic. Below 300 the bias is +262.6만 — weak demand means a huge
  allocation, and 37 historical examples cannot pin that tail down. Walk-forward below
  600, 결과 범위 held only 56% of outcomes and 흑자확률 ran 73% against 52% realised;
  엘리스그룹 (+112만 forecast, −81만 actual) and 인제니아 (+71만, −1,657만) are what that
  looks like. So below 600 the report shows the range alone and withholds EV and 흑자확률.
- **The same holds at ≥ 600 when 의무보유확약 is under 1%.** Over 23 such deals 결과 범위
  held 52% and 흑자확률 read 75% against 43% realised: retail often stays away, so the
  allocation runs far above the analogues'. EV and 흑자확률 are withheld there too.
- **The interval is the point.** For 니어스랩 the EV was −5,456원 against a CI of
  roughly ±100,000원. The point estimate alone would be a lie of precision. Show both
  ranges, and never label the bootstrap one as where the outcome will land.
- ~17 variants were scored against the same walk-forward sequence, which is selection
  on the holdout. That is why the bar for adopting one was "materially beats the
  incumbent", not "best marginal edge".

## Layout

```
run.py               entry point — screen, report, notify
backtest.py          threshold calibration against realised 시초/공모
net_returns.py       실질손익 — won earned net of opportunity cost; writes dataset.json
evaluate.py          walk-forward validation of the forecast model
config.json          thresholds, alert channels
config.local.json    machine-local overrides (alert address) — gitignored
ipo_radar/
  fetch.py           HTTP + EUC-KR decode, polite 1s delay, retries
  parse.py           38.co.kr parsers (label-keyed, not position-keyed)
  score.py           GO/WATCH/PASS rules
  forecast.py        expected net won from kernel-weighted analogues
  notify.py          alerts — macOS banner and/or email
  calendar_sync.py   Google Calendar 청약/매도 events for GO deals
  report.py          markdown rendering (daily screen)
  render_backtest.py HTML rendering (시초/공모 backtest)
  render_net.py      HTML rendering (실질손익 backtest)
state/
  latest.md          most recent report
  reports/           dated archive
  seen.json          dedupe — each deal alerts once
  backtest/          calibration output (JSON + HTML)
  cache/detail/      one 38.co.kr detail page per past deal
  cache/dataset.json the forecast's analogue set — rebuilt by net_returns.py
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
