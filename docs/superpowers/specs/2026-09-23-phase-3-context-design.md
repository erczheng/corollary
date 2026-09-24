# Phase 3: context

**Date:** 2026-09-23
**Status:** Draft, **unblocked 2026-09-24.** The seven questions the first
draft was blocked on (Q1–Q7) were put to the owner directly on 2026-09-24 and
answered; the answers are recorded under *Owner decisions* and folded into the
decisions below. Two of them differ from the draft's recommendation — Q1 (no
LLM sentiment tier in Phase 3) and Q6 (automatic re-promotion at 66%) — and the
spec is rewritten around them rather than annotated. One sample-size rule in
Q6 is a parent-session assumption the owner can override, and is labelled so.
**Branch:** the work branches from, and its pull requests target, **`master`**
(fast-forwarded to `d658602` on 2026-09-23; now the GitHub default and PR base).
**Scope:** The backend's context pipeline (`corollary/data/news/`,
`corollary/data/macro/`, new providers, `engine/scheduler.py`), notifications,
and the News page, Research's Market Pulse, and Settings' sentiment readout.
**Phase:** 3 — context. **No order reaches a broker, and nothing built here is
an input to anything that could place one.** The scanner that will read these
outputs is Phase 4's.

---

## Problem

PRD §11 defines Phase 3 in one line: *"News, calendar, sentiment composite,
social attention, analyst consensus. Self-audit job running."* It has no
done-criterion beyond the last sentence. Detail is spread across §7 (the source
table), §8.3 (News), §8.5 (Market Pulse), §8.7 (Settings' accuracy readout and
demotion banner), §9 (the sentiment tiers and the self-audit) and §10
(notifications).

Three things stand between here and a Phase 3 that says true things.

**The source table in PRD §7 names vendors for data they do not sell on the
plan this account holds.** Finnhub's own OpenAPI document marks the economic
calendar, ETF holdings and both dividends endpoints as premium — the same flag
it carries on `/news-sentiment`, which the 2026-09-11 probe found paywalled
behind a 502. Four of the composite's seven components have no named source at
all. See *Constraints*.

**The self-audit is the one Phase 3 deliverable that cannot be demonstrated on
the day it is built.** It grades labels against forward returns at 1h and 1d, so
a label is gradable a trading day after it is published at the earliest, and a
weekly job with a sample-size floor produces its first score that can act a week
or more after that. The owner chose no backfill (Q7) and no LLM tier (Q1), so the
audit starts empty and fills at the rate rules and Massive produce directional
labels. The done-criterion is written around that rather than pretending
otherwise. See *Done when*.

