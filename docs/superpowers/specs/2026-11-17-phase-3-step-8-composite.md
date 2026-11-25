# Phase 3, step 8: the composite and Market Pulse

**Date:** 2026-11-17
**Status:** Draft, **blocked on two owner questions**, Q27 and Q28, listed
directly below. Planning was authorised by the owner's request for this scope.
Implementation was not: Q20 still pauses step 8, and only the owner can lift
it (Q27). No unit below starts until Q27 is answered. The units that Q28
reaches are marked.
*Amended 2026-11-17:* **both questions are answered** (see *Owner decisions —
2026-11-17 (Q27, Q28)* below). Q27 releases U10 now, ahead of the rest, and
the rest of step 8 after it. Q28 takes option (b), with a *provisional*
marker added. Nothing is blocked on a question.
**Parent spec:** `2026-09-23-phase-3-context-design.md`. This spec carries its
step 8: decision 5 (the composite), decision 16 (the real halt), decision 19's
FRED client, owner decision Q2 (the seven proxies), and the PRD §8.3 and §8.5
doc amendments. It amends none of the parent's decisions. It corrects three
figures in the parent's *Feeds and budgets* and one stale dependency (see
*Doc amendments*).
**Branch:** every unit branches from, and its pull request targets,
**`master`** (`d320bab` when this was written).
**Phase:** 3, context. **No order reaches a broker, and nothing built here is
an input to anything that could place one.** No consumer of the composite as
an input is built here. That is Phase 4's scanner, and it is not this spec's
job.

---

## Owner decisions — 2026-11-17 (Q27, Q28) and 2026-11-23 (Q29)

*Heading amended 2026-11-17; it read "Open questions: blocking".* These were
not put to the owner during planning, because no question tool was available
to the planning session. Each one was written for the owner to answer. No
answer was assumed. Q27 and Q28 were answered on 2026-11-17, and Q29 on
2026-11-23. Each answer is recorded at the top of its question, with the owner's words quoted. The
question text below each answer is kept as it was asked.

**Q27: Does this spec release step 8 for implementation?**
- **Answer:** *"for the code issues we can fix those right now before
  starting on step 8"*. The "code issues" are this spec's *Problem* item 3,
  the three surfaces that read the fixture halt, which is unit U10.
  - **U10 is released now** and is built first, on its own branch, ahead of
    the rest. That is option (b)'s scope, taken first.
  - **The rest of step 8 follows U10.** This is a parent-session reading of
    *"before starting on step 8"*: the owner expects step 8 to start once
    the halt fix is in. U0–U9 and U11 are therefore released to begin after
    U10. *The owner should confirm this reading before U0's probes run*,
    since U0 is the first unit that needs the owner at the keyboard.
  - **Step 9 stays paused under Q20.** Nothing here touches it.

*As asked:* Q20 (2026-09-30)
paused step 5 and everything after it. Q22 (2026-10-10) released steps 5, 6
and 7 only. Asking for this scope authorised the planning, not the build.

- **(a) Release all of step 8** (U0–U11).
- **(b) Release only U10, the real-halt swap, now, and hold the rest.**
  U10 is small, touches no backend and no vendor, and closes PRD §11's
  Phase 4 carry-forward. That carry-forward is harmless while Execute is a
  fixture and a real hazard once Execute submits anything.
- **(c) Hold everything.** Step 8 stays paused under Q20.

*Trade-off:* the composite adds three context jobs and costs about 27
data-bucket requests once (the history load) and about 25 a day after the
close (*Feeds and budgets*). It builds nothing that any other code reads as an
input. U0's probes are keyed reads, run by the owner's launcher, and they need
two sittings: one after a close and one before the next open. Step 9 stays
paused under Q20 whatever the answer.
*Blocks:* every unit.