**The alert the self-audit fires has nowhere to go.** `engine/runtime.py` has a
`Notifier` protocol, a `Notification` value and a `LoggingNotifier` — so the
parent dispatch's *"no Notifier interface"* is not quite right — but there is no
`notification` table (Phase 2's Database section listed it; it never landed), no
route that serves one, no Discord sender, and the header bell still reads the
Zustand fixture store. **Rule 9's own critical alert has the same gap today**: a
dead-man's-switch halt reaches a structured log line and nothing else. That is
the most consequential thing in this spec, and it is not news-related at all.

---

## Constraints verified against the vendors

Same convention as the Phase 2 spec: each subsection says which kind of
verified. **Spec** means read from the vendor's published documentation or
OpenAPI document. **Probed** means observed today against the live service.
**No keyed probe was run in this session** — the planning agent cannot read
`.env` (rule 6's deny, by design) — so every keyed claim below is spec, and
step 0 of the order of work turns the ones that matter into probes. **Step 0
ran them on 2026-09-24** (`scripts/probe_phase3.py`, keys through the
launcher's `--env-file`, redacted fixtures under `tests/fixtures/*/p3_*.json`);
each result is marked **Probed, 2026-09-24** in the subsection it belongs to.

### Finnhub marks what it charges for, in its own OpenAPI document

**Spec, 2026-09-23.** `https://finnhub.io/static/swagger.json` carries a
per-operation `premium` field and a `freeTier` field. The flag is trustworthy on
the one endpoint where this project has ground truth: it marks `/news-sentiment`
*"Premium Access Required"*, and the 2026-09-11 probe found exactly that
endpoint paywalled.

| Endpoint | PRD use | `premium` | `freeTier` |
|---|---|---|---|
| `/calendar/economic` | §7 economic calendar | **Premium Access Required** | — |
| `/calendar/earnings` | §7 earnings calendar | — | *"1 month of historical earnings and new updates"* |
| `/etf/holdings` | §7 sector leaders | **Premium required.** | — |
| `/stock/dividend` | §8.3 dividends | **Premium Access Required** | — |
| `/stock/dividend2` | §8.3 dividends | **Premium required.** | — |
| `/stock/recommendation` | §7 analyst consensus | — | — |
| `/company-news` | §7 news | — | *"1 year of historical news and new updates"* |
| `/news` (market) | §8.3 `MARKET` items | — | — |
| `/news-sentiment` | §9 (removed tier) | **Premium Access Required** | — |
| `/stock/social-sentiment` | — | **Premium required.** | — |
| `/index/constituents` | — | **Premium Access Required** | — |
| `/stock/upgrade-downgrade` | — | **Premium Access Required** | — |

Consequences, in order of cost:

1. **PRD §7's economic calendar source does not exist on this plan.** The
   economic calendar's *"consensus, prior, actual"* is the premium half. See Q3.
2. **Sector leaders cannot come from an endpoint.** `/etf/holdings` is premium,
   and so is `/index/constituents`, which would have been the fallback for the
   breadth universe. Decision 6 uses a committed seed.
3. **Dividends need a different vendor.** Alpaca's corporate actions endpoint is
   the candidate; whether it returns *announced* future ex-dates is unverified.
4. What is free is exactly what Phase 3 needs from Finnhub apart from the above:
   company news, market news, the earnings calendar and recommendation trends.

`EarningRelease` is `{symbol, date, hour, year, quarter, epsEstimate, epsActual,
revenueEstimate, revenueActual}` — `hour` is a session code, not a time (see
*Not verified*). `RecommendationTrend` is `{symbol, buy, hold, period, sell,
strongBuy, strongSell}`: counts of analysts, not percentages, which
`SectorConsensus` in `types.ts` currently models as percentages summing to 100.

**Probed, 2026-09-24, against this project's key.**

- **The premium flags hold, but the failure is a 403, not a 502.**
  `/calendar/economic`, `/etf/holdings`, `/stock/dividend` and
  `/stock/dividend2` each answered **HTTP 403, `application/json`**,
  `{"error":"You don't have access to this resource."}` — not the HTML 502 PRD
  §9 recorded for `/news-sentiment`. A client must read a JSON 403 as "not on
  this plan", distinct from a transport failure.
- **`/calendar/earnings` returns upcoming dates on the free tier.** Today → +21
  days: 295 rows, every one dated on or after today (2026-09-24 → 2026-10-15).
  **`hour` is empty on most rows**: `""` 242, `bmo` 32, `amc` 21 — 82% carry no
  session at all, and no `dmh` was seen. An earnings row states its date, and
  its session only when the vendor has one.
- `/stock/recommendation` (AAPL): 4 monthly periods, integer counts, exactly
  the `RecommendationTrend` shape above.
- **No index quote on the free tier.** `/quote?symbol=^VIX` and `^GSPC` answer
  **200** with `{"error":"Market data subscription required for CFD
  indices."}`; `VIX` answers 200 with an **all-zero quote** (`c: 0, t: 0`) — an
  unknown symbol, not a price. Market Pulse gets no intraday VIX from Finnhub,
  and a zero quote must read as absent, never as a VIX of 0.
- **`/company-news` caps a response at ~250 rows** (248–250 observed for NVDA,
  MSFT, META, AAPL and LLY over 14 days; NVDA alone had 1,261 articles in that
  window). A request that returns the cap is truncated and must be split by
  date.

### Alpaca news: Benzinga only, polled from the data host

**Spec.** `GET https://data.alpaca.markets/v1beta1/news` — `symbols`, `start`,
`end`, `limit` (1–50), `sort`, `include_content`, `exclude_contentless`,
`page_token`. Fields `id, headline, author, created_at, updated_at, summary,
content, images, symbols, source, url` — the same list PRD §9's 2026-09-11 probe
observed, with no sentiment field. History reaches back to 2015, about 130
articles a day, *"all news data is currently provided directly by Benzinga"*.
The reference page states a 100/min limit; it sits on the `data.` host, so the
Phase 2 `HostRateLimiter` bucket for that host meters it regardless of which
number is true.

Alpaca also offers a news **websocket**. Decision 2 declines it.

**Probed, 2026-09-24.** 200, top-level `{news, next_page_token}`, article keys
exactly the list above. Over ten sessions on the 66-name watch universe it
returned 1,564 articles in 32 pages of 50, with no 429.

### Alpaca corporate actions: the fields exist; future dates are undocumented

**Spec.** `GET https://data.alpaca.markets/v1/corporate-actions` — `symbols`,
`types` (including `cash_dividend`), `start`, `end` (dates), `limit` (≤1000),
`page_token`, `sort`. `cash_dividends[]` carries `ex_date` (required),
`record_date`, `payable_date`, `rate`, `special`, `process_date`. The reference
says *"Alpaca has no guarantees on the creation time of corporate actions"* and
**does not say whether an announced dividend appears before its ex-date**. A
calendar that is *"forward-looking only"* (§8.3) needs exactly that. Step 0.

**Probed, 2026-09-24.** `types=cash_dividend`, `start`=today, `end`=today+60d:

- **Announced dividends do appear before their ex-date.** 1,079 rows had an
  `ex_date` strictly after today: median **7 days** ahead, maximum **50 days**,
  44% within the first week. 23 watch-universe names were among them
  (including SPY, QQQ, NVDA, META, JPM). Decision 7's condition is met.
  **The horizon figures are censored by the query window, not a measure of
  how far ahead Alpaca publishes.** The query stopped at `end`=today+60d, so
  every row returned had its window date inside +60 days, which caps the
  visible `ex_date` at roughly 60 days less the ex-to-pay gap. The median of 7
  and the maximum of 50 are both bounded by that; they do not show that a
  dividend rarely surfaces more than five weeks out. Step 7 sizes its query
  window by re-probing with a longer `end`, not from these figures.
- **The `start`/`end` window does not filter on `ex_date`.** 3,580 rows came
  back, most of them with an `ex_date` *before* `start`, and their
  process/payable dates inside the window — the filter appears to be on one of
  those. Filter on `ex_date` client-side.
- **`rate` is a JSON number** (`0.255`), not a string. Parse it from the raw
  text to `Decimal` (`parse_float=Decimal`); never let it pass through a float.
- **The step 0 fixtures are not byte-exact.** Every file the probe recorded
  under `tests/fixtures/` was re-serialised from parsed JSON, so its numbers
  have already been through a float. A test of `parse_float=Decimal` must
  record the raw response text rather than rely on these files for exact
  decimal bytes.

### Alpaca serves no index

**Spec.** Nothing in the `llms.txt` index covers indexes, VIX or SPX. Carried
from the Phase 2 work: the equity endpoints serve ETFs (SPY, TLT, the SPDR
sector funds), not the indexes behind them.

### Alpaca snapshots: `indicative` volume is real session volume, dated

**Probed, 2026-09-24.**

- **Option snapshot, SPY, ≤10 DTE, `feed` from `ALPACA_OPTIONS_FEED`:** one page
  of 1,000 contracts (it paginates — `next_page_token` was set). 896 carry a
  `dailyBar`, every one with `v > 0`, 10.9M contracts in total.
  **`prevDailyBar.v` equalled `/v1beta1/options/bars` 1Day volume exactly on
  5/5 of the most active contracts** (2026-09-22), so it is the same session
  volume the historical bars carry, not an artefact of the indicative feed.
- **But `dailyBar` is the contract's last *traded* session, not today's.** 697
  of the 896 were dated 2026-09-23, the last session. The other 199 were dated
  anywhere from 2026-08-11 to 2026-09-22. A put/call proxy that sums
  `dailyBar.v` without filtering on `dailyBar.t` mixes stale sessions into
  today's ratio.
- **`/v2/stocks/snapshots` shows no cap on `symbols` up to 1,000.** Lists of
  100, 500 and 1,000 all answered 200, returning 93, 476 and 931 entries. The
  cause of the shortfall is **inferred, not verified**: the script recorded
  only counts, not which symbols were missing. Symbols Alpaca does not list
  would explain it, but with `feed=iex` a listed symbol with no IEX trade
  could be missing too, and so could truncation.

### Massive news carries a per-ticker sentiment and a reason

**Spec, plus PRD §9's probe of 2026-09-12.** `GET /v2/reference/news` —
`ticker`, `published_utc.gte`/`.lte`, `limit` (≤1000), `order`, `sort`.
`results[].insights[]` holds `{ticker, sentiment, sentiment_reasoning}`;
`publisher{name, …}`, `tickers[]`, `published_utc`, `article_url`. The docs say
news is *"updated hourly"* and that the free Stocks plan carries **2 years** of
history. The PRD's probe verified **5 requests/minute** (a 429 on the fifth
call). A single untickered request with `limit=1000` returns every recent
article across all tickers, which is what makes 5/min enough for a feed.

**Probed, 2026-09-24.**

- Untickered `limit=1000`: 200, 1,000 articles spanning **5.3 days**, 1,246
  distinct tickers. That is about 190 articles a day (2,663 over 14 calendar
  days), so one call covers several days. `Authorization: Bearer` works, which
  keeps the key out of the URL, and `next_url` is a cursor carrying no key.
  Four calls 13s apart drew no 429.
- **`sentiment` takes four values, not three:** `positive`, `neutral`,
  `negative` and **`mixed`**. `mixed` is rare (3 of 7,843 insights over ten
  sessions) but real, and it is neither directional value. The mapping has to
  say so explicitly. **The recorded Massive fixture contains no `mixed`
  insight**, so nothing on disk exercises it yet. Step 5 must record a fixture
  that contains one.
- Insight keys are exactly `{ticker, sentiment, sentiment_reasoning}`.

### StockTwits: keyless, working, and noisier than the PRD assumes

**Probed, 2026-09-23, no credentials.**

- `GET https://api.stocktwits.com/api/2/streams/symbol/AAPL.json` → **200**,
  JSON, top-level `symbol, cursor, regions, filters, messages, response`.
  `messages` holds **30** items; `cursor` is `{more, since, max}`. A message's
  user label is at `entities.sentiment` (`"Bullish"` / `"Bearish"` / null).
- **6 of 30 messages carried a label** — 20%, below the PRD's *"roughly
  30–50%"*. One sample on one symbol overnight; recorded, not generalised.
- 30 AAPL messages spanned **~4 hours overnight**. A busy ticker intraday will
  exceed 30 per poll, which is what `cursor.more` reports.
- **No rate-limit headers** on the response. PRD §7's *"200 req/hr per IP"*
  cannot be confirmed from the response itself.
- `GET /api/2/trending/symbols.json` → **200**, 30 symbols, each with
  `trending_score`, `sector`, and a machine-written `trends.summary`.
- Each message's `symbols[]` entries carry `sentiment_change` and
  `volume_change` — StockTwits' own derived figures, with no published
  definition. Decision 10 declines them.

**Probed, 2026-09-24 — one hour at the planned cadence.** 180 requests, one
every 20s, round-robin across the 26 Markets symbols, `since=<newest id seen>`,
from 04:36:34 to 05:36:34 UTC, with one `httpx` client keeping its cookies and
the default `python-httpx/0.28.1` user agent:

- **All 180 answered 200 JSON.** No 429, no 403, no HTML, no `cf-mitigated`, no
  challenge.
- **Still no rate-limit headers.** The only `cf-` headers were `cf-ray` and
  `cf-cache-status`. Cloudflare set `__cf_bm` four times across the hour
  (refreshed about every 30 minutes) and `_cfuvid` once. The limit and its key
  (IP or cookie) stay unconfirmed from the response side, but 180/hour drew
  nothing.
- **41.6% of messages were labelled** (371 of 892), inside the PRD's
  "roughly 30–50%", against 20% for the single 2026-09-23 sample.
- **Overnight caveat:** the hour was 00:36–01:36 ET. After the first round only
  112 new messages arrived across 154 polls, so this measured the cadence
  against the limit, not intraday message volume. `cursor.more` was true only
  on the first, cursorless round.

### FRED

**Spec.**

- **120 requests/minute**, 429 beyond (FRED's errors page).
- `VIXCLS` is the **daily close**, published the next morning — the 2026-09-22
  close was published 2026-09-23 at 08:37 CT. A FRED VIX is always yesterday's.
- `BAMLH0A0HYM2`: *"Starting in April 2026, this series will only include 3
  years of observations"*, and ICE's notice reads *"Reproduction of this data in
  any form is prohibited except with the prior written permission"*. Three
  years covers a 252-session z-score window with room. A single-user local
  terminal displaying a derived score is not reproduction in the redistribution
  sense, but the raw series must not be exported (no CSV of it).
- `/fred/releases/dates` returns future scheduled release dates when
  `include_release_dates_with_no_data=true`, and **returns dates only, never a
  time of day**.
- `DGS3MO` is the 3-month bill, which `pricing/blackscholes.py` already names as
  the replacement for its placeholder `DEFAULT_RISK_FREE_RATE = 0.0425`.

**Probed, 2026-09-24.**

- `VIXCLS`, `BAMLH0A0HYM2` and `DGS3MO` all answered 200. Fetched at 04:37 UTC
  on 2026-09-24, the latest observation of each was **2026-09-22** (14.21, 2.68,
  4.16). The 2026-09-23 close was not yet published, consistent with the
  next-morning publication above. Values arrive as strings, so they parse
  straight to `Decimal`, with one exception: FRED marks a missing observation
  with the string `"."`, which means *no observation* and raises in
  `Decimal(".")`. It must be handled before the `Decimal` conversion, not
  caught after it. None appears in these fixtures. `DGS3MO` feeds step 3's
  risk-free rate, so the gap has to be handled there.
- **`/releases/dates` returns dates only, confirmed.** With
  `include_release_dates_with_no_data=true`, today → +30d gave 842 rows, every
  `date` in `YYYY-MM-DD` form. Row keys are `{date, release_id,
  release_last_updated, release_name}`. `release_last_updated` is a timestamp
  of the last revision, not a scheduled time.
- **FRED's key has no header form.** It travels in the query string, so every
  FRED URL carries the key, and nothing may log one unredacted, exception
  messages included (`httpx` errors quote the request URL).

### Not verified

Carried forward as implementation-time checks, most of them step 0. Step 0 ran
on 2026-09-24. What it resolved is struck through here, with the result moved to
its subsection above. What remains is left standing, with a note on what is
still open.

- ~~**Every Finnhub premium/free flag above, against this project's key.**~~
  Resolved: the flags hold, and the premium answer is a JSON 403, not an HTML
  502. See the Finnhub subsection.
- ~~Whether Finnhub's free `/quote` answers for `^VIX` or any index symbol.~~
  Resolved: it does not ("subscription required for CFD indices"). See the
  Finnhub subsection.
- ~~Whether `/calendar/earnings` returns **upcoming** dates on the free tier,
  and what `hour` holds.~~ Resolved: upcoming dates yes; `hour` is empty on 82%
  of rows and otherwise `bmo`/`amc`. See the Finnhub subsection.
- ~~Whether Alpaca corporate actions returns announced dividends before
  `ex_date`, and how far ahead.~~ Resolved: yes. How far ahead is still open:
  the median of 7 and maximum of 50 days are censored by the probe's +60-day
  window, and step 7 re-probes with a longer `end`. The window does not filter
  on `ex_date`. See the corporate actions subsection.
- ~~StockTwits' real rate limit, whether it is per IP or per session cookie, and
  whether a scheduled poll draws a challenge.~~ Resolved as far as a keyless
  client can see it: an hour at 180/hour drew no 429 and no challenge. The
  response still carries no rate-limit header, so the limit itself and its key
  remain unpublished. See the StockTwits subsection.
  **Still open, a human read: StockTwits' terms of use for automated access to
  the public endpoints.** Decision 15 of the Phase 2 spec is the precedent:
  CBOE's data was free, reachable and forbidden to automate. Terms page:
  `https://stocktwits.com/about/legal/terms/`. No separate API or developer
  terms page is linked from it. Not read for a legal conclusion by any agent.
- ~~Whether `dailyBar.v` on the `indicative` option snapshot is real session
  volume.~~ Resolved: yes, it matches historical bars exactly, but `dailyBar`
  is the last *traded* session, so it must be filtered on `dailyBar.t`. See the
  Alpaca snapshots subsection.
- ~~Whether Massive's `sentiment` takes values beyond
  `positive`/`neutral`/`negative`.~~ Resolved: it also takes `mixed`. See the
  Massive subsection. **Still open, a human read: Massive's terms for the free
  tier.** Terms pages: `https://massive.com/legal/terms-of-service`, with
  `https://massive.com/legal/individuals-terms-of-service` and
  `https://massive.com/legal/website-terms-of-service` linked from it. No
  separate market-data or API acceptable-use page was found. Not read for a
  legal conclusion by any agent.
- ~~**How many directional labels rules and Massive actually produce per
  session on the watch universe, and what share of Massive's insights are
  `neutral`.**~~ Measured over ten sessions: see *Done when*, where the
  measurement replaces the estimates. The rules figure is still an
  **estimate** from draft patterns, and step 5 re-measures it with the real
  ones.
- ~~Whether `/v2/stocks/snapshots` caps the `symbols` list.~~ Resolved: no cap
  up to 1,000, though the cause of the missing entries is inferred, not
  verified. See the Alpaca snapshots subsection.

---

## Owner decisions — 2026-09-24

The first draft of this spec was blocked on seven questions. The owner answered
all seven directly on 2026-09-24, via the coordinating session. **These are
owner decisions, not assumptions**, and they are the reason the decisions below
read as they do. Where the owner chose something other than the draft's
recommendation, the draft's option is kept here in one line so the reasoning is
not re-run. Q5 and Q6 are the two that reach Phase 4's order path, because they
define when news sentiment is, and stops being, a scanner input.

**Q1 — Sentiment sources: rules + Massive. No LLM tier in Phase 3.**
*Not* the draft's recommendation, which was rules + LLM + Massive. The LLM
sentiment tier arrives with Phase 4's LLM layer, where §8.5 already puts the
live model. Carried through as decisions 4, 17 and 18: two sources, displayed
rules-first; `SentimentTier` becomes `'rules' | 'vendor'`; `ANTHROPIC_API_KEY`
is unused in Phase 3.

**The cost of this answer, stated so nobody is surprised by it later:** fewer
labels. Rules fire only on high-signal event patterns, and Massive labels only
its own articles (Zacks, The Motley Fool and similar — not the Benzinga copies
Alpaca serves). So **most headlines in the feed will read `Unclassified`**, and
the audit fills at the rate those two sources produce *directional* labels on
the watch universe. Combined with Q7's no-backfill, that is what sets the
timings in *Done when*.

**Q2 — The composite: all seven components, via proxies.** As recommended.
Momentum = SPY vs its 125-session MA (Alpaca SIP daily); strength = 52-week
highs minus lows and breadth = advancing minus declining volume, both over the
~500-name S&P 500 universe from decision 6's seed, from SIP daily bars;
volatility = FRED `VIXCLS` vs its 50-session MA; safe haven = 20-session SPY
return minus TLT return (Alpaca); junk = FRED `BAMLH0A0HYM2`; put/call = SPY
chain put/call volume from Alpaca's EOD option snapshots, **accumulated
forward** and shown as *forming* for its first 60 sessions, since Alpaca has no
historical option volume short of Phase 5's bulk download. Every proxy is
labelled as one, and as a proxy of what, on hover.

**Q3 — Economic calendar: FRED release dates + a hand-kept times table;
consensus shown unavailable.** As recommended. FRED `/releases/dates` supplies
the dates automatically; a committed per-release ET time table (~15 rows)
supplies the times; prior and actual come from FRED after release. Finnhub's
economic calendar is premium and is not bought.

**Q4 — Notifications: the `notification` table, the bell API, a Discord
sender, and rule 9's halt alert routed through them.** As recommended.
Decision 14.

**Q5 — A label is correct when its sign matches the ticker's return in excess
of SPY's over the same window; neutral labels are excluded from the accuracy
fraction and reported as a count.** As recommended. `MARKET` labels grade
against SPY's raw return, since SPY's excess over itself is zero.

**Q6 — Demotion is per source, with automatic re-promotion at a higher
threshold.** The owner's own answer, verbatim: *"why is 52% the threshold? lets
do it per source with auto re-promote at a different threshold: 66%"*. *Not*
the draft's recommendation, which made re-promotion a human action.

On *"why 52%"* — the answer given to the owner, recorded here: it is PRD §9's
figure. Random directional labels score 50%, and 52% is a thin margin above
coin-flip. **It has no empirical derivation** — no study, no backtest, no
calibration behind it. It is kept as the floor because the owner kept it, not
because anything proved it.

The rule, written out in decision 13: per source (rules and Massive
independently); **demote** when accuracy is below 52% in *either* window;
**re-promote automatically** when accuracy is at or above 66% in *both*
windows. Between 52% and 66% is a dead band in which a source stays in
whichever state it is already in. Both transitions are audit-logged and both
notify.

**Sample floors — a parent-session assumption, not an owner decision. The owner
can override it.** Demotion requires ≥30 scored labels in the window over the
trailing 28 days (the draft's rule, unchanged). Re-promotion requires **≥100**
scored labels in each window. Reasoning, computed as exact binomial tails and
re-checked when this was written:

| Scored labels | True accuracy | P(scores < 52% → demoted) | P(scores ≥ 66% → re-promoted) |
|---|---|---|---|
| 30 | 50% (coin flip) | 57.2% | **4.9%** |
| 30 | 55% | **35.5%** | 13.5% |
| 30 | 60% | 17.5% | 29.2% |
| 30 | 65% | 6.5% | 50.8% |
| 100 | 50% (coin flip) | 61.8% | **0.09%** |
| 100 | 55% | 24.0% | 1.7% |
| 100 | 60% | **4.2%** | 13.0% |
| 100 | 65% | 0.3% | 46.2% |

Standard error near 50% is ±9.1 points at n=30 and ±5.0 at n=100. At n=30 a
coin-flip source clears 66% on 4.9% of checks, which over 52 weekly checks
would be ~93% likely to happen at least once if the checks were independent.
They are not — trailing windows overlap and the two windows share labels — so
93% is an upper bound, but it is the right order of magnitude to reject n=30 for
re-promotion. At n=100 the same source clears 66% on under 0.1% of checks.

Each row is one window. Demotion fires on *either* window, which makes it
somewhat more likely than the table shows; re-promotion needs *both*, which
makes it somewhat less likely. The two windows are graded on the same labels,
so the effect is smaller than two independent draws would suggest.

**Known risk, recorded rather than changed:** the 52% floor at n=30 will demote
genuinely useful sources often. A source that is truly right 55% of the time —
better than a coin, and plausibly worth reading — is demoted on about a third of
the checks it faces. The hysteresis then makes the demotion sticky: a truly-60%
source re-qualifies on only 13% of checks at n=100. That is the conservative
direction for a scanner input, and it is what the owner chose; it is written
down so that a sentiment source sitting demoted for months is read as the rule
working, not as a bug.

**Q7 — No backfill.** The audit starts empty. How long that takes to become a
figure that can act is in *Done when*.

---

## Decisions

Taken by this spec on evidence, on a stated default, or on an owner decision
above — each one that rests on an owner decision names it. Each records what it
rules out.

### 1. Context jobs run in the engine's scheduler, in the one process — and are never a rule-9 producer

`engine/scheduler.py` stops being a stub. It becomes the asyncio task set,
started in the FastAPI lifespan beside the sockets and the watchdog, that runs
every Phase 3 job on a market-calendar clock (`corollary/calendars.py`, never a
hardcoded 09:30–16:00). Phase 4's pre-market build and 15-minute refresh are
added to it later; the docstring's two Phase 4 items stay Phase 4's.

**No context job calls `EngineRuntime.record_message` or any watchdog input,
and no context failure halts the engine.** A news vendor being down is not an
Alpaca connection loss, and a halt log full of *"Finnhub 502"* is the log
nobody reads the morning it matters — the same argument CLAUDE.md makes for
`dev_app`. This holds even for the Alpaca news and corporate-actions calls,
which reach the same vendor: the watchdog's producers are the three vendor
sockets and nothing else, per `routes/ws.py`'s documented rule. A test pins it.

A context job failure is logged with its rule and inputs (rule 8's spirit) and
surfaced on the page it feeds as *stale since HH:MM ET* — never as silence.

Rejected: a separate context-worker process. It buys crash isolation for jobs
that cannot place an order, and costs the IPC the one-process decision (PRD §12)
already declined. Also rejected: running jobs on request from the API — the
self-audit's weekly clock and the composite's EOD computation have no request
to hang off.

### 2. News is polled, never streamed

Alpaca's news websocket would be a fourth vendor socket. Every socket in this
process is a rule 9 input by construction (`engine/sockets.py`), and a news
socket dropping must not halt trading; carving an exception into the watchdog
is exactly the sort of special case rule 9 is weakest against. Headlines are
not quotes: *"a headline published at 10:04 is the same headline at 10:05"*
(`News.tsx`'s own comment on its 15s poll).

Rejected: the news websocket with a watchdog exemption. Also rejected: polling
the websocket's REST twin at quote-like cadence — see *Feeds and budgets*.

### 3. One article store across vendors, deduplicated, with provenance kept

Every vendor's article lands in `news_article`, keyed `(vendor, vendor_id)`, so
re-polling is idempotent. Cross-vendor duplicates — Benzinga on Alpaca and the
same story via Finnhub — link to one canonical row by normalised URL, falling
back to normalised headline within ±10 minutes. The feed shows the canonical
row once, naming its publisher; the duplicates remain, because a Massive label
arrives on the Massive copy and must still reach the article.

Tickers are a child table (`news_article_ticker`), because Massive tags one
story to eight symbols and labels each separately. `MARKET` is a ticker value,
per `types.ts`.

**Sector for filtering** comes from decision 6's seed (ticker → SPDR sector);
a ticker outside the seed files under `Other`; `MARKET` under `Macro`
(`MACRO_SECTOR`, already in `types.ts`).

**Retention: keep everything.** ~500 articles a day is ~180k rows a year,
trivial for SQLite, and the audit needs the old labels. The UI's lookbacks
(`today` / `3d` / `1w` / `2w` / `all`, in `lib/news.ts`) read from the store.

Rejected: dedupe on headline alone (wire services retitle), and keeping only
the canonical row (orphans vendor labels).

### 4. Two sources label independently, rules first; the audit grades both

Owner decision Q1: rules and Massive, no LLM tier.

A headline in the watch universe (decision 12) is labelled by every source that
can reach it — the rules tier on every headline, Massive on the articles it
carries an `insights[]` entry for — and each label is its own `sentiment_label`
row. The feed displays one, by precedence **rules → vendor**: a deterministic
match on *"guidance cut"* outranks a vendor's read of the same story. The
self-audit grades every row, which is what produces §9's *"accuracy per source
and per tier"* rather than accuracy for whichever label happened to win display.

**What `Unclassified` means in Phase 3.** §9's confidence gating (*"publishes a
direction only above a confidence threshold. Below it, the item displays as
`Unclassified`"*) is written for the LLM tier, and neither Phase 3 source has a
confidence to gate: a rule either matches or does not, and Massive ships no
score. So in Phase 3 **`Unclassified` means no source labelled the item for
that ticker** — no rule matched the headline, and Massive carried no insight for
that ticker on that article (which is the usual case for Alpaca's Benzinga
copies and Finnhub's aggregation, since Massive labels only its own
publishers). A Massive `neutral` is a label and displays `Neutral`, not
`Unclassified`; it is excluded from accuracy per Q5, and counted. Confidence
gating returns with the LLM tier in Phase 4 (decision 18). `Unclassified` is
never graded, and renders in `caution`, not `error`, as the fixtures'
`SENTIMENT_CLASS` already does.

Expect most of the feed to read `Unclassified`. That is the honest outcome of
two narrow sources, and §9's own rule — *"silence beats a wrong label"* — is
the reason it is acceptable.

Rejected: a cascade that stops at the first source to answer, which is how §9's
*"in order"* reads. It would grade Massive only on stories rules had not already
caught — the hard remainder — so the two accuracies would not be comparable.
This matters more, not less, when the LLM joins in Phase 4.

### 5. The composite stores raw inputs and derives everything else; it is computed once a day

`composite_component_value(date, component, raw_value, source)` holds each
component's **raw** daily value — SPY's distance from its 125-session MA, the
highs-minus-lows count, and so on — loaded from price history on first run
where the source has history (market prices, not labels; Q7's no-backfill
governs the sentiment audit only). The z-scores, the 0–100 component scores and the
composite are **derived on read**, never stored. That is §8.5's requirement
(*"derived from its seven components, never a stored copy, so it cannot
disagree with the breakdown"*) applied one level further down: a stored score
can drift from the window it was computed over.

- **Window:** trailing 252 sessions. A component with fewer than **60**
  observations is *forming*, shown with its count, and excluded.
- **Score:** Φ(z) × 100, each component oriented so that higher means greed
  (VIX above its MA is fear, so it enters negated). Bounded and monotone, with
  no clipping constant to argue about.
- **Composite:** the mean of the components not forming, with *"6 of 7
  components"* printed beside it whenever it is not seven. A composite over
  fewer inputs is a different claim and has to say so.
- **Cadence: once a day, after the close,** stamped *as of* that session. The
  inputs are daily closes (FRED publishes VIX the next morning), and on the
  Basic plan the only intraday equity data is IEX, which is 2.5% of volume — an
  intraday breadth reading over it would mean nothing. Revisit at Phase 4's
  plan upgrade. Not asked as a question because nothing on this plan could make
  intraday honest.

Which components exist, and from where, is owner decision Q2.

Rejected: storing the composite (drifts from its breakdown); a fixed z clip at
±3 rescaled linearly (an arbitrary constant with a visible kink at the ends);
renormalising weights silently when a component is missing.

### 6. Sector membership and leaders come from a committed seed of SPDR holdings

`/etf/holdings` and `/index/constituents` are premium. State Street publishes
each Select Sector SPDR's full holdings, with weights, as a downloadable file.
Phase 3 commits one CSV built from the eleven funds' files —
`corollary/data/seeds/spdr_holdings.csv`, columns `etf, sector, symbol, weight`,
with the as-of date in a header row — **refreshed by hand**, quarterly, via a
small script the owner runs against files the owner downloaded.

One seed does three jobs: ticker → sector for the news filter, the ~500-name
universe for Q2's breadth and strength components (the eleven funds together
are the S&P 500), and the consensus roll-up below.

**Consensus: the top five holdings by weight in each fund, weighted by weight.**
§7 says *"top holding"* (singular) and §8.3 *"largest constituents"* (plural).
Plural is right: XLK's top holding alone would make *"Technology"* a single
analyst consensus on one company. Five per fund is 55 Finnhub calls, refreshed
weekly, spread across a minute of a 60/min bucket. PRD §7 is amended to match.
Not asked, because top-1 and top-5 differ by a constant.

The panel shows the seed's as-of date beside the consensus as-of date, and
warns once the seed is more than 100 days old.

Rejected: Finnhub premium for this alone; a scheduled scrape of State Street's
site (terms not checked, and the Phase 2 CBOE finding is the reason not to
assume); a hardcoded Python list (the as-of date gets lost, and a list in code
reads as permanent).

### 7. Earnings from Finnhub; dividends from Alpaca if announced dates exist

The free earnings calendar supplies earnings for the watch universe, with
estimate and actual. Its `hour` field is a session (`bmo`/`amc`/`dmh`), not a
time: the row renders *"Before open"* / *"After close"* rather than inventing a
07:00 — a placeholder time is how a calendar row reads *"7:00 PM"* for
something that never had one, per `CalendarEvent.at`'s own comment.

Dividends come from Alpaca corporate actions **if step 0 shows announced
ex-dates arrive before the ex-date**. If they arrive only at or after it,
dividends leave the forward-looking calendar and the legend says why. A
dividend is date-only (`at: null`).

Rejected: Finnhub's dividend endpoints (premium); deriving dividends from price
gaps (after the fact, which is useless forward).

### 8. Central-bank dates are a committed seed per year

PRD §7 already says it: *"seeded annually — Fed/ECB/BoE/BoJ publish years
ahead"*. `corollary/data/seeds/central_banks_2026.csv` and `_2027.csv`, with ET
times where the bank publishes one (FOMC statements at 14:00 ET). A calendar
with no seed for the coming year says so on the panel rather than going quietly
empty on 1 January.

### 9. Geopolitical events are entered by hand on the News calendar, and are not config

A small add/edit/remove form on the calendar panel, writing
`calendar_event` rows with `source = 'manual'`. Removal is a soft delete. The
rows carry `created_at`/`updated_at`.

**Not in the configuration audit log.** That log answers *"did anything that
governs the engine change before the bad day"* (§8.7); a note that a summit is
on Thursday governs nothing. Putting it there would dilute the log it is for.

Rejected: entry in Settings (the calendar is where you look when you want to
know what is on Thursday); audit-logging it.

### 10. Social attention counts messages itself, on a small watch set

For each symbol in the social watch set — the Markets universe (26) plus open
position underlyings — the job polls the symbol stream with `since=<cursor>`,
counts new messages and labelled ones per Eastern session, and pages while
`cursor.more` is true until the per-symbol share of the hourly budget is spent.
A session whose count hit the budget is flagged `truncated` and says so.

The 30-day baseline is the mean of the preceding 30 sessions' counts. **It
forms over the first 30 sessions and says so** (*"baseline forming — 12 of 30
sessions"*); velocity is not printed until then. Sentiment is aggregated only
over labelled messages, and the labelled count is shown, as §8.3 requires — the
probe's 6-of-30 is the reason that count is not decoration.

Context only, never a trigger: nothing in Phase 3 or the Phase 4 plan reads it
as an input.

Rejected: StockTwits' `volume_change`/`sentiment_change` (undocumented
definitions — a number we cannot explain is not one we should print as a
velocity); `trending_score` (a ranking, not a rate); backfilling the baseline
by paging backwards (~hundreds of requests per busy symbol against 200/hr).

### 11. Analyst consensus is Finnhub's latest period, refreshed weekly

`/stock/recommendation` returns monthly periods. The job keeps the latest per
symbol and refreshes weekly (monthly source, weekly check, so a new month is
picked up within a week). The panel shows counts rolled into buy / hold / sell
(`strongBuy` into buy, `strongSell` into sell) and the period as-of.
`SectorConsensus`'s percentages are derived from counts at the API, not stored.

### 12. The self-audit's mechanics

**What is graded.** Every directional label (`bullish` / `bearish`) on a ticker
in the **watch universe**: the Markets universe ∪ open-position underlyings ∪
decision 6's sector leaders ∪ `MARKET`. `neutral` labels are graded for the
record and excluded from accuracy, with their count reported (owner decision
Q5). Labels outside the watch universe are stored and displayed but not graded —
grading every ticker a vendor tags would mean minute bars for hundreds of
symbols a week, which is the Phase 2 bars-page arithmetic at a scale that
collides with the Markets poll.

**What is correct** (owner decision Q5). A label is correct when its sign
matches the sign of the ticker's return **in excess of SPY's** over the same
window. `MARKET` labels grade against SPY's raw return. Scoring against raw
return would credit beta — in a rising week every bullish label looks right —
and the floor would then be measuring the market, not the source.

**The reference price** is the last SIP trade at or before publication if it was
published during the regular session; otherwise the next regular-session open,
which is the first price anyone could have acted on. SPY's reference is taken at
the same instant.

**The windows are in regular-session minutes**, from `calendars.py`: **1h** is
the reference plus 60 session minutes; **1d** is the reference plus 390 session
minutes. A label published at 15:30 has its 1h window end at 10:00 the next
session, not at 16:30 in an empty market. Half-days are real, and a session-minute
clock is the only definition that treats them correctly.

**Prices** come from Alpaca SIP historical minute bars, free because every
window is more than 15 minutes old when graded (CLAUDE.md: historical is SIP
even on Basic).

**Schedule.** Saturday 10:00 ET, plus `POST /api/settings/sentiment/audit`
limited to once an hour. Each run grades every label whose window has matured
and was not yet graded, writes the outcome, computes each source × window's
accuracy, and then applies decision 13's transitions. A Friday afternoon
label's 1d window ends Monday, so it is graded the following Saturday — correct,
not late.

**No backfill** (owner decision Q7). The first run grades nothing it did not
see published live, so no label in the audit was produced with knowledge of its
own outcome.

Rejected: grading at raw clock offsets (a 1h window over a closed market grades
nothing); grading all tagged tickers (cost, above); computing accuracy over only
the week just graded (a quiet week with nine labels would swing the figure
across the floor).

### 13. Per-source demotion with 52/66 hysteresis and automatic re-promotion

Owner decision Q6, plus one parent-session assumption (the ≥100 re-promotion
floor) and two rules this spec had to supply to make the owner's rule complete,
each marked.

**The state.** `sentiment_source_status(source, status, changed_at, reason,
audit_run_id)`, one row per source — `rules` and `massive` — with `status` ∈
`unaudited | active | demoted`. Every source starts `unaudited`. The weekly
audit run is the **only** writer; there is no human route to change it.

**The transitions**, evaluated per source, per run, after grading:

| From | To | When |
|---|---|---|
| `unaudited` | `demoted` | eligible to demote (below) **and** accuracy < 52% in **either** window |
| `unaudited` | `active` | eligible to demote **and** accuracy ≥ 52% in **both** windows — *spec-supplied rule* |
| `active` | `demoted` | eligible to demote **and** accuracy < 52% in **either** window |
| `demoted` | `active` | eligible to re-promote (below) **and** accuracy ≥ 66% in **both** windows |
| any | unchanged | otherwise — including every run where a source is not eligible |

**Eligible to demote:** ≥30 graded directional labels in *each* window over the
trailing 28 calendar days. Accuracy for the demotion check is computed over that
same trailing window.

**Eligible to re-promote:** ≥100 graded directional labels in *each* window,
counted over **the labels graded since the demotion**, and accuracy for the
re-promotion check is computed over those same labels. The ≥100 is the
parent-session assumption recorded under *Owner decisions*, Q6, with its
binomial table. *Since the demotion* is this spec's rule, not the owner's or the
parent's: a trailing-28-day window would make re-promotion structurally
impossible for any source producing fewer than ~5 directional labels a session
— which on step 0's estimates is likely the rules tier — and would turn the
owner's automatic re-promotion into a permanent demotion for exactly the
low-volume source, without anyone deciding that. Counting from the demotion also
keeps the labels that caused it out of the evidence for undoing it.

**The dead band.** Between 52% and 66% a source stays in whatever state it is
in. That is the point of two thresholds: one threshold would flap a source
sitting near it in and out of the scanner week to week on noise alone.

**Why 52%.** It is PRD §9's figure — random directional labels score 50%, and
52% is a thin margin above that. **It has no empirical derivation.** It stays
because the owner kept it; the 66% re-promotion bar is the owner's.

**Both transitions are audit-logged and both notify.** The configuration audit
log gains a `sentiment` category; a transition writes `field` = the source,
`previous_value` and `new_value` = the statuses, and the reason carries the two
accuracies and their sample sizes. `demoted` emits `sentiment_demoted`
(`warning`); `active` from `demoted` emits `sentiment_repromoted` (`info`). The
first `unaudited → active` transition is logged and emits nothing — nothing was
withdrawn or restored.

**Automatic re-promotion is deliberately unlike rule 9.** Rule 9 never resumes
itself, because resuming into an unverified position state is how a bot doubles
a position it already holds. None of that applies here: the flag is not an
order path, it changes no position, and re-promotion is itself an act of
verification — it happens only on a larger sample, at a higher bar, than the
demotion it reverses. The asymmetry that rule 9 protects is preserved in a
different form: getting out is easy (30 labels, 52%), getting back in is hard
(100 labels, 66%).

**What Phase 4 must honour** (carried, not built here): the scanner reads the
flag **once, at the start of each scan**, and a state change takes effect at its
next scan — never mid-decision. A candidate half-evaluated with sentiment as an
input and half without is a decision nobody made. Phase 4 also decides whether
an `unaudited` source may be a scanner input at all; the conservative reading is
no.

**In Phase 3 nothing consumes the flag as an input**, because nothing consumes
sentiment as an input. Settings renders the banner from it, and the feed marks
labels from a demoted source with a quiet *"display only"* suffix so the
demotion is visible where the labels are.

**Known risk, recorded rather than changed** (from the Q6 table): at n=30 a
source that is truly right 55% of the time is demoted on about a third of
checks, and at n=100 a truly-60% source re-qualifies on only 13%. A useful
source sitting demoted for months is the rule working conservatively, not a bug.

**A second known risk, and an open item for step 0:** trailing 28 calendar days
is about 19–20 sessions, so a source producing fewer than ~1.5 directional
labels per session **can never become eligible to demote** and stays
`unaudited` indefinitely. The rules tier is the candidate. The owner's
trailing-28-day rule is kept as given; if step 0 measures the rules tier below
that rate, the choice between a longer lookback for that source and accepting a
permanently unaudited source goes back to the owner rather than being changed
here.

Rejected: a boolean on a config row (loses the reason and the run that caused
it); deriving the state from the latest accuracy on read (a transition must be
an event with a timestamp, and hysteresis needs the previous state, which a
read-time derivation does not have); a single threshold (flaps); human-only
re-promotion (the draft's recommendation, not chosen).

### 14. Notifications land for real, and rule 9's halt alert goes first

Owner decision Q4.

- The `notification` table Phase 2's Database section described:
  `notification(id, at, event, severity, account, title, body, correlation_id,
  read_at, dismissed_at)`. `account` is null for engine and audit events, which
  per `notifications.ts` show in both books.
- `DbNotifier` writes the row; `DiscordNotifier` posts a rich embed (§10) when
  the event's `discord` route is on. Both implement the existing
  `engine/runtime.py` `Notifier` protocol; a `FanoutNotifier` holds the list.
- **Discord delivery is asynchronous.** `emit` is synchronous by design so the
  halt path stays testable, and a webhook is an HTTP call that can hang: the
  Discord notifier enqueues and returns, and an asyncio task delivers with a
  timeout, one retry on 5xx or 429 (honouring `retry_after`), and a logged
  failure — never a raise into the halt path. A failed delivery is recorded
  in `notification_delivery(notification_id, channel, status, attempted_at,
  detail)`, because an alert that silently did not arrive is the rule 9 failure
  the routing confirm exists to prevent.
- **The webhook URL is a secret** — it embeds its token. It is read for
  presence by `runtime.py` today and for use only inside the Discord notifier;
  `wire.vendor_detail`-style scrubbing covers any error body that could echo
  it. Rule 6.
- `GET /api/notifications` (account-scoped, per the bell's rules),
  `POST /api/notifications/{id}/read`, `…/dismiss`. Polled at 15s — the frame
  contract of 2026-09-13 keeps notifications off the socket, because rule 9
  halts *because* the socket died.
- The bell moves from `useUIStore` to a query. **The routing gate stays at
  emit time**, now in the engine rather than in `store.tick()`: CLAUDE.md's rule
  that unchecking a route must not retroactively erase notifications already
  received carries over unchanged.
- **Two new events.** `sentiment_demoted`, severity `warning` — the audit
  working as designed is a degradation report, not a system failure, the same
  reasoning that makes `stop_loss_hit` a warning. `sentiment_repromoted`,
  severity `info` — nothing is wrong. Seeded routes for both: bell on, Discord
  on, because each changes what Phase 4's scanner may read and the two should
  arrive on the same channels. PRD §10's table gains both rows. The frontend's
  `NotificationEvent` union, label map and severity map gain them too.

Rejected: sending Discord inline from `emit` (a hung webhook stalls the halt
path); a second, news-only notifier (two paths to the same channel, and the
second one is the one that drifts); routing `sentiment_repromoted` to the bell
only, the way `recommendations_ready` is (a restoration you hear about on one
channel and a withdrawal on two is an asymmetry nobody would design on purpose).

### 15. Every new host gets a bucket in the shared limiter

`corollary/ratelimit.py` gains `api.massive.com` at **5/min**,
`api.stlouisfed.org` at **120/min**, and `api.stocktwits.com` at **200/hour**.
`TokenBucket` is per-minute today; StockTwits needs a per-hour window, so the
bucket learns a window length rather than StockTwits getting a private limiter
— *"two limiters against one server-side ceiling over-spend by double and look
fine locally"* (`finnhub.py`). Budget 180/hour for the poll, leaving 20 for
retries. No Anthropic bucket: nothing in Phase 3 calls it.

### 16. Research and the chain ticket read the real halt, in this phase

PRD's Phase 4 carry-forward: *"the halt source must not survive the rewrite."*
Phase 3 edits `Research.tsx` for Market Pulse, so the swap to
`useEngineState()` happens here, in `Research.tsx` **and**
`ChainOrderTicket.tsx` — the 2026-09-16 re-verification found both reading
`useUIStore((s) => s.isHalted)`. Swapping one leaves two halts in one
application, which is the divergence behind four Phase 2 bugs. The third
carry-forward — *"Submits to the risk manager"* on three surfaces — stays
Phase 6's: it becomes true only when `RiskManager.approve()` exists, and
changing one surface without the others is the failure the PRD warns about.

### 17. `SentimentTier` becomes `'rules' | 'vendor'`

Owner decision Q1. The Phase 2 carry-forward resolves as follows:

- `SentimentTier = 'rules' | 'vendor'`. `'provider'` is renamed `'vendor'`,
  because the tier returns sourced from Massive (restored as *bought for $0*,
  the case PRD §9 anticipated).
- **`'llm'` is removed from the type**, not kept for Phase 4. A member with no
  producer is a member a fixture can claim, and a feed row labelled *"LLM"* in a
  phase with no model would be a fixture impersonating a tier — the same
  invented-number failure §8.5 exists to prevent. It returns, with its detail
  string, when Phase 4 builds the tier; adding a union member then is a
  one-line change the typechecker walks through every `Record<SentimentTier,…>`.
- `SENTIMENT_TIER_DETAIL.vendor` names Massive and says the score and its
  reasoning are Massive's, published as-is. `SENTIMENT_TIER_DETAIL.rules` loses
  its *"Tier 2"* numbering. The comment above `SENTIMENT_LABEL` that says
  *"an unlabelled item is always tier 3 falling below its confidence
  threshold"* is rewritten to decision 4's meaning of `Unclassified`.
- **`NewsItem.tier` becomes `SentimentTier | null`, and it is null exactly
  when `sentiment` is `'unclassified'`.** Under decision 4 an unclassified item
  is one *no source labelled*, so there is no tier to name; today every item
  carries a tier because unclassified meant "the LLM declined". The feed's tier
  cell renders an em dash for null. `news.test.ts:219`'s *"only ever leaves an
  LLM-tier item unclassified"* inverts into *"an item is unclassified if and
  only if its tier is null"*.
- Fixtures, counted 2026-09-24: **8** `tier: 'provider'` become `'vendor'`;
  **10** `tier: 'llm'` become `'rules'` or `'vendor'` where the item carries a
  direction, and `tier: null` where it is `sentiment: 'unclassified'` (four
  news items are); the **15** `'rules'` stay. `news.test.ts:210`'s
  *"covers all three classification tiers"* becomes two tiers plus the null
  case.
- Not to be confused with `Recommendation.origin: 'scanner' | 'llm'` in
  `types.ts` — LLM *origination*, a Phase 4/6 concept, which this decision does
  not touch.
- The backend's `tier` column carries the same two values, CHECK-constrained,
  so adding `llm` in Phase 4 is a migration — deliberately.

### 18. The LLM sentiment tier is deferred to Phase 4

Owner decision Q1. Recorded here so Phase 4 inherits the design rather than
re-deriving it; **none of it is built in Phase 3**:

- Direct Anthropic API over HTTPS, never MCP (rule 3). One vendor file,
  `corollary/llm/anthropic.py`, behind a `LanguageModel` protocol in
  `corollary/llm/interface.py` — top-level, because both `data/news/` and
  `engine/llm/` would use it and `data/` must not import from `engine/`.
- The model name is configuration, never a literal — a
  `COROLLARY_SENTIMENT_MODEL` variable, failing loudly when unset.
- Batched 20 headlines per call, cached by article ID (§9); strict JSON output;
  a batch that fails validation leaves every item unlabelled rather than
  partially or speculatively parsed.
- Confidence gating: below a configured threshold (the draft proposed 0.70) the
  label is stored with its confidence and displayed `Unclassified`, and the
  audit reports accuracy by confidence decile so the threshold is tuned on
  evidence.
- The LLM tier becomes a third audited source under decision 13's rules, and
  starts `unaudited`.

**In Phase 3:** `ANTHROPIC_API_KEY` is unused; nothing is added to
`.env.example`; `corollary/llm/` is not created.

### 19. FRED replaces the risk-free placeholder while it is here

`pricing/blackscholes.py`'s `DEFAULT_RISK_FREE_RATE = 0.0425` is documented as a
placeholder for FRED `DGS3MO`. Phase 3 builds the FRED client anyway, so
`AlpacaProvider` takes the latest `DGS3MO` observation, falling back to the
placeholder only when FRED is unreachable, and the chain's derived greeks
record which one they used — the same *measured versus derived* discipline
decision 10 of the Phase 2 spec set for IV.

---

## Design

### Module layout

```
corollary/
├── data/
│   ├── seeds/
│   │   ├── spdr_holdings.csv     # decision 6, as-of in header
│   │   ├── central_banks_2026.csv
│   │   ├── central_banks_2027.csv
│   │   └── econ_release_times.csv  # Q3: per-release ET times, human-kept
│   ├── providers/
│   │   ├── interface.py          # + NewsProvider, CorporateActionsProvider
│   │   ├── alpaca.py             # + news(), corporate_actions() — Alpaca's surface
│   │   ├── finnhub.py            # + company_news, market_news, earnings, recommendations
│   │   ├── massive.py            # the vendor sentiment source (Q1)
│   │   ├── stocktwits.py
│   │   └── fred.py               # series + release dates (Q3)
│   ├── news/
│   │   ├── ingest.py             # poll, normalise, dedupe → news_article
│   │   ├── rules.py              # the rules tier: deterministic headline patterns — pure
│   │   ├── vendor.py             # Massive insights → sentiment_label
│   │   ├── audit.py              # grading and transitions — pure — plus a runner
│   │   └── social.py             # StockTwits counting
│   ├── macro/
│   │   ├── components.py         # raw component computations — pure
│   │   └── composite.py          # z-score, Φ, mean — pure, on read
│   └── calendar.py               # earnings, dividends, central banks, releases, manual
├── engine/
│   ├── scheduler.py              # the job set, calendar-clocked
│   └── notify.py                 # DbNotifier, DiscordNotifier, FanoutNotifier
└── api/routes/
    ├── news.py                   # feed, composite, social, consensus
    ├── calendar.py               # read + manual geopolitical CRUD
    └── notifications.py
```

`engine/notify.py` implements the protocol `engine/runtime.py` already defines;
the protocol stays where it is so the halt path's imports do not move. No
`corollary/llm/` in Phase 3 (decision 18).

### Feeds and budgets

| Feed | Source | Cadence | Host bucket | Spend |
|---|---|---|---|---|
| Benzinga headlines | Alpaca `/v1beta1/news`, untickered | 60s in session, 5 min otherwise | `data.alpaca.markets` 200/min | ≤1/min |
| Company news | Finnhub `/company-news`, per symbol | each symbol every 10 min | `finnhub.io` 60/min | ~3/min for ~30 symbols |
| Market news | Finnhub `/news?category=general` | 5 min | `finnhub.io` | 0.2/min |
| Vendor-scored news | Massive `/v2/reference/news`, untickered, `limit=1000` | 15 min | `api.massive.com` 5/min | 0.07/min |
| Social | StockTwits symbol streams | budgeted round-robin | `api.stocktwits.com` 200/hr | 180/hr |
| Earnings | Finnhub `/calendar/earnings`, 3-week window | daily 07:00 ET | `finnhub.io` | 1/day |
| Consensus | Finnhub `/stock/recommendation` | weekly | `finnhub.io` | 55/week |
| Dividends | Alpaca corporate actions | daily | `data.alpaca.markets` | ≤2/day |
| FRED series and release dates | `VIXCLS`, `BAMLH0A0HYM2`, `DGS3MO`, `/releases/dates` + per-release observations | daily 10:00 ET, and ~10 min after each scheduled release for its actual | `api.stlouisfed.org` 120/min | ≤20/day |
| Composite prices | SPY, TLT, sector ETFs, ~500-name universe daily bars | EOD, 16:30 ET | `data.alpaca.markets` | ~1–2 pages/day; ~26 pages once, on the first run's history load |
| Put/call | SPY chain snapshots | EOD | `data.alpaca.markets` | ~9 pages/day |
| Audit prices | SIP 1Min bars, watch universe, the week | Saturday | `data.alpaca.markets` | ~40 pages/week |
| Top sector (Pulse) | sector ETF snapshots | with the Markets poll's cadence when Research is open | `data.alpaca.markets` | shares the Markets request |

**Massive at 5/min is not the binding constraint on label volume.** One
untickered call every 15 minutes returns every article Massive published in
that interval — its news is *"updated hourly"* anyway — so the 5/min budget is
~1% spent. What bounds the vendor source's labels is how many articles Massive's
publishers write about watch-universe tickers, which step 0 measures.

**The data bucket is the one that matters.** Decision 18 of the Phase 2 spec
spends 150/min of it on the Markets poll at 400ms, leaving ~49/min. Phase 3
adds ≤1/min in session. The EOD composite pages and the Saturday audit run
outside the session, when the Markets poll is backgrounded or stopped, and wait
on the bucket rather than refusing — which is how `HostRateLimiter` already
behaves. Nothing here reprices a Phase 2 cadence.

The composite's one-time history load is a load of *market prices* for the
z-score window, not a backfill of labels — Q7's no-backfill governs the
sentiment audit only.

### Database

Migrations **0005** onward, one per step that introduces tables. Money-like
prices and returns use `corollary.db.types.Money` (TEXT, exact `Decimal`, SQL
comparison refused); every timestamp is `UtcDateTime`.

- `news_article(id, vendor, vendor_id, canonical_id, url, headline, summary,
  publisher, published_at, ingested_at)`, UNIQUE `(vendor, vendor_id)`.
- `news_article_ticker(article_id, ticker)`.
- `sentiment_label(id, article_id, ticker, source, tier, direction, reasoning,
  rule_id, labeled_at)`, UNIQUE `(article_id, ticker, source)`. `direction` ∈
  `bullish | bearish | neutral`; `tier` ∈ `rules | vendor`, CHECK-constrained.
  No `confidence` column — neither Phase 3 source has one; Phase 4 adds it with
  the LLM tier. An item with no row displays `Unclassified` (decision 4); the
  table stores labels, not their absence.
- `label_outcome(label_id, window, reference_at, reference_price,
  outcome_at, outcome_price, benchmark_return, excess_return, correct,
  audit_run_id)`, UNIQUE `(label_id, window)`. `correct` is null for a neutral
  label (Q5).
- `audit_run(id, started_at, finished_at, trigger, status, detail)`.
- `sentiment_accuracy(audit_run_id, source, window, basis, graded, correct,
  neutral)` — `basis` ∈ `trailing_28d | since_demotion`, so the figure a
  transition acted on is the figure stored. Counts only; the percentage is
  derived on read.
- `sentiment_source_status(source, status, changed_at, reason, audit_run_id)`,
  plus the transitions themselves in `audit_log` (decision 13).
- `calendar_event(id, kind, source, title, ticker, date, at, estimate, prior,
  actual, unit, vendor_id, created_at, updated_at, deleted_at)`. `date` and `at`
  are separate columns for the reason `CalendarEvent` documents: an ex-date
  stored as an instant groups under the previous day. `estimate` stays null on
  economic releases (Q3: consensus unavailable).
- `composite_component_value(date, component, raw_value, source)`, UNIQUE
  `(date, component)`.
- `fred_observation(series_id, date, value)`. Not exported, per ICE's notice.
- `social_session_count(ticker, session_date, mentions, labeled, bullish,
  bearish, truncated, cursor)`.
- `analyst_consensus(symbol, period, strong_buy, buy, hold, sell, strong_sell,
  fetched_at)`.
- `notification(...)`, `notification_delivery(...)` — decision 14.
- `AUDIT_CATEGORIES` gains `sentiment`; `notification_route` gains the
  `sentiment_demoted` and `sentiment_repromoted` rows. Both are
  CHECK-constrained, so both are migrations — which is the point: a typo does
  not create a category.

### API

All read routes are plain `GET`s depending on no broker; the structural guards
in `tests/api/test_account_mode.py` apply unchanged.

- `GET /api/news` — `lookback`, `ticker`, `sector`, `publisher`, `sentiment`,
  `sort`, paginated. Each item carries its displayed label, the tier and source
  that produced it, and whether that source is demoted.
- `GET /api/news/composite` — per component: raw value, z, score, source,
  whether it is a proxy and of what, forming count, as-of; the composite and how
  many components it averages.
- `GET /api/news/social`, `GET /api/news/consensus`.
- `GET /api/calendar?from&to`; `POST`/`PUT`/`DELETE /api/calendar/manual/{id}`
  for geopolitical rows only — a vendor or seeded row is not editable.
- `GET /api/markets/pulse` — VIX (with its as-of and whether intraday), the
  composite, the top sector ETF by session change.
- `GET /api/settings/sentiment` — per source: status and since when, the
  accuracy per window with its graded count and basis, whether the source is
  eligible to demote or re-promote and how many more labels it needs, last and
  next run. `POST /api/settings/sentiment/audit` (hourly limit). **No
  reinstate route** — the audit is the only writer of the status (Q6).
- `GET /api/notifications`, `POST /api/notifications/{id}/read`,
  `POST /api/notifications/{id}/dismiss`.
- `GET /api/settings/sources` — each Phase 3 source moves from *"nothing reads
  it yet"* to its real status as its step lands.

### Frontend

- **News:** the feed, composite, social, consensus and calendar move from
  `mockData` to query hooks, **one panel at a time**, and `FixtureMarker` comes
  off each panel as it goes real — decision 8 of the Phase 2 spec, applied in
  reverse. The page's marker moves from the title to the panels while the page
  is mixed, as Settings already does.
- **`SectorConsensus`** changes to counts plus derived percentages; the
  existing `sortConsensus` / `consensusNet` keep working on the percentages.
- **Calendar:** earnings rows carry a session label rather than a time;
  economic releases show prior and actual with consensus as an explicit
  *"not available"*; the manual-entry form is the one write control on the page.
- **Research:** Market Pulse from `/api/markets/pulse`; `isHalted` from
  `useEngineState()` in `Research.tsx` and `ChainOrderTicket.tsx` (decision 16).
  Research's own marker narrows to what is still fixture (recommendations,
  strategies, origination, chat).
- **Settings:** the accuracy table becomes two rows (rules, Massive) with `n`
  per cell and an *"insufficient sample — N of 30"* state below the eligibility
  floor, rendered as a sample-size fact, not a caution colour. It states both
  thresholds (52% to demote, 66% to re-promote) and the dead band between them
  in words. The demotion banner reads from `sentiment_source_status`; a demoted
  source shows its re-promotion progress (*"since demotion: 41 of 100 in the
  1h window"*). There is **no re-promote button** (Q6). `settings.ts`'s
  `demotedSources` stops computing demotion from accuracy — the status is the
  server's, and a client-side recomputation is how the page and the engine would
  come to disagree; a `SENTIMENT_REPROMOTE` constant joins `SENTIMENT_FLOOR`.
- **Bell:** `NotificationBell` reads the query; unread and account scoping stay
  in `notifications.ts`, which gains the two new events.

---

## Testing

Phase 3 is PRD-rated *safe to build fast*, and the testing weight follows the
blast radius: the context pipeline cannot place an order, so it gets the
standard coverage; the two things that can hide a real failure — notifications
and the audit's transitions — get the careful coverage.

**Backend.**

- Providers: recorded vendor responses as fixtures, no live calls, redaction
  that sees into prose (the Phase 2 recorder's lesson). One recorded premium
  refusal per vendor, so a paywall is a tested path rather than a surprise.
- `rules.py`: every pattern has a positive and a negative headline; the
  rule set is deterministic — same headline, same label.
- `vendor.py`: one Massive article tagging several tickers yields one label
  per ticker with its own direction; an article with no insight for a ticker
  yields no row for it.
- Dedupe: two vendors' copies of one story collapse; two different stories
  with the same headline an hour apart do not.
- `components.py` / `composite.py`: each component against a hand-computed
  fixture; orientation (VIX up ⇒ lower score); a forming component is excluded
  and counted; the composite equals the mean of the scores the endpoint prints
  (§8.5's cannot-disagree invariant, asserted rather than trusted).
- `audit.py` grading, pure: reference price in session and out; the 1h window
  across a close lands in the next session; a half-day; excess return vs SPY;
  a `MARKET` label against SPY raw; neutral excluded and counted; re-running a
  completed audit is a no-op.
- `audit.py` transitions, pure, **one test per row of decision 13's table and
  one per boundary**: 29 labels at 40% does not demote; 30 at 15/30 (50%)
  demotes; 30 at 16/30 (53.3%) does not; below 52% in the 1d window alone
  demotes; a demoted source at 99 labels and 80% does not re-promote; 100 at
  66/100 in both windows re-promotes; 100 at 66% in one window and 65% in the
  other does not; a source at 60% stays in its current state from both sides of
  the dead band; re-promotion counts only labels graded after the demotion;
  `unaudited` goes to `active` at ≥52% in both windows.
- Transitions write exactly one `audit_log` row and emit exactly one
  notification each; `unaudited → active` emits none.
- **Rule 9 isolation:** a context job raising — including an Alpaca news
  failure — leaves `engine_state.halted` unchanged and never touches the
  watchdog. Marked `risk`, since it guards the dead-man's switch.
- Notifications: `emit` returns without awaiting Discord; a hung webhook does
  not delay a halt; a failed delivery is recorded; the webhook URL appears in no
  log line or error body; routing is read at emit time; the halt alert reaches
  both sinks. Marked `risk`.
- Scheduler: jobs fire on a calendar clock across a holiday and a half-day.
- `uv run python -m pytest -m risk` still collects and passes; `mypy` clean.

**Frontend.**

- Each migrated panel renders loading, empty, stale and populated from mocked
  API responses; a forming component and a forming baseline render their counts.
- The `SentimentTier` change (decision 17) moves the fixtures and
  `news.test.ts` together, and the typechecker proves no `'llm'` or
  `'provider'` survives.
- Settings renders unaudited, active, demoted-with-progress and
  insufficient-sample states, and offers no re-promote control.
- Research and the chain ticket disable Execute from the engine-state query,
  not the store.
- `npm run typecheck` and `npm run test` pass.

---

## Doc amendments

To land with the step that makes them true, not before. `PRD.md` is not edited
by this spec; these are the amendments it owes.

- **PRD §7** —
  - Economic calendar: **FRED release dates plus a hand-kept per-release time
    table, with prior and actual from FRED; consensus unavailable** — not
    Finnhub.
  - Sector leaders become plural: *"the top five holdings of each SPDR sector
    ETF, weighted by fund weight, from a committed seed of the funds' published
    holdings"*.
  - A note that Finnhub's economic calendar, ETF holdings, both dividends
    endpoints, index constituents and `/news-sentiment` are premium, verified
    from Finnhub's own OpenAPI document (`premium` field) on 2026-09-23, and
    confirmed against the key in step 0.
  - Dividends from Alpaca corporate actions (or removed, per step 0).
  - News sentiment row: *Rules + Massive (vendor); LLM tier from Phase 4*.
- **PRD §8.3** — the composite's proxies stated, its daily cadence and *"N of 7
  components"*; social baseline forms over 30 sessions; earnings carry a
  session, not a time; consensus as counts; economic releases show no
  consensus.
- **PRD §8.5** — VIX on Market Pulse is the last close unless an intraday
  source is confirmed in step 0.
- **PRD §8.7** — the accuracy readout carries sample sizes and eligibility,
  both thresholds and the dead band, and re-promotion progress for a demoted
  source.
- **PRD §9** —
  - The vendor tier is **reinstated, sourced from Massive** (the *bought for
    $0* case §9 already anticipated), labelling per ticker with its reasoning.
  - The **LLM tier moves to Phase 4**, with its confidence gating; the Phase 3
    meaning of `Unclassified` is decision 4's.
  - Sources label independently and are graded independently; display is
    rules first (decision 4).
  - Scoring is sign vs return in excess of SPY; neutral excluded and counted.
  - Demotion becomes **per source, with 52/66 hysteresis and automatic
    re-promotion**: below 52% in either window demotes (≥30 labels, trailing 28
    days); at or above 66% in both windows re-promotes (≥100 labels since the
    demotion); the dead band holds state. §9's *"demotes news sentiment"* reads
    as all of it and is replaced. The 52% floor is stated as PRD-given with no
    empirical derivation.
  - No backfill; the audit starts empty.
- **PRD §10** — two rows: `Sentiment source demoted` (bell ✓ Discord ✓,
  warning) and `Sentiment source re-promoted` (bell ✓ Discord ✓, info).
- **PRD §11** — Phase 3's done-criterion (below). And under Phase 4: the LLM
  sentiment tier (decision 18), and the scanner reading the demotion flag once
  per scan (decision 13).
- **CLAUDE.md, Layout** — `data/news/`, `data/macro/`, `data/seeds/`,
  `data/calendar.py`, `engine/notify.py`, `engine/scheduler.py` no longer a
  stub, and the new routes.
- **CLAUDE.md, Working with market data** — the vendor-surface paragraph
  names the Phase 3 vendor files (`massive.py`, `stocktwits.py`, `fred.py`)
  with the same one-file-per-vendor rule.
- **CLAUDE.md, frontend — `web/src/lib/settings.ts` entry** — demotion is
  server state, not recomputed client-side; the 66% re-promotion bar.
- **`corollary/data/news/__init__.py`** — its docstring still says *"Three-tier
  news sentiment pipeline: provider-supplied, rules, LLM"*. Rewritten with
  step 5 to *rules and vendor, LLM in Phase 4*.
- **`.env.example`** — nothing added. No StockTwits key (it is keyless), no
  sentiment-model variable (decision 18).
- **Phase 2 spec, carried items** — the `SentimentTier` follow-up is closed by
  decision 17; the Research/`isHalted` item by decision 16.

---

## Order of work

Same form as the Phase 2 list: each entry gives **Status**, **Depends on**,
**Files**, and **Done when**. Every step branches from, and its pull request
targets, **`master`**. **A step's status changes in the same commit as the
thing it describes** — the Phase 2 spec records why that rule exists.

Nothing below is blocked on an owner question any longer. Step 0 can still
send one question back — the rules tier's volume against the demotion window
(decision 13's second known risk).

**0. Keyed probes.** One script, run with the launcher's `--env-file .env` so no
agent reads the file, recording redacted fixtures. Checks: Finnhub's premium
flags on `/calendar/economic`, `/etf/holdings`, `/stock/dividend`; the free
earnings calendar returns upcoming dates and what `hour` holds;
`/stock/recommendation` shape; `/quote?symbol=^VIX` on the free tier; Alpaca
news shape; Alpaca corporate actions — does an announced dividend appear before
its ex-date; `indicative` option snapshot `dailyBar.v` for SPY; Massive's
untickered `limit=1000` call and its `sentiment` values; a StockTwits poll
sequence at the planned cadence for one hour, watching for a 429 or a
challenge; FRED `VIXCLS`, `BAMLH0A0HYM2`, `DGS3MO`, `/releases/dates`. **And a
label-volume measurement:** over at least five sessions of recorded headlines,
how many directional labels per session the rules patterns and Massive's
insights produce on the watch universe, and Massive's neutral share. Plus a
human read of StockTwits' and Massive's terms for automated access.
- *Status:* **done 2026-09-24, except the two human terms reads** (StockTwits,
  Massive — URLs in *Not verified*). All probes ran, including the full
  one-hour StockTwits poll (180 requests, no 429, no challenge), and the label
  volume is measured over ten sessions. Decision 13's second-risk condition
  was checked and is **not triggered as measured**: the strict rules estimate
  is 4.2 labels per session, of which Finnhub `/company-news` supplies 3.7 —
  measured across the whole 66-name watch universe. It therefore holds only if
  step 4 ingests Finnhub company news for that whole universe; *Feeds and
  budgets* still budgets company news for ~30 symbols, and at that scope the
  figure was not measured and may not clear 1.5. An Alpaca-only rules tier, at
  1.2, would trigger it. Either case goes back to the owner.
  *Depends on:* nothing.
- *Files:* `scripts/probe_phase3.py`, `tests/fixtures/{finnhub,alpaca,massive,stocktwits,fred}/`.
- *Done when:* every *Not verified* item above is struck through or moved to
  Constraints with its result, the estimates in *Done when* are replaced by the
  measured volumes, and — if the rules tier measures below ~1.5 directional
  labels per session — decision 13's second risk has gone back to the owner.

**1. Notifications land; rule 9's halt alert is delivered.**
- *Status:* **done 2026-09-24** (45171a1 backend, 787fe80 bell).
  Hung-webhook-does-not-delay-halt, failed-delivery recording and webhook
  redaction are pinned in `risk` tests. **Not run: the live forced watchdog
  halt on `:app` producing a bell entry and a Discord embed** — left for the
  owner, because it halts the engine against the configured database and
  posts to the real webhook. *Depends on:* nothing.
- *Files:* `corollary/engine/notify.py`, `corollary/engine/runtime.py` (wire
  the notifier), `corollary/db/models.py`, migration 0005,
  `corollary/api/routes/notifications.py`, `web/src/components/NotificationBell.tsx`,
  `web/src/lib/{api,queries,notifications,types}.ts`.
- *Done when:* a forced watchdog halt on `:app` produces a bell entry and a
  Discord embed, the halt path is not delayed by a hung webhook in test, and
  `pytest -m risk` passes. First, because it closes a rule 9 gap that exists
  today.

**2. Scheduler and rate-limit hosts.**
- *Status:* **done 2026-09-24** (9f659f5 rate-limit windows and hosts, a898760
  scheduler). The rule 9 isolation tests are `risk`-marked and run the
  shipped job set. *Depends on:* nothing.
- *Files:* `corollary/engine/scheduler.py`, `corollary/ratelimit.py`,
  `corollary/api/app.py`.
- *Done when:* a calendar-clocked no-op job runs in the lifespan across a
  holiday fixture, the per-hour bucket works, and the rule 9 isolation test
  passes.

**3. FRED client; risk-free rate from `DGS3MO`.**
- *Status:* not started. *Depends on:* 2.
- *Files:* `corollary/data/providers/fred.py`, `corollary/data/providers/alpaca.py`,
  `corollary/pricing/blackscholes.py`, migration for `fred_observation`.
- *Done when:* the chain's derived greeks state which rate they used and use
  FRED's when it is reachable.

**4. News ingestion: Alpaca and Finnhub, deduplicated, served.**
- *Status:* not started. *Depends on:* 0, 2.
- *Files:* `corollary/data/news/ingest.py`, the two provider files,
  `corollary/data/seeds/spdr_holdings.csv` (for sector), migration for
  `news_article*`, `corollary/api/routes/news.py`, News feed panel.
- *Done when:* the News feed shows today's real headlines with ticker,
  publisher and sector, lookbacks work, and the feed panel's marker is off.
  Every label reads `Unclassified` until step 5 — honestly.

**5. Sentiment labelling: rules and Massive; `SentimentTier` resolved.**
- *Status:* not started. *Depends on:* 4.
- *Files:* `corollary/data/news/{rules,vendor}.py`,
  `corollary/data/providers/massive.py`, migration for `sentiment_label`,
  `web/src/lib/{types,mockData}.ts`, `web/src/lib/news.test.ts`,
  `corollary/data/news/__init__.py`.
- *Done when:* the feed carries real labels from both sources, each naming its
  tier, rules taking display precedence, everything else honestly
  `Unclassified`; no `'llm'` or `'provider'` survives the typecheck.

**6. The self-audit and the demotion state.**
- *Status:* not started. *Depends on:* 1 (for the two alerts), 5.
- *Carries:* Q5 (excess-return scoring, neutral excluded), Q6 (per source,
  52/66 hysteresis, automatic re-promotion, ≥30 / ≥100 floors), Q7 (no
  backfill) — all decided.
- *Files:* `corollary/data/news/audit.py`, `corollary/engine/scheduler.py`,
  migrations for `label_outcome`, `audit_run`, `sentiment_accuracy`,
  `sentiment_source_status`, the `sentiment` audit category and the two
  notification routes, `corollary/api/routes/settings.py`,
  `web/src/pages/Settings.tsx`, `web/src/lib/settings.ts`.
- *Done when:* the Saturday job has run once for real and written a run row;
  Settings shows each source's status, graded counts per window and how many
  more labels each needs to become eligible; every transition in decision 13's
  table is exercised in test with its audit row and notification; and the
  readout's marker is off. **It is not a done-criterion that a score exists** —
  see *Done when*.

**7. Calendar: earnings, dividends, central banks, economic releases, manual
geopolitical.**
- *Status:* not started. *Depends on:* 0, 2, 3.
- *Files:* `corollary/data/calendar.py`, `corollary/data/seeds/` (including
  `econ_release_times.csv`), `corollary/api/routes/calendar.py`, migration for
  `calendar_event`, the calendar panel.
- *Done when:* the next two weeks show real earnings (with sessions), the next
  FOMC with its time, economic releases on FRED's dates at the table's times
  with consensus shown unavailable and actuals filled after release, dividends
  or a stated reason they are absent, and a manual entry round-trips.

**8. Composite and Market Pulse.**
- *Status:* not started. *Depends on:* 2, 3, the holdings seed from 4.
- *Files:* `corollary/data/macro/{components,composite}.py`, migration for
  `composite_component_value`, `/api/news/composite`, `/api/markets/pulse`,
  `SentimentGauge`, `web/src/pages/Research.tsx`,
  `web/src/components/ChainOrderTicket.tsx` (decision 16).
- *Done when:* News shows the composite with all seven components, every
  proxy stated, put/call shown forming with its count; Research's Pulse agrees
  with it to the digit; and both Research and the chain ticket read the real
  halt.

**9. Social attention and analyst consensus.**
- *Status:* not started. *Depends on:* 0 (the StockTwits terms read can stop
  the social half), 2, the holdings seed.
- *Files:* `corollary/data/providers/stocktwits.py`,
  `corollary/data/news/social.py`, `corollary/data/providers/finnhub.py`,
  migrations, `/api/news/{social,consensus}`, the two panels,
  `web/src/lib/types.ts` (`SectorConsensus`).
- *Done when:* both panels are real, the baseline says how many sessions it
  has, consensus shows both as-of dates, and both markers are off.

**10. Doc amendments.** Last, so they describe what was built.
- *Status:* not started. *Depends on:* 1–9.
- *Files:* `PRD.md`, `CLAUDE.md`, this file.
- *Done when:* every item under *Doc amendments* is applied or struck with a
  reason.

**Can start now, in parallel:** 0, 1, 2. Then 3 and 4; 5 after 4; 6 after 1 and
5; 7, 8 and 9 once 0 and 2 are in.

---

## Done when

*You open it at 08:00 and learn something true about the day ahead, and the
system tells you whether its own sentiment is worth reading yet — or, on day
one, exactly how far it is from being able to say.*

Concretely, on a trading-day morning against the live paper account:

1. The calendar shows today's and this week's scheduled events — earnings
   with their session, central-bank decisions with their time, economic
   releases at their scheduled time with consensus shown unavailable, dividends
   or a stated absence — and nothing on it is a placeholder time.
2. The feed shows overnight headlines on your names, labelled by rules or
   Massive where either has something to say, and honestly `Unclassified`
   everywhere else — which will be most rows.
3. The composite prints as of yesterday's close with every component's
   source, proxy and forming status stated, and Research's Pulse agrees with it.
4. Settings shows the self-audit's last run and, per source, its status, its
   graded counts per window and how many more it needs to be eligible — plus an
   accuracy figure once one exists.
5. No panel on News, Pulse or the accuracy readout carries a fixture marker
   unless it is still a fixture.
6. A forced halt reaches the bell and Discord.

**On day one, *"self-audit job running"* means the job runs on schedule and
Settings shows n and eligibility — not a score.** The audit starts empty (Q7),
and a label is gradable a trading day after publication at the earliest.

**How long until it can act — measured by step 0, 2026-09-24.** Trailing 28
calendar days holds ~19–20 sessions, and every graded label is graded in both
windows, so the binding count is directional labels per session per source.
Measured from history over the **ten completed sessions 2026-09-10 → 2026-09-23**.
An article counts toward the first session whose close is at or after its
publication, with session boundaries taken from `calendars.py`. The watch
universe was **66 tickers**: the 26 Markets names ∪ a **hand-written
approximation** of decision 6's sector-leader seed, which does not exist until
step 4. Open-position underlyings were omitted, and `MARKET` contributed
nothing, since no vendor tags it and the draft patterns are company events.

| Source | Measured directional labels / session on the watch universe | First run eligible to demote (≥30 in each window, trailing 28 days) | First possible re-promotion after a demotion (≥100 since) |
|---|---|---|---|
| Massive (vendor insights) | **71.4** mean (37–132). **Neutral share 47.1%** (63.6 neutral per session), and `mixed` is excluded. Directional labels skew **88% positive** (627 : 87). | Even the slowest session clears 30, so it is eligible at the **first Saturday audit** after launch that has one full session's 1d windows matured | ~2 sessions (at most 3 on the slowest days) → the **first Saturday audit after the demotion** |
| Rules — **draft-pattern estimate**, credited only where the headline names the ticker, deduplicated across vendors by normalised headline | **4.2** mean (1–7). Counted as distinct (session, ticker, direction) events, **2.9** (1–4). A hand read of the matches put precision at ~85–90%, i.e. ~3.6 true labels | ~7–11 sessions → the **second or third Saturday** after launch (~1.5–2.5 weeks). ~82 labels in a trailing 28 days, well clear of 30 | ~24–35 sessions → **~5–7 weeks** after the demotion |
| Rules — **draft-pattern estimate**, credited to every vendor-tagged ticker (upper bound) | 8.4 (article, ticker) pairs, 7.0 articles (4–14) | ~4 sessions → the **first or second Saturday** | ~12 sessions → **~2.5 weeks** |

These figures were measured on 2026-09-24 by `scripts/probe_phase3.py labels
--sessions 10`. The raw output behind them was not retained; re-running that
command reproduces the measurement, and step 5 re-measures anyway.

**Against decision 13's second risk (1.5 directional labels per session): the
rules estimate is above it**, under both attributions, and above it on nine of
the ten sessions under the strict one (the minimum was 1, on 2026-09-17).
**That depends on ingesting Finnhub `/company-news` for the watch universe.**
Per vendor, the strict estimate is Finnhub 3.7, Alpaca 1.2 and Massive's own
headlines 0.2. An Alpaca-only rules tier would sit **below 1.5**, and that
configuration goes back to the owner. The rules figures are an estimate from
draft regexes in `scripts/probe_phase3.py`, not step 5's patterns, and **step 5
must re-measure** them. This does not resolve the second risk. It says the
risk is not triggered by what could be measured before the patterns exist.

The social baseline needs 30 sessions and the put/call component 60. *"Self-audit
producing a figure that can act"* is a later, dated event per source, and
Settings says when it happened.

---

## Out of scope

No order path of any kind: no `submit_order`, no `RiskManager.approve()`, no
`BrokerExecution`. **No consumer of sentiment, the composite, social attention
or consensus as an input** — the scanner is Phase 4, and so are its obligations
under decision 13 (read the flag once per scan; decide the `unaudited` case).
**No LLM sentiment tier and no Anthropic call of any kind** (owner decision Q1;
decision 18 records the design for Phase 4). No Research chat model: the chat
stays a scripted shell until Phase 4 (§8.5). No manual re-promotion or
demotion control (Q6). No label backfill (Q7). No Algo Trader Plus, no
plan-dependent cadence changes (Phase 2 decision 20). No put/call backfill from
historical option bars — Phase 5's Parquet download is where that becomes cheap.
No Alpha Vantage, Marketaux, Reddit or X (PRD §7). No change to the three
*"Submits to the risk manager"* surfaces (Phase 6). No purchase of Finnhub
premium.