**Q28: On a morning before FRED has posted, which close does the composite
describe?**
- **Answer:** *"whatever makes the most sense and will cause the least
  amount of confusion"*. The owner delegated the choice. It was made in the
  parent session as **option (b), with a *provisional* marker added**, and C8
  carries it.
  - **Why (b).** Under (a), at 08:00 the gauge is two closes old while every
    other surface in the terminal (positions, Markets, the calendar) is on
    yesterday's close. Then at about 11:15 ET it jumps forward a session.
    Under (b) the gauge is on the same close as everything else, and only
    the two FRED rows carry an older date. (b) also meets the parent's *Done
    when* item 3 as written.
  - **Why the marker.** (b) has one surprise of its own. When FRED posts
    mid-morning, the composite for the same close *D* is recomputed with the
    newer VIX and junk values, so a figure labelled "as of Nov 16" can move a
    point with no market event to explain it. So **while any component lags
    *D*, the composite says it is provisional and names what it is waiting
    for** (for example, *"Provisional: VIX and junk from Nov 13; FRED posts
    mid-morning"*). The marker clears by itself once all included components
    are dated *D*. A move while the marker is showing is then expected, not
    unexplained.
  - **Rejected:** (a), for the reasons above. Also rejected is (b) without
    the marker, because a same-dated number that changes mid-morning is the
    exact confusion the owner asked to avoid.

*As asked:* FRED posts each close the next morning. On 2026-11-17 (*Constraints*),
`VIXCLS` for Nov 16 was posted at **09:37 ET** and `BAMLH0A0HYM2` for Nov 16
at **10:44 ET**. The five Alpaca components for a session are in hand by
17:00 ET that day. So from 17:00 ET on day D until about 11:00 ET on D+1, the
store holds D for five components and only D−1 for volatility and junk.
Bond-market holidays the NYSE trades through (Columbus Day, Veterans Day) give
junk no value for that session at all.

- **(a) One date for every component.** The composite is as of the latest
  session for which every non-forming component has a value dated that
  session. At 08:00 it is **two closes old** (D−1), until the FRED job lands
  around 11:15 ET. On a bond holiday, junk is excluded for that session and
  the composite says "6 of 7".
- **(b) The latest close, with stated lags.** The composite is as of the
  latest session D for which the five Alpaca components have values. Each
  component uses its newest value dated D or earlier, **at most one session
  older than D**. Each lagged component prints its own date ("VIX: Nov 13
  close"). Anything older than that is excluded as stale, and the count drops.
  At 08:00 the composite is as of yesterday's close, with VIX and junk from
  the session before.

*Trade-off:* (a) never mixes dates, which is the parent's *"as of that
session"* read literally. But at the hour the owner reads it, it is a session
behind. That would fail the parent's *Done when* item 3 (*"prints as of
yesterday's close"*) every morning before 11:15 ET. (b) meets item 3. It is a
different claim, a composite partly from yesterday, and it has to print that
on every lagged row. The code differs only in the read rule in U2 and in the
lag fields U7 serves. The stored data is the same either way. *Planner
recommendation:* (b), since it is the only option that meets *Done when* item
3 as written. This is a recommendation, not an assumption. U2 is not built on
it until the owner answers.
*Blocks:* U2's as-of rule, and through it U7, U8 and U9. The other units do
not depend on the answer.

**Q29: What is *D* when some, but not all, of the five Alpaca components have
no row?** *Added 2026-11-23. This question was raised while U2 was being
built, not during planning. It was asked and answered the same way as Q27
and Q28.*
- **Answer, 2026-11-23:** *"ii for the decision"*. Option (ii) is taken, and
  it is read as below. The reading is the parent session's, and it applies
  uniformly to every case where a component has no rows or its rows have
  stopped.
  - ***D* is the newest session among the five Alpaca components' newest
    rows.** It is still subject to C8's fixed rule that *D* is never a
    session whose close + 60 minutes has not passed. A row dated after that
    session is not a candidate: *D* is always a session with an Alpaca row,
    never a capped date that has none.
  - Every component, Alpaca or FRED, uses its newest value dated *D* or
    earlier, at most one session older than *D*. This is Q28's lag rule,
    unchanged.
  - A component with **no rows at all** is excluded as `no_value`. One whose
    newest row is **more than one session older than *D*** is excluded as
    `stale`. The count ("N of 7") drops in both cases.
  - **A one-session lag on an Alpaca component** is included as a lag and
    sets the provisional marker, naming the component, exactly as VIX and
    junk do. An example is a put/call gap on *D*. This means a put/call
    outage no longer holds *D* back for the other six, which matches C13's
    reason for running separate jobs.
  - **No Alpaca rows at all** means no *D* and no composite. The result
    states the reason, and the code does not raise.
  - `previous` is derived by the same rule as of the session before *D*
    (C9). Like-with-like (C9) and stale (C14) are unchanged.

*As asked:* C8 read literally (*"D is the latest session for which the five
Alpaca components have values"*) conflicts with U5's *"No seed, no row ...
The other five are unaffected"*. It also conflicts with put/call before its
first run, because put/call has no history to load. Read literally, C8 gives
no *D*, and so no composite, until all five have rows. A component whose rows
*stopped* would also freeze *D* for all seven.
- **(i) C8 read literally.** There is no composite until every Alpaca
  component has a row, and *D* freezes at the last session all five share.
- **(ii) *D* comes from the components that have rows.** Absent components
  are excluded as `no_value` and stopped ones as `stale`, and the count
  drops.
*Blocked:* U7, whose route would otherwise have raised on every fresh
install until put/call's first write.

**3. a: What does `members` store for breadth?** *Added 2026-11-24, raised
while U3 was being built.*
- **Answer, 2026-11-24: option (a).** For breadth, `members` is the number
  of names counted, the names behind the volume totals. That matches the
  *Database* table ("eligible names") and U1's `breadth()`. C3's
  "`members` stores the denominator" read wrongly for breadth, whose
  denominator is a volume, and is amended there.
- *Also 2026-11-24, recorded in C1:* a `breadth` recomputed inside the
  recheck window after a split can differ from the stored one, because
  split adjustment rescales earlier volume. Accepted and documented, with
  no code change.

---

## Problem

Step 8 is the last step of Phase 3 that puts numbers on two pages that are
still fixtures. Read on 2026-11-17 at `master` `d320bab`, four things stand
between the tree and the parent's step 8 *Done when*.

**1. Nothing computes the composite.** `corollary/data/macro/` holds a stub
docstring (*"Not implemented yet"*) and `risk_free.py` (step 3's `DGS3MO`
rate). `fred_observation` (migration 0007) stores `DGS3MO` only. No job
fetches `VIXCLS` or `BAMLH0A0HYM2`. There is no `composite_component_value`
table, and neither `/api/news/composite` nor `/api/markets/pulse` exists.

**2. Both pages derive the composite on the client, from fixtures.**
`SentimentGauge.tsx` and `Research.tsx` each call `compositeScore`
(`web/src/lib/news.ts`) over `SENTIMENT_COMPONENTS`. That is `Math.round` of a
mean of integer fixture scores. The day's change subtracts a stored
`SENTIMENT_PREVIOUS = 51`. Pulse's VIX and top sector are `MARKET_PULSE`
constants. The fixture's put/call row reads *"CBOE equity put/call, 5-day
average"*, which is not the owner's proxy (Q2: SPY's chain).

**3. Three surfaces still read the fixture halt.** Decision 16 named two of
them, and a third has the same problem:

- `ChainOrderTicket.tsx:102` reads `useUIStore((s) => s.isHalted)`.
- `Research.tsx:45` reads the same store, and gates Execute on
  **`executionMode === 'auto' && isHalted`**. So in Manual mode no halt
  disables Execute at all, against PRD §8.5 (*"Execute is disabled while the
  engine is halted"*). The Dashboard gates on `engine?.halted` in both modes.
- `CommandPalette.tsx:219` reads the real halt, but ORs in the fixture halt,
  and reads the real one as `engineQuery.data?.halted ?? false`. Its own
  comment says *"Both sides of this move to the API together"*.

`useUIStore.isHalted` has no production writer: `store.halt()` is called only
from tests, and `store.flatten()` has no production caller (PRD §11). So in
practice the fixture halt is always false, and the two surfaces that read it
offer to open a position while the engine is halted.

**4. The parent's arithmetic for this step is out of date.** Its step 8 entry
waits on *"the holdings seed... until Q17's guard decision lands"*. That was
resolved on 2026-10-01: the 2026-06-30 N-PORT snapshot is accepted, and the
loader serves all eleven funds. *Feeds and budgets* gives the composite "~1–2
pages/day", but strength needs a 252-session lookback for every name on every
recomputed day. It says the top sector "shares the Markets request", but the
Markets universe carries only one sector fund (XLE). It also puts the FRED
fetch at 10:00 ET, before junk posts. See *Feeds and budgets*.

---

## Constraints verified against the vendors

**Read** means read on 2026-11-17 from the vendor's public page or docs, with
no key. **Probed** means observed against the live service, and every probe
cited here is the parent's step 0 of 2026-09-24. The planning session cannot
read `.env`, so it made no keyed call. U0 turns the open items into probes.

### FRED: `VIXCLS` and `BAMLH0A0HYM2`

**Read, 2026-11-17**, from `fred.stlouisfed.org/series/VIXCLS` and
`/series/BAMLH0A0HYM2`:

| | `VIXCLS` | `BAMLH0A0HYM2` |
|---|---|---|
| Title | CBOE Volatility Index: VIX | ICE BofA US High Yield Index Option-Adjusted Spread |
| Units | Index, NSA | Percent, NSA |
| Frequency | Daily, Close | Daily, Close |
| Latest observation | 2026-11-16, 22.91 | 2026-11-16, 3.34 |
| Page "Updated" | **Nov 17, 2026 8:37 AM CST (09:37 ET)** | **Nov 17, 2026 9:44 AM CST (10:44 ET)** |
| Source | Chicago Board Options Exchange | ICE Data Indices, LLC |
| Rights | *"Copyright, 2016, Chicago Board Options Exchange, Inc. Reprinted with permission."* | *"Reproduction of this data in any form is prohibited except with the prior written permission of ICE Data Indices."* Distribution of top-level data to third parties is restricted. |
| History | not stated on the page | *"Starting in April 2026, this series will only include 3 years of observations"* |

Consequences:

- **Junk posts after the existing 10:00 ET FRED slot** (`FRED_DAILY_AT`,
  `engine/scheduler.py`). A 10:00 fetch on 2026-11-17 would have had junk only
  through Nov 13. That is one sample (*Not verified*). It is why U4's job runs
  at 11:15 ET, and why the close-hour job fetches FRED again.
- **Three years of junk** is enough for a 252-session window with room
  (the parent made this point as well).
- **Rights.** Both series stay as the parent left them: displayed locally,
  never exported (decision C15).

**Probed, 2026-09-24 (parent).** All three series answer 200. Values are
strings. `"."` means no observation and is handled before `Decimal()`.
`tests/fixtures/fred/p3_observations_{vixcls,bamlh0a0hym2}.json` exist.
`FredProvider.observations(series_id, limit=)` returns the newest `limit`
observations, sorted by the provider, not by the vendor.

### Alpaca historical stock bars

**Read, 2026-11-17**, `docs.alpaca.markets/us/reference/stockbars.md`:

- `GET /v2/stocks/bars`. `limit` defaults to 1000, max **10000**, and counts
  **total data points across all symbols**, not per symbol. Results are sorted
  by symbol, then time. A page can hold only one symbol.
- **No maximum symbol count is stated.** The provider caps the ADV path at
  `ADV_MAX_SYMBOLS = 200` (`data/providers/alpaca.py`), and step 8 batches at
  that ceiling.
- `adjustment` takes `raw` (the vendor default), `split` (price and volume),
  `dividend`, `spin-off` or `all`. The provider sends `split` unless asked
  otherwise, and the sentiment audit already asks for `all`.
- `asof` defaults to today. It *"identifies the underlying entity"*, so a
  query spanning a rename returns the old ticker's bars under the new one.
- `feed` defaults to `sip`. The provider always sends
  `ALPACA_STOCK_FEED_HISTORICAL`.

### Alpaca daily bars are built from trades, and extended hours move only volume

**Read, 2026-11-17**, `docs.alpaca.markets/us/docs/market-data-faq.md`:

- *"Minute and daily bars are aggregated from trades."* A trade's SIP
  timestamp is *"truncated... to the day (in New York) for daily bars"*.
- Per the FAQ's condition table, **extended-hours trades (`T`, `U`) update
  daily volume but not daily open, high, low or close**. Official-close
  prints (`M`) update nothing. Corrected consolidated closes (`9`) update
  OHLC but not volume.
- **Consequence.** A daily bar's close and range are regular-session figures.
  Its volume includes pre- and post-market volume, which keeps changing until
  20:00 ET, and a late `9` correction can move the close. A bar fetched at
  17:00 ET is provisional, which is why decision C1 rechecks a window.

### Alpaca option chain snapshots

**Read, 2026-11-17**, `docs.alpaca.markets/us/reference/optionchain.md`:

- `GET /v1beta1/options/snapshots/{underlying_symbol}`. `limit` max
  **1000** in total per response, paginated by `next_page_token`. Filters:
  `type` (`call`/`put`), strikes, expiries, `root_symbol` and
  `updated_since`.
- `dailyBar` and `prevDailyBar` are both `option_bar` objects (`t, o, h, l,
  c, v, n, vw`). **Neither is described**: the reference does not say which
  day either covers.
- Changelog 2026-11-10: the `feed` **default** is `opra` only with a
  real-time subscription and a signed OPRA agreement, otherwise `indicative`.
  The provider always sends `ALPACA_OPTIONS_FEED`, so the change does not
  reach it.

**Probed, 2026-09-24 (parent).** SPY at ≤10 DTE was one page of 1,000 that
paginated. **`dailyBar` is the contract's last *traded* session, not
today's**: 697 of 896 were dated the last session, and the rest were dated
anywhere back to six weeks. **`prevDailyBar.v` equalled the historical
1Day bar exactly on 5 of 5 active contracts**, so indicative volume is real
session volume. The fixture `tests/fixtures/alpaca/p3_option_snapshots_spy_indicative.json`
was re-serialised from parsed JSON, so its numbers have been through a float.

### Equity options extended hours start on 2026-12-04, and SPY is on the list

**Read, 2026-11-17**, `docs.alpaca.markets/us/docs/equity-options-eth.md`
(page updated that day):

- Sessions: a morning **07:30–09:25 ET** ("GTH") and an after-hours
  **16:00–16:15 ET** ("Curb"). Production go-live is targeted for
  **Dec 4, 2026**.
- 29 designated underlyings, **SPY among them**, with QQQ, IWM, GLD and XLF.
- *"Market data, including quotes and trades, will be available during both
  the Morning and After-Hours sessions."* Nothing is said about bars or
  volume.
- **Consequence.** The put/call series may change definition partway through
  its first 60 sessions, if GTH trades start counting in `dailyBar.v`
  (*Not verified*, re-probed after Dec 4).

### No index on this plan

Carried from the parent. Alpaca serves no index (VIX, SPX), and nothing in
`llms.txt` covers one. Finnhub's free `/quote` refuses `^VIX` (*"subscription
required for CFD indices"*) and answers `VIX` with an all-zero quote
(**probed 2026-09-24**). So **VIX is FRED's daily close** on Pulse and in the
composite, and never intraday.

### The code, at `master` `d320bab`

**Read:**

- **Migration head `0020`** (`0020_unverdicted_skip.py`). Step 8's one
  migration is `0021`, assigned at merge (Q22's rule).
- `AlpacaProvider.stock_bars(symbols, timeframe, start, end, limit,
  adjustment="split")` always sends the historical feed and clamps `end` to
  the 15-minute embargo. `option_snapshots(underlying, ...)` sends
  `root_symbol=underlying` and pages at 1,000. Every paginated request is
  capped at `_MAX_PAGES = 50` and refuses beyond that. `stock_snapshots`
  uses the realtime feed (`iex`).
- `Bar` carries `Decimal` OHLC, an `int` volume, and `at` (the opening
  stamp). Alpaca stamps a daily bar at midnight New York.
- The Markets `UNIVERSE` (`api/routes/markets.py`) holds SPY, QQQ, IWM,
  **XLE** and ARKK among its funds. **Ten of the eleven sector funds are not
  in the Markets poll.**
- The scheduler's schedule types are `EveryWhileOpen`, `AtTime`,
  `EveryInterval`, `TwoRate`, `WatchTierCadence` and the econ cadence. **None
  is "the calendar's close plus an offset".**
- `api/schemas.py`: *"Money is `JsonMoney`, and that is **the only float in
  the codebase**."* `JsonMoney` already serialises non-money exact values,
  such as the chain's `riskFreeRate`.
- `GET /api/settings/sources`'s FRED row says *"The macro series are a later
  step."*
- `FredObservationRecord`'s docstring says the table is *"Not exported...
  no route serves this table as CSV or in bulk; derived figures only."*

### Not reached

`cnn.com/markets/fear-and-greed` answered **HTTP 451**, so CNN's exact
component formulas were not read. Step 8 mirrors CNN's **list of seven**,
which is what PRD §8.3 and Q2 name. It does not claim to mirror the formulas.
Every proxy is labelled as one (C2).

### Not verified

Each item is a check at implementation time, and most are U0's. Each one
states what its answer changes.

1. **The SPY chain at the close.** The whole-chain snapshot's size and page
   count (the parent guessed ~9 pages), and whether at 17:00 ET every
   contract that traded that day carries a `dailyBar` dated that day,
   16:00–16:15 trades included. *Changes:* the put/call slot (C4) and the
   budget row.
2. **What `prevDailyBar` is.** Is it the contract's previous *traded*
   session, or the previous calendar session? *Changes:* nothing in the
   design, which reads `dailyBar` only. It is recorded because the
   alternative design in C4 depends on it.
3. **Whether a contract that expired today is still in the chain snapshot
   after the close, and still there before the next open.** SPY lists daily
   expiries, so 0DTE contracts carry much of the day's volume. *Changes:* if
   they remain until the next open, a start-up catch-up before the open may
   record a missed session's put/call (C4). If they vanish at the close or
   overnight, a missed session is a gap.
4. **Whether extended-hours option trades (from Dec 4) count in
   `dailyBar.v`.** *Changes:* nothing in the code. If they do, the put/call
   row's description gains a dated note that the series includes GTH volume
   from that date. Re-probed in the first week after go-live.
5. **That a daily bar's close excludes extended hours,** as the FAQ table
   says. U0 compares SPY's daily close with its last regular-session minute
   bar on one day. *Changes:* if it does not hold, momentum, strength and
   safe haven take the 15:59 minute bar's close instead, a bigger fetch.
6. **How much a provisional 17:00 ET daily bar moves by the next day.** U0
   fetches the universe at 17:00 and again the next morning, and reports the
   largest change in close, in volume share, and in each raw value. *Changes:*
   `RECHECK_SESSIONS` (C1), if a value still moves after five sessions.
7. **The real symbol cap on `/v2/stocks/bars`.** U0 tries 200 and 503.
   *Changes:* only the batch size.
8. **FRED's posting times**, beyond the one sample above. *Changes:* the
   11:15 ET slot, if junk often posts later. The close-hour refetch bounds the
   damage either way.
9. **`BAMLH0A0HYM2`'s real depth on FRED.** U0 counts it. *Changes:*
   nothing, unless it is under 252 sessions.
10. **How many of the ~503 seed members lack two years of bars,** whether
    from recent listings, spin-offs, or renames `asof` does not map. The
    history load reports this. *Changes:* nothing. Those names drop out of
    strength (C3) until they have 252 sessions.
11. **Whether all eleven sector-fund snapshots come back on `iex`.** The
    parent's probe saw lists come back short. *Changes:* nothing. Pulse ranks
    whatever returns and says how many (C11).
12. **CNN's formulas** (above). *Changes:* nothing. Step 8 does not claim
    them.

---

## Decisions

Each one records what it rules out. "Parent" means
`2026-09-23-phase-3-context-design.md`.

### C1. Raw values are dimensionless, one per session, rechecked for five sessions and then frozen

`composite_component_value` holds one raw value per (session, component), as
parent decision 5 requires. Step 8 adds three rules.

- **Ratios, not prices.** Each raw value is a ratio, a difference of returns,
  a fraction, or (for the two FRED series) the published level. A later split
  or dividend adjustment rescales every earlier price by one factor, which
  leaves a ratio unchanged. So a stored value ~~never goes stale against
  re-adjusted bars~~ built from prices never goes stale against re-adjusted
  bars. *Amended 2026-11-24 (owner decision, accept and document, no code
  change):* volume is the exception. A split rescales a name's earlier
  split-adjusted volume, so a `breadth` recomputed inside the five-session
  recheck window after a split can differ from the stored one. Frozen rows
  never change. This is accepted because:
  - split adjustment is required, or a split reads as a crash;
  - the up/down classification uses adjusted prices and is unaffected;
  - it touches about 1 of 503 names for at most five sessions;
  - the recheck window exists for revisions;
  - fetching raw volume as well would double the job's requests.
- **A rolling recheck.** Every run recomputes, and upserts, each session from
  `RECHECK_SESSIONS = 5` sessions before the newest stored row through the
  latest completed session. Older rows are never rewritten. A 17:00 ET bar is
  provisional (extended-hours volume until 20:00, late corrections), and the
  next day's run settles it.
- **Quantized on the way in.** Each value is quantized to `RAW_QUANTUM =
  1E-10` (`ROUND_HALF_EVEN`), so the stored text is deterministic. Every
  input is `Decimal` and stays `Decimal`.

*Rejected:*
- **Storing prices or bars.** A dividend adjustment would rewrite them.
  Phase 5's Parquet store is where bars live.
- **Write-once with no recheck.** It freezes a provisional bar as fact.
- **Recomputing all history on every run.** A new SPDR snapshot would
  silently rewrite a year of strength readings computed over the previous
  universe.
- **Storing z, scores or the composite.** Parent decision 5.

### C2. The seven components, and what each one is a proxy of

Owner decision Q2 names the sources. This table is how each raw value is
computed. The definitions are this spec's, so the owner can override any of
them. Higher z means greed after orientation.

| Key | Raw value for session *t* | Bars and source | Orientation | Proxy of |
|---|---|---|---|---|
| `momentum` | close(SPY, *t*) ÷ mean of SPY's 125 closes ending *t*, − 1 | daily, `split` | + | S&P 500 vs its 125-day MA |
| `strength` | (names at a 52-week high − names at a 52-week low) ÷ eligible names (C3) | daily, `split` | + | NYSE 52-week highs vs lows |
| `breadth` | (advancing volume − declining volume) ÷ total volume, over the universe | daily, `split` | + | advance/decline volume (CNN uses a summation index) |
| `put_call` | Σ put `dailyBar.v` ÷ Σ call `dailyBar.v`, SPY chain, contracts whose `dailyBar` is dated *t* (C4) | option snapshot | − | CBOE put/call ratio |
| `volatility` | VIX(*t*) ÷ mean of the 50 VIX observations ending *t*, − 1 | FRED `VIXCLS` | − | VIX vs its 50-day MA |
| `safe_haven` | 20-session SPY return − 20-session TLT return, ending *t* | daily, **`all`** | + | stock vs Treasury returns |
| `junk_bond` | the OAS as published, percent | FRED `BAMLH0A0HYM2` | − | junk vs investment-grade spread |

- **Windows count sessions or observations, never calendar days.** "125
  closes" means 125 NYSE sessions with a bar. "50 VIX observations" means 50
  non-missing ones.
- **`safe_haven` uses total-return bars (`adjustment=all`).** TLT's coupon is
  most of its return over 20 sessions, and a price-only return would score
  every ex-date as stocks beating bonds. `momentum` uses `split` bars,
  because the thing it stands in for is a price index.
- **No smoothing.** Breadth and put/call are daily figures, so they are
  noisier than CNN's 5-day and summation forms. *This is an assumption the
  owner can override:* a smoothing window is one constant per component, and
  it would change no table.
- **A raw value is missing, not zero, when its inputs are missing.** No SPY
  bar on *t*, zero call volume, fewer than 125 closes, or a FRED `"."` means
  no row for *t*. Each case is logged with its rule (rule 8 below).

*Rejected:*
- **CNN's exact formulas.** They were not read (HTTP 451), and a McClellan
  summation index needs a cumulative history this plan does not have.
- **`raw` bars.** A split would read as a 52-week low.
- **IEX for anything.** CLAUDE.md's `min_avg_volume` warning applies to
  breadth word for word. The bars job refuses to compute `breadth` or
  `strength` unless `ALPACA_STOCK_FEED_HISTORICAL` is `sip`, and logs the
  rule. The feed name is recorded in `source`.

### C3. Strength and breadth run over the current accepted seed, survivorship and all

The universe is every symbol in the latest accepted SPDR snapshot
(`load_spdr_seed_from_db`), about 503 names. History uses the same universe.

- **Eligibility for strength on *t*:** a bar on *t*, and a first bar on or
  before the first session of the 252-session window ending *t*. Interior
  gaps are allowed, and the high and low are taken over the bars that exist.
  A 52-week high is `high(t) ≥` the maximum high over that window, so a tie
  counts as a high, and a 52-week low is the mirror of that.
- **Breadth on *t*:** names with bars on *t* and on the session before.
  Advancing means close above the previous close, declining means below, and
  unchanged counts only toward the total.
- **A fraction, not a count,** so a 497-name day and a 503-name day are
  comparable. ~~`members` stores the denominator,~~ `members` stores the
  names counted: the denominator for strength, and the names behind the
  volume totals for breadth *(amended 2026-11-24, owner decision 3a)*. And
  `universe_as_of` stores the seed's report date.
- **No seed, no row.** Without an accepted snapshot the two components are
  not computed, and the run logs why. The other five are unaffected.
- **A new snapshot applies from the next run on.** Rows already stored keep
  the universe they were computed over (C1).

**Known bias, recorded rather than corrected:** history is computed over
today's members, and that is survivorship. Names dropped from the index in
the past two years, which were likelier to be making lows, are absent from
the history. The z window is a year, so the effect on a z-score is a bias in
the mean, not a sign error.

*Rejected:*
- **A point-in-time universe** from earlier N-PORT filings. About eight
  quarters × 11 funds of filings, plus CUSIP and ISIN resolution for every
  name that left, for a display figure. It is the owner's to ask for.
- **A raw count.** It drifts with universe size.
- **Closes for highs and lows.** "Hitting a 52-week high" is an intraday
  event, and the bar's high is its regular-session high (FAQ above).

### C4. Put/call is accumulated forward from one snapshot after each close

- **What is read.** The whole SPY chain (`option_snapshots("SPY")`, no expiry
  or strike filter, `root_symbol=SPY` as the provider already sends). Put or
  call is taken from the OCC symbol, and adjusted roots are dropped as the
  chain already drops them.
- **The session filter.** A contract counts toward session *t* only if its
  `dailyBar` is dated *t*. That is the parent's finding: an unfiltered sum
  mixes in contracts last traded weeks ago.
- **When.** At the calendar close + 60 minutes (17:00 ET, or 14:00 on a
  half-day). SPY options trade to 16:15 and the indicative feed runs 15
  minutes late, so this is the earliest slot that holds the whole session.
- **Written once** per session. A session with no snapshot taken in time is
  a **gap**: the forming count does not include it, and nothing invents it.
- **A start-up catch-up before the open is allowed only if U0 proves it
  safe** (*Not verified* 3). If the session that just closed has no row, no
  later session has opened, and U0 showed expired contracts stay listed
  until the next open, the catch-up reads it. Otherwise it records the gap
  and logs why.
- **Forming** for its first 60 sessions (parent decision 5). That is about
  three months at one session a day, longer for every gap.

*Rejected:*
- **A backfill from historical option bars.** The parent's *Out of scope*:
  Phase 5's bulk download is where that becomes cheap.
- **Re-deriving session *t* the next day from `prevDailyBar`.** The
  contracts that expired on *t*, a large share of SPY's volume, are likely
  gone from the chain by then (*Not verified* 3). The ratio would then
  silently exclude 0DTE.
- **CBOE's own put/call statistics.** Free, reachable, and forbidden to
  automate (Phase 2 decision 15).
- **A ≤N-DTE chain.** The owner chose SPY's chain, and a DTE cut is a
  second definition nobody asked for.

### C5. VIX and junk come from FRED through a new job, at 11:15 ET and again after the close

`fred_macro` fetches `VIXCLS` and `BAMLH0A0HYM2` through the existing
`FredProvider.observations` and stores them in the existing
`fred_observation` by the existing `store_observations` upsert. It then
recomputes `volatility` and `junk_bond` over the recheck window.

- **Slots:** 11:15 ET on NYSE trading days, after junk's observed 10:44 ET
  posting. The close-hour prices job (C13) also calls the same fetch, so a
  late posting is picked up the same day.
- **Depth:** `limit=400` when a series has fewer than 320 stored
  observations (≥301 are needed for 252 raw `volatility` values). Otherwise
  `limit=10`, a fortnight, the way `DGS3MO` works.
- **A date that is not an NYSE session** (a bond-market day the NYSE closed)
  is stored, as FRED published it, and belongs to no session. The raw value
  for session *t* is computed from observations dated on or before *t*.

*Rejected:*
- **The 10:00 ET `fred_dgs3mo` slot.** Junk had not posted at 10:00 on
  2026-11-17.
- **Adding the series to `fred_dgs3mo`.** That job's skip-and-adopt rules
  are about the risk-free rate (decision 19). Mixing them in would make a VIX
  outage read as a rate outage.
- **VIX ETFs (VIXY, VXX) as an intraday VIX.** They are futures products,
  not the index, and they decay. A number labelled VIX that is not VIX is
  the invented-figure failure §8.5 exists to prevent.

### C6. The window, forming, and z

- **The window for as-of session *D*** is the trailing **252 NYSE sessions
  ending *D***, from `calendars.py`. A component's window holds its stored
  values dated within that span, at most one per session. Gaps shorten the
  count. They never stretch the window further back in time.
- **Forming:** fewer than **60** values in the window. A forming component
  is shown with its count ("forming, 23 of 60 sessions") and excluded from
  the composite (parent decision 5).
- **z = (x_D − mean) / s,** where *x_D* is the component's value for *D*
  (per the as-of rule, C8), the window includes *x_D*, and *s* is the
  **sample** standard deviation (n − 1). Computed in `Decimal` under an
  explicit 40-digit `localcontext`, with `Decimal.sqrt`.
- **s = 0** (a constant window) means no z. The component is excluded with
  the reason "no variance in the window", never treated as z = 0.

*Rejected:*
- **The last 252 observations, whenever they fell.** After a long gap that
  window reaches back more than a year and scores today against a different
  market.
- **The population standard deviation.** It is the wrong estimator for a
  sample window, though the difference at n = 252 is about 0.2%.

### C7. Scores are integers, Φ is exact, and the composite is the mean of what is printed

- **Score = `ROUND_HALF_UP`(100 · Φ(oriented z)), an integer from 0 to
  100.** Φ is computed in `Decimal`: the Taylor series of erf at 50 digits
  for |z| < 7. For |z| ≥ 7 the score is exactly 100 or 0, because
  100 · (1 − Φ(7)) < 1.3 × 10⁻¹⁰, which no integer rounding can see. z itself
  is never clipped, and it is served as computed.
- **Composite = `ROUND_HALF_UP`(mean of the integer scores of the
  non-forming, non-excluded components).** It is the mean of the numbers on
  screen, so anyone can recompute it from the printed breakdown and get the
  printed composite. The parent's *Testing* asks for exactly this
  (*"the composite equals the mean of the scores the endpoint prints"*).
- **"N of 7"** is served and printed whenever N < 7 (parent decision 5).
  Zero usable components means no composite, and the panel says why.

*Rejected:*
- **A float Φ** (`math.erf`, `statistics.NormalDist`). `schemas.py` says
  `JsonMoney` is *"the only float in the codebase"*, and a second float, even
  for a display figure, is a second exception to defend. The exact series is
  about 20 lines with a pinned test.
- **A composite from unrounded scores.** Seven components at 50.5 and 49.5
  print 51s and 50s, while the unrounded mean prints a composite one point
  off the mean of the rows beneath it. That is the disagreement §8.5 forbids.
- **Silent renormalisation** (parent decision 5).

### C8. The as-of rule. ~~**Blocked on Q28.**~~ **Q28: option (b), with a provisional marker (2026-11-17).**

Two rules are fixed whichever answer Q28 gets:

- The as-of session *D* is never a session whose close + 60 minutes has not
  passed.
- Every component carries its own `valueDate`.

Q28 decides only whether a component may lag *D* by one session, under
option (b), or must match *D*, under option (a). U2 builds the chosen rule as
one pure function with a test per boundary.

*Amended 2026-11-17, Q28 answered:* **the rule is (b).**

- ~~***D* is the latest session for which the five Alpaca components have
  values**, subject to the close + 60 minutes rule above.~~ *Amended
  2026-11-23 (Q29):* ***D* is the newest session among the five Alpaca
  components' newest rows**, subject to the close + 60 minutes rule above.
  Rows dated after the latest session whose close + 60 minutes has passed
  are not candidates, so *D* is always a session with an Alpaca row.
  A component with no rows at all is excluded with reason `no_value`. If no
  Alpaca component has a row, there is no *D* and no composite, and the
  result states why.
- **A component may lag *D* by at most one session.** This applies to Alpaca
  and FRED components alike (Q29). It then uses its newest value dated on or
  before *D*, and its `lagSessions` is 1. A value more than one session
  older than *D* is excluded with reason `stale`, and the count ("N of 7")
  drops.
- **`provisional` is true whenever any included component has `lagSessions`
  > 0.** That includes an Alpaca component, such as put/call missing *D*. The
  read result carries it, U7 serves it, and U8 and U9 render it in words,
  naming each lagged component and its date. It is derived on read like
  everything else, never stored, so it clears by itself once the lagging
  values for *D* land, or once *D* moves on past a gap that will never be
  filled (a put/call session with no snapshot, a bond-holiday junk gap).
- **Yesterday's change (C9) is unaffected.** A lagged component is still
  in the set, so lagging alone never trips *"component set changed"*.

*Rejected:* (a), and (b) without the marker. Both are under Q27 and Q28
above.

### C9. Yesterday's change is derived, and printed only when like is compared with like

`previous` is the composite as of the session before *D*, derived on read by
the same function, with windows ending at that session. The change in
points is printed **only when both days average the same set of
components**. Otherwise the panel says *"component set changed"*: on the
session put/call stops forming, a 6-of-7 against a 7-of-7 is a different
claim, not a move.

*Rejected:*
- **The fixture's stored `SENTIMENT_PREVIOUS`.** It is a copy that can
  drift.
- **A delta across different component sets.**

### C10. One server function feeds News and Pulse, and the client derives nothing

`corollary/data/macro/composite.py` exposes `read_composite(session, *,
now)`, a pure derivation over rows the caller loads. `/api/news/composite`
serves the full object. `/api/markets/pulse` embeds the same function's
summary (`asOf`, `composite`, `componentsAveraged`, `componentsTotal`), built
in the same request through the same schema type. The frontend formats both
with one helper. `compositeScore` and the fixtures behind it are deleted from
`web/src/lib/`. "Agrees to the digit" then holds by construction, and a test
asserts it.

*Rejected:*
- **Keeping `compositeScore` on the client.** Two derivations of one number
  is how the page and the breakdown come to disagree.
- **Pulse computing its own composite, or caching one.**

### C11. Pulse: VIX is FRED's last close, and the top sector comes from one snapshot request

- **VIX:** the newest non-missing `VIXCLS` observation, with its date. The
  day's change is against the observation before it, derived rather than
  stored (as the fixture already does). `intraday: false`, and the label
  reads *"last close, FRED (CBOE)"*. That is PRD §8.5's amendment, made true:
  the parent's step 0 found no intraday source on this plan.
- **Top sector:** one `stock_snapshots` request for the eleven sector funds,
  ranked by the Markets table's own change rule (`_change_pct` over
  `snapshot.price` and `previous_close`), ties broken by symbol. It is cached
  in process for 60 seconds, so two tabs cost one request. It is served with
  the session date of the bars it read, the feed (`iex` on Basic), and how
  many of the eleven answered. With no market-data provider (no keys), the
  card says so, and the VIX and composite cards are unaffected.

*Rejected:*
- **"Shares the Markets request"** (the parent). The Markets universe holds
  one of the eleven funds.
- **Adding the ten missing funds to the Markets universe.** That changes a
  Phase 2 surface and its 26-name budget for one card.
- **Any intraday VIX proxy** (C5).

### C12. The real halt, everywhere, and fail closed while it is unknown

Decision 16, extended to the third reader and to two gaps the parent did not
see:

- `Research.tsx`, `ChainOrderTicket.tsx` and `CommandPalette.tsx` read halt
  state from `useEngineState()` only. The palette's `fixtureHalted` OR goes
  in the same change, so no production module reads `useUIStore.isHalted`.
- **Execute (Research) and Review (the chain ticket) are disabled whenever
  the engine is halted, in Manual as in Auto.** PRD §8.5 says so, and the
  Dashboard already does it. Research's `executionMode === 'auto' &&` is
  removed.
- **While the engine state is loading or has failed, they are disabled too,
  and say which** (*"Reading engine state"* or *"Engine state unavailable:
  …"*). The palette omits Execute in both cases, as it does when halted.
  Dismiss is never gated.
- **A guard test** fails if any file under `web/src/` other than
  `lib/store.ts` and test files reads `isHalted` from the store.

The store's `isHalted` field and `halt()`/`resume()` stay. `flatten()`'s
test is where rule 7's distinction is written down (PRD §11's Phase 6
carry-forward), and pruning the store is Phase 6's question.

*Rejected:*
- **`?? false`.** It reads "not halted" before anyone has answered, which is
  the Dashboard pill's one wrong answer, and here it would enable a control.
- **Keeping the Auto-only gate.** Halt stops new entries (rule 7). A
  human-clicked entry while halted is still a new entry.
- **Leaving the palette's OR.** Decision 16's reason is *"two halts in one
  application"*. Fixing two surfaces of three leaves exactly that.
- **Deleting `isHalted` from the store** (above).
- **Changing the three "Submits to the risk manager" surfaces.** Phase 6,
  per the parent.

### C13. Three jobs, calendar-clocked, never rule 9 producers

| Job | Slot | Catch-up at start | What it writes |
|---|---|---|---|
| `fred_macro` | 11:15 ET, NYSE trading days | if either series' newest stored row is more than two sessions old, or has fewer than 320 observations | `fred_observation`; `volatility` and `junk_bond` raws |
| `composite_prices` | calendar close + 60 min, trading days | if any of its four components' newest row is older than the latest completed session | `momentum`, `strength`, `breadth`, `safe_haven`; also calls `fred_macro`'s fetch |
| `composite_put_call` | calendar close + 60 min, trading days | only under C4's pre-open condition | `put_call` |

- **A new schedule type,** `AfterClose(offset, days)`: the calendar's close
  for the day plus `offset`, so a half-day runs at 14:00. Added to
  `engine/scheduler.py` beside `AtTime`, with a test across a half-day and a
  holiday. Inside the weekday runner's window (Q24d's 18:05 ET stop).
- **The history load is a catch-up, not a backfill.** An empty component
  loads its whole window from market prices (parent: *"a load of market
  prices for the z-score window, not a backfill of labels"*). Put/call has no
  history to load.
- **Rule 9:** each job is a context job (parent decision 1). Its module
  imports no runtime, no `engine.state`, no socket and nothing under
  `corollary.api`, and `context_jobs`' output pulls it into the existing
  `risk`-marked isolation tests. All parsing and arithmetic over the ~253,000
  history bars runs in `asyncio.to_thread`, because a CPU-bound loop on the
  event loop stalls the socket readers the watchdog judges. Sliding-window
  maxima and minima use a monotonic deque, so the load is linear in bars.
- **New services.** `ContextServices` gains `macro_prices`, a narrow
  `Protocol` over `stock_bars` and `option_snapshots`, which in production is
  the `AlpacaProvider` (market data, never the broker). `fred` already
  exists. The isolation walker covers the new field without change.
- **Failure.** A raise is logged by the scheduler with the job's rule and
  inputs, and the job's next slot comes round. Nothing notifies: a stale
  composite says so on its panel (C14), as every other context feed does.
  *Amended 2026-11-23 (Q29):* the whole panel reads stale only when **both**
  Alpaca jobs fail, since either job's row moves *D*. When one job fails,
  its components show as lagged on the first session (the composite is
  provisional and names them). From the second session they are excluded,
  each with reason `stale`, and the count drops. The failure is still on the
  panel in words, but as lags and exclusions rather than as C14's stale
  sentence.

*Rejected:*
- **One combined job.** A put/call outage would then stop momentum.
- **Running at 16:30 ET** (the parent's row). The indicative feed is 15
  minutes behind SPY's 16:15 option close.
- **A new notification event.** A composite failure is a stale panel, or
  under Q29 a provisional or reduced one (see *Failure* above), not an
  alert. No `notification_route` row, so no migration for one.

### C14. "Stale" is derived from the rows and the calendar

The composite is **stale** when its *D* is older than the latest NYSE session
whose close + 60 minutes has passed. The API serves `expectedAsOf` beside
`asOf`, and the panel reads *"stale: as of Nov 13; Nov 14's close not
computed"*. The scheduler's in-memory status is not read: a restart would
erase it, and the newest row is the durable answer (`engine/scheduler.py`'s
own note).

*Rejected:* `Scheduler.status()` as the source of staleness, because it does
not survive a restart.

### C15. The FRED raw series is displayed, never exported

The composite endpoint serves each component's **latest** raw value for
display: the OAS beside the junk row, and the VIX close on Pulse. It serves
no history, no series route and no CSV of either. This is the parent's
reading: displaying one value in a single-user local terminal is not
reproduction in the redistribution sense, but the series is not exported.
`FredObservationRecord`'s *"derived figures only"* is reworded to match in
U11. *This is an assumption the owner can override:* the alternative is to
serve junk's z and score only, which is one field.

*Rejected:* a history endpoint or CSV export of `fred_observation` or of
`composite_component_value` for the FRED components.

---

## Design

### Module layout

```
corollary/
├── calendars.py                  # + nyse_sessions_between(start, end) (U1)
├── data/macro/
│   ├── __init__.py               # docstring: no longer a stub (U11)
│   ├── risk_free.py              # unchanged
│   ├── components.py             # C2/C3: the seven raw computations — pure (U1)
│   ├── composite.py              # C6–C10: window, z, Φ, scores, as-of, previous — pure (U2)
│   ├── store.py                  # upsert with the recheck window; window reads (U3)
│   ├── fred_macro.py             # C5: fetch, store, recompute volatility/junk (U4)
│   ├── prices.py                 # C3: bars → momentum/strength/breadth/safe_haven (U5)
│   └── put_call.py               # C4: chain snapshot → put_call (U6)
├── db/models.py                  # + CompositeComponentValue (U3)
├── engine/scheduler.py           # + AfterClose; + the three jobs; + ContextServices.macro_prices (U4–U6)
└── api/
    ├── routes/news.py            # + GET /api/news/composite (U7)
    ├── routes/markets.py         # + GET /api/markets/pulse (U7)
    ├── routes/settings.py        # FRED source row (U7)
    └── schemas.py                # CompositeComponent, CompositeSummary, Composite, MarketPulse (U7)
scripts/probe_phase3_step8.py     # U0
web/src/
├── components/SentimentGauge.tsx # from the query (U8)
├── pages/News.tsx                # composite marker off (U8)
├── pages/Research.tsx            # Pulse from the query (U9); real halt (U10)
├── components/ChainOrderTicket.tsx, CommandPalette.tsx   # real halt (U10)
└── lib/{api,queries,types,news,mockData}.ts             # hooks, types; fixtures and compositeScore removed (U8, U9)
```

`components.py` and `composite.py` import nothing from the database, the
scheduler, the providers or the API. Their inputs are plain dataclasses of
`Decimal`, `int` and `date`, which is what makes them testable by hand
computation.

### Feeds and budgets

| Feed | Source | Cadence | Bucket | Spend |
|---|---|---|---|---|
| VIX and junk | FRED `VIXCLS`, `BAMLH0A0HYM2`, `limit=10` (`400` on first load) | 11:15 ET trading days, plus the close-hour refetch | `api.stlouisfed.org` 120/min | 4 a day |
| Composite prices: SPY, TLT | Alpaca daily, `split` (SPY, 130 sessions) and `all` (SPY and TLT, 25 sessions) | close + 60 min | `data.alpaca.markets` 200/min | 2 a day |
| Composite prices: universe | Alpaca daily `split`, ~503 names in ≤200-symbol batches, 252 + recheck sessions | close + 60 min | `data.alpaca.markets` | **~15 a day** (≈131,000 points); **~27 once** for the history load (≈253,000 points, 504 sessions) |
| Put/call | Alpaca SPY chain snapshot, whole chain | close + 60 min | `data.alpaca.markets` | **~9 a day, unverified** (*Not verified* 1) |
| Top sector | `stock_snapshots`, 11 sector funds, realtime feed | per `GET /api/markets/pulse`, cached 60 s, polled at 60 s while Research is open | `data.alpaca.markets` | ≤1/min, and only while Research is open |

Against the parent's row: **the universe fetch is ~15 a day, not ~1–2.**
Every recomputed day's strength needs the 252-session lookback for every name,
and step 8 stores no bars (C1). All ~26 close-hour requests fall after the
close, when the 150/min Markets poll is idle or backgrounded. They wait on the
bucket (`HostRateLimiter`) rather than refuse. **In session step 8 adds only
the Pulse poll**, ≤1/min against the ~49/min the Markets poll leaves.

### Database

One migration, `0021` at merge (Q22's rule), in U3:

`composite_component_value`:

| Column | Type | Notes |
|---|---|---|
| `date` | `Date`, PK | the NYSE session the value describes |
| `component` | `String(16)`, PK | CHECK in the seven keys of C2 |
| `raw_value` | `Money` | signed, quantized to `1E-10` (C1) |
| `source` | `String(64)` | e.g. `alpaca_daily:sip`, `alpaca_option_snapshot:indicative`, `fred:VIXCLS`. CHECK non-empty |
| `members` | `Integer`, nullable | the count behind the value: eligible names (strength, breadth), contracts counted (put/call), else NULL. CHECK ≥ 0 |
| `universe_as_of` | `Date`, nullable | the seed's report date, for strength and breadth only. CHECK it is set exactly for those two |
| `computed_at` | `UtcDateTime` | last write |

- `Money` refuses SQL comparison, so readers load the window's rows and
  compare as `Decimal` in Python, as `db.seed.risk_limits` does.
- `fred_observation` is unchanged. Its docstring gains the two series (U11).
- **No new notification route, audit category or event** (C13).

### API

Both routes are plain `GET`s depending on no broker, so
`tests/api/test_account_mode.py`'s structural guards apply unchanged. Every
exact value serialises through `JsonMoney`, the existing display-boundary
number. Scores and counts are `int`, and dates are `CalendarDate`.

**`GET /api/news/composite`:**

```
asOf: date | null              expectedAsOf: date      stale: bool
provisional: bool              provisionalNote: str | null  # C8 (Q28 b): names each lagged component and its date
composite: int | null          componentsAveraged: int componentsTotal: 7
unavailableReason: str | null  # e.g. "no component has 60 sessions yet"
previous: {asOf, composite, componentsAveraged} | null
change: int | null             changeNote: str | null  # C9
computedAt: datetime | null    # newest computed_at among the rows used
components: [{
  key, name, description,      # description says what it is a proxy of (C2)
  proxyOf, proxy, source,      # e.g. "S&P 500 vs its 125-day MA" / "SPY vs its 125-session MA" / "alpaca_daily:sip"
  rawValue, rawLabel,          # display only (C15); rawLabel e.g. "SPY 2.1% above its 125-session MA"
  valueDate, lagSessions,      # lagSessions per C8: 0 or 1 when included; a stale component reports its actual lag
  observations, formingNeeds: 60, forming: bool,
  z, score: int | null,
  orientation: "greed_up" | "fear_up",
  excludedReason: null | "forming" | "stale" | "no_variance" | "no_value",
  members, universeAsOf
}]
```

**`GET /api/markets/pulse`:**

```
vix: {close, previousClose, change, date, intraday: false, source} | {unavailableReason}
composite: CompositeSummary      # the same type as /api/news/composite's head, same function (C10)
topSector: {etf, sector, changePct, sessionDate, answered: int, of: 11, feed} | {unavailableReason}
```

**`GET /api/settings/sources`**'s FRED row stops saying *"The macro series are
a later step"*. It names the three series it now reads.

### Frontend

- **News, `SentimentGauge`.** It reads `useComposite()`. There are seven rows,
  always. A forming row shows *"forming, 23 of 60 sessions"* in place of a
  bar. An excluded row shows its reason in words. *"6 of 7 components"*
  appears beside the headline whenever N < 7. The change line is C9's.
- **Every proxy is stated visibly**, not on hover alone. A *Sources* toggle
  (a button, so it has a keyboard state) expands each row to its proxy, its
  source, its value date and its raw value. The component's own comment
  already argues that hover-only does not exist on a screenshot or to a
  keyboard. The row's `title` keeps the one-line description.
- **The gauge's designed states:**
  - loading: a skeleton.
  - no rows yet: *"first computed after today's close, 17:00 ET"*.
  - forming: put/call's count.
  - stale: C14's sentence.
  - error: the request failed, and it says so.
  - populated.
  The composite's `FixtureMarker` comes off. The footer reads *"as of the Nov
  14 close · computed 17:02 ET"* instead of a fixture timestamp.
- **Research, Market Pulse.** Three `StatCard`s from `useMarketPulse()`,
  polled every 60 s while the page is visible:
  - VIX: last close, with the change and its date.
  - Composite: the same number, formatted by the same helper as News, with
    *"N of 7"* when it applies.
  - Top sector: name, fund and change, with *"10 of 11 answered"* when it
    applies.
  The page's title marker narrows to *"Recommendations, strategies,
  origination stats and the chat are sample data"*.
- **Removed:**
  - `compositeScore`, from `lib/news.ts`.
  - `SENTIMENT_COMPONENTS`, `SENTIMENT_PREVIOUS`, `SENTIMENT_AS_OF` and
    `MARKET_PULSE`, from `mockData.ts`.
  - The `SentimentComponent` shape, from `types.ts`, replaced by the API's.

  The typechecker proves nothing still reads them. `sentimentBand` and
  `sentimentTone` stay, since they map a served integer to a word and a tone.
- **The real halt** (C12) in Research, the chain ticket and the palette.

---

## Testing

The pure computations get the careful coverage. They are the only place a
wrong number originates. Everything else gets the standard coverage.

**U1, `components.py`, every component against a hand-computed fixture:**
- `momentum` on a 125-close fixture, and **no value with 124 closes**.
- `strength`:
  - a tie with the window maximum counts as a high.
  - a name with a first bar inside the window is ineligible.
  - interior gaps are allowed.
  - the fraction uses the eligible count.
- `breadth`:
  - unchanged closes count only toward the total.
  - a name missing the previous session is excluded.
  - zero total volume gives no value.
- `put_call`:
  - a contract whose `dailyBar` is dated a different session is excluded.
  - zero call volume gives no value.
  - an adjusted-root contract is excluded.
- `volatility` skips FRED gaps when counting 50 observations.
- `safe_haven` is computed on its own bars, and a test asserts `momentum`'s
  caller asks for `split` and `safe_haven`'s for `all`.
- **Ratio stability:** rescaling every price before a date by one factor
  leaves every raw value unchanged (C1's reason, pinned).
- **No float:** a test asserts no `float` reaches any function in the module
  (inputs typed and checked).
- `nyse_sessions_between` across a holiday and a half-day.

**U2, `composite.py`:**
- **The window:**
  - 59 values is forming, 60 is not.
  - a gap shortens the window rather than stretching it.
- **z:**
  - z on a hand-computed window.
  - z includes *x_D*.
  - s = 0 gives `no_variance`, not 50.
- **Φ:**
  - Φ(0) = 0.5 exactly.
  - Φ(1), Φ(1.96) and Φ(−2.5) match published values to 15 digits.
  - the score at |z| = 6.99 and at 7.
  - integer rounding just either side of a .5 boundary.
- **Orientation:** VIX above its MA lowers the score, and so do a higher
  put/call and a wider spread.
- **The composite:**
  - it equals `ROUND_HALF_UP` of the mean of the served integer scores,
    asserted over random fixtures, not one.
  - forming and excluded components are not averaged and are counted.
  - zero usable components gives no composite and a reason.
- **The as-of rule (C8, Q28 option b, Q29):** one test per boundary. A
  lag of 0 and of 1 session, and a lag of 2, which is stale, for FRED and
  Alpaca components alike. *D* is the newest Alpaca row, and rows after the
  latest closed session + 60 minutes are not candidates. An Alpaca component
  with no rows is `no_value`, and the rest still average. No Alpaca rows at
  all gives no composite and a stated reason, not an exception. A bond-holiday
  junk gap. The close + 60 minute boundary for *D*. `provisional` is true
  exactly when an included component lags *D*, false once every included
  component is dated *D*, and its note names each lagged component and date.
- **The previous composite** is derived over windows ending the session
  before. A different component set gives `change: null` and the note.

**U3, the store:**
- The migration upgrades and downgrades. The CHECKs refuse:
  - an unknown component.
  - empty `source`.
  - negative `members`.
  - `universe_as_of` on the wrong component.
- The upsert rewrites only rows inside the recheck window. A row older than
  five sessions before the newest is never touched, even when newer inputs
  arrive.
- The model's component CHECK and C2's key list are the same set.

**U4–U6, the jobs, on recorded fixtures:**
- **The history load:** an empty table loads the window and a second run is
  a no-op.
- **Rule 9:** a fetch failure raises, is logged with the rule, and leaves
  earlier rows untouched. The three jobs are inside the `risk`-marked
  isolation tests (`test_the_shipped_context_jobs_never_move_the_halt_state`,
  `test_shipped_context_jobs_hanging_past_the_watchdog_window_never_halt_or_delay_the_heartbeat`)
  without the tests being edited to name them.
- **`breadth` and `strength` refuse a non-`sip` historical feed** and log
  the rule.
- **No seed:** strength and breadth are skipped with the reason, and the
  other two price components still write.
- **Put/call:** a session with no snapshot by the next open is a gap, never
  a row. The pre-open catch-up is exercised only in the form U0 allows.
- **`AfterClose`:** a full day gives 17:00 ET, a half-day 14:00 ET, and a
  holiday no slot, on both sides of the DST change.
- **The history computation runs in `to_thread`:** a test asserts the event
  loop stays responsive while a 253,000-bar fixture is computed.

**U7, the API:**
- Both routes on a seeded store: populated, forming, stale, and empty.
- **News and Pulse agree:** with one store and one clock, the `composite`,
  `asOf` and `componentsAveraged` that Pulse serves equal those of
  `/api/news/composite`.
- **Pulse:**
  - the VIX change is derived from two observations, and a `"."` row is
    skipped.
  - with no provider, `topSector` is unavailable and VIX and the composite
    still serve.
  - the 60-second cache makes one snapshot request for two calls.
- No route serves a FRED or composite history (a test lists the routes, for
  C15).

**U8–U10, the frontend:**
- **The gauge:** each designed state renders from a mocked response. Seven
  rows, always. The *Sources* toggle works by keyboard. The *"N of 7"* text.
  C9's note. The marker is gone.
- **Pulse:** each card's states. The composite text equals the gauge's
  through one helper.
- **The halt (C12):**
  - Execute and Review are disabled when the engine is halted, in Manual
    and in Auto.
  - They are disabled with words while state is loading or failed.
  - The palette omits Execute in all three cases.
  - Dismiss is never gated.
  - The guard test fails on a planted `useUIStore((s) => s.isHalted)` in a
    page.
  - `CommandPalette.test.tsx`'s fixture-halt cases are rewritten against
    mocked engine state.
- `npm run typecheck` and `npm run test` pass.

**Before every engine change:** `uv run python -m pytest -m risk -p
no:time_machine`, `uv run python -m mypy corollary`, and the two `npm`
commands in `web/`.

---

## Doc amendments

Each lands with the unit that makes it true, or in U11.

- **Parent spec, step 8's entry.** *Depends on* loses the stale Q17 text
  (accepted 2026-10-01). Its status points here, made in this spec's own
  commit. Its status becomes "done" in U11. When answered, a Q27 and Q28
  entry points here, like Q23 and Q24. *(Applied 2026-11-17, with the
  answers.)*
- **Parent spec, *Feeds and budgets*:**
  - the composite-prices row: ~15 a day, ~27 once.
  - the top-sector row: one request for eleven funds, not shared.
  - the FRED row: junk at 11:15 ET, not 10:00.
  - the composite slot: close + 60 min, not 16:30.
  Applied in U11 as dated notes beside the text.
- **PRD §8.3, Market Sentiment.**
  - Each component is a stated proxy (C2's table, in words).
  - The composite is computed once a day after the close.
  - It prints *"N of 7"* when components are forming or excluded.
  - Put/call is SPY's chain, accumulated forward and forming for 60
    sessions.
  - The breakdown is visible, not hover-only.
  - The as-of rule as Q28 settled it (option b). The composite is as of the
    latest close, a lagged component prints its own date, and the composite
    says it is provisional while any component lags.
- **PRD §8.5, Market Pulse.**
  - VIX is FRED's last close, with its date. Step 0 found no intraday
    source on this plan.
  - The composite is the same server-derived figure News prints.
  - The top sector is ranked from the eleven sector funds' snapshots on the
    realtime feed.
- **PRD §11, Phase 4 carry-forward.** *"Research reads a different halt"* is
  recorded as resolved by step 8, together with the palette's fixture OR and
  Research's Manual-mode gap (C12). *(Applied with U10, 2026-11-17.)*
- **Phase 2 spec, carried items.** The Research/`isHalted` item is closed by
  decision 16 (C12). *(Applied with U10, 2026-11-17.)*
- **CLAUDE.md, Layout.** `data/macro/` is no longer *"(stub)"*:
  `components.py`, `composite.py`, the store and the three jobs.
- **CLAUDE.md, "Where frontend logic lives".** *(Added 2026-11-17 by U10;
  lands in U11.)* *(Applied early, 2026-11-21, at the owner's request.)* An entry for `web/src/lib/entryGate.ts`: the one
  "may a new position open now" rule, read from the engine's halt and closed
  while it is unknown. It should name `useEntryGate()` and
  `haltSource.test.ts`, so the next surface that opens a position reaches
  for it, not for `useUIStore`.
- **Code docstrings:**
  - `corollary/data/macro/__init__.py`.
  - `FredObservationRecord`: the two series, and C15's wording.
  - `CommandPalette.tsx`'s *"Still the fixture halt"* comment. *(Applied
    with U10, 2026-11-17.)*

---

## Order of work

Each unit gives **Status**, **Depends on**, **Files** and **Done when**, as the
parent and the remainder do, plus its rule 9 position, its rule 8 logging and
its `Decimal` handling. **A unit's status changes in the same commit as the
thing it describes.** Every unit is one pull request into `master`.

~~**Every unit is blocked on Q27.** U2 and everything that serves or renders the
composite (U7, U8, U9) is also blocked on Q28.~~ *Amended 2026-11-17:* both
are answered. U10 goes first, and the rest follow it (Q27). U2's as-of rule
is option (b), with the provisional marker (Q28, C8).

**U0. Pre-flight: keyed probes. Operational, launched by the owner.**
- *Status:* not started. ~~Blocked on Q27.~~ Released 2026-11-17, to follow
  U10 (Q27).
- *Depends on:* nothing.
- *What it does:* `scripts/probe_phase3_step8.py`, run with `uv run --env-file
  .env python scripts/probe_phase3_step8.py <check>`, so no agent reads
  `.env`. It is GET-only, so the rule 1 guard on `scripts/*.py` applies
  unchanged. It records redacted fixtures, **keeping the raw response text**,
  because the parent's fixtures went through a float (*Constraints*). Checks,
  one per *Not verified* item:
  - `chain-close` at about 17:00 ET: the whole SPY chain. Pages, contracts,
    the `dailyBar` date histogram, put and call volume, and every contract
    expiring today with its `dailyBar`.
  - `chain-preopen` at about 08:45 ET the next session: the same, plus
    whether yesterday's expiries are still listed and what `prevDailyBar` is
    dated.
  - `bars-close` and `bars-morning`: SPY, TLT and the ~503 seed names, 504
    sessions. The symbol cap at 200 and at 503. The SPY daily close against
    its last regular-session minute bar. The largest change between the two
    fetches.
  - `fred`: both series at `limit=400`, their depth, and the latest date at
    10:00 and at 11:15 ET.
  - `sectors`: one snapshot request for the eleven funds.
  - `chain-eth`: repeat `chain-close` once in the week after 2026-12-04.
- *Rule 9:* a script, not a job. It never starts the app, and it refuses to
  run while anything answers `GET /api/health` on 127.0.0.1:8000, so it never
  shares the account's 200/min with a live app (the drain script's
  precedent).
- *Rule 8:* each refusal (app running, keys missing, a vendor error) logs the
  rule, the inputs and the time as JSON on stderr, never a key or a URL with
  one.
- *Decimal:* response text is kept raw. Parsed figures in its report use
  `parse_float=Decimal`.
- *Files:*
  - `scripts/probe_phase3_step8.py`.
  - `tests/fixtures/alpaca/p8_*.json`, `tests/fixtures/fred/p8_*.json`.
  - this spec's *Not verified*, each item struck through with its result.
- *Done when:*
  - *Not verified* items 1–3 and 5–11 are struck through with results.
  - C4's pre-open catch-up is confirmed or removed.
  - item 4 has a dated re-probe owner.

**U1. Session ranges and the seven raw computations, pure.**
- *Status:* ~~not started. Blocked on Q27.~~ ~~Released 2026-11-17, to
  follow U10 (Q27).~~ **Built 2026-11-21** on `phase3-step8-u1-components`,
  stacked on `phase3-step8-spec`; not yet merged. *Evidence:* the U1 tests
  pass (101, in `test_components.py` and `test_calendars.py`), `pytest -m
  risk` passes (723), the full suite passes (7,249), and `mypy corollary` is
  clean (120 files, `compiled: no`). Ten deliberate mutations of the module
  (a tie not counting as a high, the first-bar rule dropped, the sliding
  window never evicting, flat closes counted as declines, and others) each
  fail at least one test. The audit's fixes are in: exact-type guards on the
  `universe` and `contracts` elements, so no float volume reaches the
  arithmetic, and an OCC `symbol` on each contract, with repeats refused, so
  a merged page cannot be counted twice. *Left for U5 and U6:* U5 fetches
  bars from some sessions before the earliest strength window, because a
  name's first bar stands in for its listing date. U6 parses `root` and
  `is_put` with `instruments.parse_occ_symbol`, which `components.py` may
  not import. *For the owner:* C1's "a ratio never goes stale" holds for
  prices but not for volume. A split rescales one name's earlier volume, so
  a `breadth` recomputed inside the recheck window can differ from the
  first computation. Frozen rows are unaffected.
- *Depends on:* nothing. Runs in parallel with U0, U3 and U10.
- *What it does:*
  - `calendars.nyse_sessions_between(start, end)`.
  - `components.py`: one function per component of C2, over plain input
    dataclasses, returning a value or a stated `NoValue(rule, detail)`.
  - The component keys as an enum.
  - The C2 orientation table as data.
  - Sliding-window maxima and minima for strength.
- *Rule 9:* pure. No I/O, no imports beyond `calendars` and the standard
  library.
- *Rule 8:* returns `NoValue` with its rule, never `None` and never zero. The
  caller (U4–U6) logs it.
- *Decimal:* inputs `Decimal`, volumes `int`, arithmetic under an explicit
  40-digit `localcontext`, output quantized to `1E-10` `ROUND_HALF_EVEN`. No
  float accepted or produced.
- *Files:*
  - `corollary/calendars.py`.
  - `corollary/data/macro/components.py`.
  - `tests/data/macro/test_components.py`.
  - `tests/test_calendars.py`.
- *Done when:* U1's tests in *Testing* pass, and `mypy` and `pytest -m risk`
  are clean.

**U2. Scoring and the composite, pure.**
- *Status:* ~~not started. Blocked on Q27 and **Q28**.~~ ~~Released
  2026-11-17, to follow U10 (Q27).~~ ~~Built 2026-11-21, except one case
  open with the owner.~~ **Built 2026-11-21 and amended 2026-11-23 for Q29**,
  on `phase3-step8-u2-composite`, stacked on U1's branch; not yet merged.
  The as-of rule is option (b) (Q28), with *D* taken from the Alpaca
  components that have rows (Q29).
  - *Evidence, 2026-11-21:* Φ agrees with an independent series to better
    than 1E-45. Ten deliberate mutations each fail at least one test:
    - rounding half-to-even
    - the population standard deviation
    - a lag of 2 accepted
    - the 60-minute delay dropped
    - *x_D* left out of the window
    - forming at 59
    - no orientation
    - s = 0 treated as z = 0
    - a changed set not noticed
    - a lag of 0 marked provisional
  - *Evidence, 2026-11-23 (Q29):*
    - `test_composite.py` and `test_components.py` pass (152).
    - `pytest -m risk` passes (723).
    - The full suite passes (7,336).
    - `mypy corollary` is clean (121 files).
    - The Q29 tests were run against the old rule before the code changed,
      and six failed.
    - `AsOfUndetermined` and `NO_COMMON_SESSION` are gone. The two
      unavailable reasons that are still reachable are `no_rows` and
      `no_usable_components`.
  - *A consequence of Q29 to know about:* a failure of **one** Alpaca job no
    longer makes the panel stale (C13 is amended to say so). Its components
    lag for a session, which marks the composite provisional and names them.
    After that they are excluded as `stale`, and the count drops. The whole
    panel reads stale only when both Alpaca jobs fail.
  - *Carried into U3 and U7:* `read_composite(rows, *, now, loaded_from)` takes
  the first session the caller loaded. It raises `IncompleteLoad(needed_from)`
  rather than score a short window, and `load_start(as_of)` sizes the load.
  U7 loads from `load_start(expected_as_of(now))` and reloads once on
  `IncompleteLoad`. U3's loader must return every row on or after
  `loaded_from`. Verdicts that rest on a row's absence (`no_value`, no rows)
  cannot see rows from before `loaded_from`. Under Q29 the reload no longer
  repairs this. A component dead for more than a year reads `no_value` in
  U7's exact load, but `stale` in a load of everything. The composite and
  the count are the same either way. That needs data dead for most of a
  year to matter, and it is documented in `composite.py`.
- *Depends on:* U1 (the enum and the orientation table).
- *What it does:* C6–C10.
  - The window over rows.
  - z.
  - An exact `Decimal` Φ.
  - Integer scores.
  - The composite.
  - The as-of rule.
  - `previous` and the change.
  - `read_composite()`, over rows the caller loads.
- *Rule 9:* pure.
- *Rule 8:* each excluded component carries its reason (`forming`, `stale`,
  `no_variance`, `no_value`) in the result. Nothing is dropped silently.
- *Decimal:* everything, Φ included (C7). The only rounding is the two
  `ROUND_HALF_UP` integer quantizations.
- *Files:*
  - `corollary/data/macro/composite.py`.
  - `tests/data/macro/test_composite.py`.
- *Done when:* U2's tests in *Testing* pass, including the as-of boundaries
  for the rule Q28 chose (option b, C8), as amended by Q29, and the
  `provisional` flag.

**U3. Schema and store.**
- *Status:* ~~not started.~~ ~~Blocked on Q27.~~ Released 2026-11-17, to follow
  U10 (Q27). **Built 2026-11-24.**
  - *Evidence, 2026-11-24:*
    - `uv run python -m pytest`: 7433 passed.
    - `-m risk`: 723 passed.
    - `uv run python -m mypy corollary`: no issues in 123 source files (`compiled: no`).
    - `uv run alembic heads`: `0021 (head)`, one head.
    - On a scratch DB: `upgrade head`, then `downgrade -1` (0020, table
      gone), then `upgrade head` (0021, table back).
    - Both component CHECK lists, the model's and the migrated table's, are
      parsed and tied to `ComponentKey`.
  - *Departures, recorded above:*
    - `load_window` takes `loaded_from`, U2's contract, not a session count.
    - Each floor is anchored on the later of the caller's `newest_stored`
      and the table's own newest, so a caller can narrow the recheck
      window but never widen it.
    - A batch with any value outside the window is refused whole and logged
      with `composite_recheck_window`.
  - *Carried into U4–U6:*
    - `upsert_values` refuses a date that is not an NYSE session.
    - FRED can publish `junk_bond` on a day bonds trade and the NYSE does
      not (Good Friday). U4 must drop those observations before calling the
      store, or the whole batch raises.
    - The store does not refuse a future-dated session. Writing only
      through the latest completed session is the job's duty (C1). A
      future-dated row would become the newest, move the floor forward, and
      freeze real rows; later rechecks would then be refused loudly, not
      written silently.
- *Depends on:* nothing (the CHECK list is literal, and a test ties it to
  U1's enum once both exist). Runs in parallel with U1.
- *What it does:*
  - Migration `0021` (assigned at merge) for `composite_component_value`.
  - The `CompositeComponentValue` model.
  - `store.py`: `upsert_values(session, values, *, newest_stored)`, which
    writes only inside the recheck window.
  - ~~`load_window(session, components, end_session, sessions=252)`.~~
    `load_window(session, components, *, loaded_from)`, every stored row
    dated on or after `loaded_from`, with no end cap *(amended 2026-11-24)*.
    U2's contract requires it: `read_composite` needs every row on or after
    `loaded_from`, its `previous` window needs 253 sessions (`load_start`),
    and `IncompleteLoad.needed_from` is a date, so a count of 252 would
    silently short the load.
  - `newest_dates(session)`.
- *Rule 9:* ~~imports only `db`.~~ imports only `corollary.db`,
  `corollary.calendars`, and the pure siblings
  `corollary.data.macro.components` and `corollary.data.macro.composite`
  *(amended 2026-11-24)*. Used by context jobs and read routes, never by
  the runtime.
- *Rule 8:* an upsert outside the recheck window is refused with a logged
  rule, `composite_recheck_window`, and its inputs. It never writes
  silently.
- *Decimal:* `Money` column. No SQL comparison of `raw_value`; ordering and
  windows go by `date`.
- *Files:*
  - `corollary/db/models.py`.
  - `corollary/db/migrations/versions/0021_composite_component_value.py`.
  - `corollary/data/macro/store.py`.
  - tests.
- *Done when:* U3's tests pass, and `uv run alembic heads` shows one head.

**U4. The FRED macro job.**
- *Status:* not started. ~~Blocked on Q27.~~ Released 2026-11-17, to follow
  U10 (Q27).
- *Depends on:* U0 (the `fred` check), U1, U3.
- *What it does:* C5. `fred_macro.py` fetches through `ObservationSource`,
  stores through `risk_free.store_observations`, and recomputes `volatility`
  and `junk_bond`. A `fred_macro` job runs at `AtTime(11:15,
  trading_days)` with the catch-up of C13.
- *Rule 9:* a context job. No runtime, state, socket or API import. It
  joins the shipped set, so the isolation tests cover it, and its SQLite
  work runs in `to_thread`.
- *Rule 8:* `fred=None` returns `JobSkipped` with the reason. A session with
  no computable value logs `NoValue`'s rule, the series, the session and the
  time. A malformed FRED value is refused (`FredError`) and never read as a
  gap.
- *Decimal:* FRED strings straight to `Decimal`, with `"."` caught before
  conversion, as the provider already does.
- *Files:*
  - `corollary/data/macro/fred_macro.py`.
  - `corollary/engine/scheduler.py` (the job).
  - tests on `tests/fixtures/fred/`.
- *Done when:*
  - on a recorded fixture, a first run loads both histories and writes 252
    `volatility` and junk raws, and a second run writes only the recheck
    window.
  - live, after deploy, the 11:15 ET run on a trading day stores the
    previous session's VIX and junk.

**U5. The composite prices job and the history load.**
- *Status:* not started. ~~Blocked on Q27.~~ Released 2026-11-17, to follow
  U10 (Q27).
- *Depends on:* U0 (`bars-*`), U1, U3. Runs in parallel with U4 and U6.
- *What it does:* C3, C13.
  - `prices.py`: fetches SPY `split`, SPY and TLT `all`, and the seed
    universe `split` in ≤200-symbol batches, maps bars to U1's inputs, and
    computes and stores four raws.
  - The `AfterClose` schedule type.
  - `ContextServices.macro_prices`.
  - The `composite_prices` job with its catch-up (the history load). It
    also calls U4's fetch, if U4 has merged; until then that call is absent.
- *Rule 9:* a context job. The bar-to-input mapping and every computation
  run in `asyncio.to_thread`. `macro_prices` is market data, and the
  isolation walker refuses a broker there.
- *Rule 8:* a non-`sip` historical feed refuses `strength` and `breadth`
  (rule, configured feed, time). A missing seed skips those two with its
  reason. A missing SPY or TLT bar for a session logs `NoValue`. Per-name
  gaps are logged once a run, as counts with a bounded sample of symbols,
  not a line a name.
- *Decimal:* `Bar` is already `Decimal`. Volumes `int`. No float.
- *Files:*
  - `corollary/data/macro/prices.py`.
  - `corollary/engine/scheduler.py`.
  - `corollary/api/app.py` (pass the provider into `macro_prices`).
  - tests on U0's fixtures.
- *Done when:*
  - on fixtures, the history load and the recheck behave as *Testing*
    says.
  - live, the first run writes 252 sessions of each of the four, and
    reports how many seed names were ineligible.
  - the next trading day's 17:00 ET run writes that session and rechecks
    four before it.

**U6. The put/call job.**
- *Status:* not started. ~~Blocked on Q27.~~ Released 2026-11-17, to follow
  U10 (Q27).
- *Depends on:* U0 (`chain-*`), U1, U3, and U5's `AfterClose` and
  `macro_prices`. If U5 has not merged, U6 adds those two itself and U5
  rebases.
- *What it does:* C4. `put_call.py` reads the whole SPY chain after the
  close and writes `put_call` for the session. A pre-open catch-up is
  included only in the form U0 confirmed.
- *Rule 9:* a context job. Pagination runs in the provider, and the
  summation runs in `to_thread`.
- *Rule 8:* zero call volume, an empty chain, or a snapshot taken after the
  next open each log the rule, the session, the counts and the time, and
  write nothing. A gap is logged as a gap.
- *Decimal:* volumes are `int`, and the ratio is `Decimal` quantized
  `1E-10`.
- *Files:*
  - `corollary/data/macro/put_call.py`.
  - `corollary/engine/scheduler.py`.
  - tests on U0's `chain-*` fixtures.
- *Done when:*
  - on fixtures, a contract with a stale `dailyBar` is excluded and a
    missed session is a gap.
  - live, three consecutive trading days write three rows.

**U7. The two routes.**
- *Status:* not started. ~~Blocked on Q27 and Q28 (through U2).~~ Released
  2026-11-17, to follow U10 (Q27). Q28 is answered.
- *Depends on:* U2, U3. U4–U6 are needed for the live check, not for the
  code.
- *What it does:*
  - `GET /api/news/composite`.
  - `GET /api/markets/pulse`, with the top-sector cache.
  - The schemas.
  - The FRED sources row.
- *Rule 9:* read-only `GET`s, depending on no broker and no runtime.
  `topSector` uses the market-data provider through the existing
  dependency.
- *Rule 8:* a route never refuses a request for missing data. It answers
  200 with the stated `unavailableReason` (decision 1's *never silence*). A
  provider error on the sector snapshot is logged with its rule and served
  as unavailable, not as a 500.
- *Decimal:* every exact value is `JsonMoney` at the display boundary.
  Nothing parses it back.
- *Files:*
  - `corollary/api/routes/{news,markets,settings}.py`.
  - `corollary/api/schemas.py`.
  - `web/src/lib/{api,types}.ts` (the response types only).
  - tests.
- *Done when:* U7's tests pass, including News-and-Pulse agreement. Live,
  both routes serve the stored composite.

**U8. News: the real gauge.**
- *Status:* not started. ~~Blocked on Q27 and Q28 (through U7).~~ Released
  2026-11-17, to follow U10 (Q27). Q28 is answered.
- *Depends on:* U7. Runs in parallel with U9.
- *What it does:*
  - `useComposite()`.
  - `SentimentGauge` rewritten per *Frontend*.
  - The composite's marker off.
  - `compositeScore`, the fixtures and `SentimentComponent` removed. Pulse
    still reads `MARKET_PULSE` until U9; if U9 lands first, U8 removes
    nothing U9 still needs.
- *Rule 9:* none, display only.
- *Rule 8:* none on the client. Unavailable and stale states render the
  server's reason verbatim.
- *Decimal:* the client formats and never computes. Its one helper,
  `formatComposite`, in `lib/format.ts`, is shared with U9.
- *Files:*
  - `web/src/components/SentimentGauge.tsx`.
  - `web/src/pages/News.tsx`.
  - `web/src/lib/{queries,news,mockData,types,format}.ts`.
  - tests.
- *Done when:*
  - News shows the composite with all seven components.
  - Every proxy is stated visibly.
  - Put/call is shown forming with its count.
  - While any component lags, the gauge says it is provisional and names
    each lagged component and its date (C8, Q28).
  - The panel's marker is off.
  - `typecheck` and `test` pass.

**U9. Research: the real Market Pulse.**
- *Status:* not started. ~~Blocked on Q27 and Q28 (through U7).~~ Released
  2026-11-17, to follow U10 (Q27). Q28 is answered.
- *Depends on:* U7. Runs in parallel with U8.
- *What it does:*
  - `useMarketPulse()`, polled at 60 s while visible.
  - The three cards.
  - The narrowed title marker.
  - `MARKET_PULSE` removed.
- *Rule 9:* none.
- *Rule 8:* none on the client. It renders the server's reasons.
- *Decimal:* display only, through `formatComposite` (U8, or added here if
  U9 lands first).
- *Files:*
  - `web/src/pages/Research.tsx` (the Pulse block only; the halt is U10's).
  - `web/src/lib/{queries,mockData}.ts`.
  - tests.
- *Done when:*
  - Pulse's composite agrees with News to the digit on the same load (a
    test, and a side-by-side check on the live app).
  - VIX shows its close date.
  - The composite carries the same provisional note as News (C8, Q28).
  - The top sector shows its session and answered count.

**U10. The real halt on Research, the chain ticket and the palette.**
- *Status:* ~~not started. Blocked on Q27 only. It is the one unit Q27
  option (b) releases on its own.~~ ~~**Released 2026-11-17 (Q27), first,**
  ahead of every other unit.~~ **Built 2026-11-17** on
  `phase3-step8-u10-real-halt`, stacked on `phase3-step8-spec`; not yet
  merged. *Evidence:* `npm run typecheck` is clean and `npm run test` passes
  (1,479 tests in 46 files, after the audit fixes below). The guard test
  fails by name on a planted `useUIStore((s) => s.isHalted)` in a new page
  file, which was removed afterwards. `npm run build` succeeds, and the
  three colour utilities are in the emitted CSS. The rules audit found
  nothing high-severity; its findings and what was done follow the as-built
  notes.
  *As built, where it adds to the text below:*
  - **One gate, not three reads.** `web/src/lib/entryGate.ts` holds the rule
    as a pure function over the engine-state query's fields, and
    `useEntryGate()` in `queries.ts` wraps it. Research and the chain ticket
    call the hook. The palette calls `entryGate(engineQuery)` over the query
    it already holds for Halt/Resume.
  - **A failed read closes the gate even with a cached "running"**, because
    a refetch that fails leaves the last answer in `data`. A cached halt
    stays the stated reason.
  - **The confirm dialogs honour the gate too.** On Research and the chain
    ticket, a halt that lands while the confirm is open disables its submit,
    states the reason, and `onConfirm` re-checks before acting. The
    dead-man's switch fires on its own (rule 9), so the halt can arrive
    between Review and Submit.
  - **The reason is on the page**, as a `role="status"` line, not only in
    the button's `title`. The colours are `ENTRY_BLOCK_CLASS`: `caution`
    for a halt, `error` for a failed read, `on-surface-variant` for a read
    in flight.
  - `RecommendationActions` takes `entryBlocked: string | null` in place of
    `halted: boolean`.
  - **The palette's Halt/Resume pair is unchanged:** neither is offered while
    pending, and a failed read falls through to Halt.

  *The rules audit (2026-11-17), and what was done about it:*
  - **Engine state was never polled (medium, pre-existing; fixed here).**
    `useEngineState()` had no `refetchInterval`, though the Phase 2 frame
    contract (human decision 2, 2026-09-13), `liveSocket.ts`, `types.ts` and
    `queries.ts` all said it was polled at 15s. So a halt the dead-man's
    switch took reached an open page only on a refocus. That held for the
    Dashboard's pill as well as the new gate. It now polls at
    `ENGINE_STATE_POLL_MS` = 15s with `refetchIntervalInBackground: true`.
    That setting is a parent-session choice, flagged for the owner: a
    visible, unfocused window is how the terminal is watched, and this read
    is local, with no vendor budget behind it. A test pins the cadence the
    way the bell's is pinned.
    *Owner decision, 2026-11-20:* *"for the halt bell whatever makes the
    most sense"*. The choice was to **keep it, and give the notification
    bell the same setting.** The bell is how a rule 9 halt reaches the
    screen, and it reads the local engine only. Both polls now run while
    the window is unfocused. A hidden-tab poll of market data stays
    forbidden (decision 18), because that one spends vendor budget.
  - **The gate opens only on `halted: false`** (low; fixed). An empty body
    or a renamed field reads `unavailable`, with a stated reason.
  - **The guard catches `{ isHalted = false }` too** (low; fixed). Its
    remaining blind spots are written into its docstring: a selector
    exported from the store, and a read on a line beginning with `*`.
  - **Nits fixed:** the chain ticket's confirm no longer nests a `<p>` in a
    `<p>`, and a failure with no message reads *"The request failed."*
  - **Carried to Phase 6 (not U10's surface):** Activity's *Add to
    position* (`OrderTicket.tsx`, `mode === 'add'`) is a new entry. It is
    disabled today only by `READ_ONLY_REASON`. When that is lifted it must
    take `useEntryGate()`, and nothing forces it to.
- *Depends on:* nothing. It runs in parallel with everything.
- *What it does:* C12 and decision 16.
- *Rule 9:* reads the halt, writes nothing, resumes nothing. Fail-closed
  while the state is unknown is the conservative direction.
- *Rule 8:* the disabled states name their reason in words. Nothing is
  silently disabled.
- *Decimal:* none.
- *Files:*
  - `web/src/pages/Research.tsx` (the halt lines only).
  - `web/src/components/ChainOrderTicket.tsx`.
  - `web/src/components/CommandPalette.tsx`.
  - their tests.
  - a guard test (`web/src/lib/haltSource.test.ts`).
- *Done when:*
  - Research and the chain ticket read the real halt, in Manual and in
    Auto.
  - The palette has no fixture OR.
  - The guard test passes and fails on a planted read.
  - `typecheck` and `test` pass.

**U11. Doc amendments and statuses.**
- *Status:* not started. ~~Blocked on Q27.~~ Released 2026-11-17, to follow
  U10 (Q27).
- *Depends on:* U0–U10.
- *Files:*
  - `PRD.md`.
  - `CLAUDE.md`.
  - the parent spec.
  - `2026-09-10-phase-2-real-data-design.md` (its carried item).
  - this spec.
  - the three docstrings under *Doc amendments*.
- *Done when:*
  - every item under *Doc amendments* is applied or struck with a reason.
  - the parent's step 8 reads done, with this spec as its record.
  - the parent's step 10 carries step 8's items as already applied.

**Parallelism.** ~~Once Q27 is answered, U0, U1, U3 and U10 can start at once.
U2 follows U1 once Q28 is answered.~~ *Amended 2026-11-17:* U10 goes first
(Q27). After it, U0, U1 and U3 can start at once. U2 follows U1. U4, U5 and U6 follow U0, U1 and U3, in
parallel with each other. U7 follows U2 and U3. U8 and U9 follow U7, in
parallel. U11 is last. **The longest path is U0's two sittings** (a close
and the next pre-open), then U5, U7, U8 and U11. Put/call then forms for 60
sessions after U6 deploys. That is a property of the data, not a unit, and
the parent's *Done when* already reads it as *"shown forming with its
count"*.

---

## Out of scope

- **Step 9** (social attention, analyst consensus). It is still paused
  under Q20.
- **Any consumer of the composite as an input.** The scanner is Phase 4.
- **No order path of any kind**: no `submit_order`, no
  `RiskManager.approve()`, no `BrokerExecution`, and no change to the three
  *"Submits to the risk manager"* surfaces (Phase 6).
- **No LLM or Anthropic call, and no MCP** anywhere in the data path (rule
  3).
- **A put/call backfill** from historical option bars (parent; Phase 5).
- **An intraday composite or intraday VIX** (parent decision 5; C5, C11).
  Revisit at Phase 4's plan upgrade.
- **A point-in-time S&P 500 universe** (C3) and **component smoothing**
  (C2). Each is the owner's to ask for.
- **Adding sector funds to the Markets universe** (C11).
- **Exporting** any FRED series or the composite's history (C15).
- **A notification event, Settings control or audit category** for the
  composite (C13).
- **Pruning the fixture store** (`isHalted`, `flatten()`). That is Phase 6's
  question (C12).
- **Algo Trader Plus**, or any cadence that depends on the plan.
