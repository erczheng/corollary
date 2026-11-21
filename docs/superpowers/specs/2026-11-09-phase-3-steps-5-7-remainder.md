# Phase 3, steps 5–7: what is left

**Date:** 2026-11-09
**Status:** Draft. The owner decisions it rests on were taken on 2026-11-09 and
are recorded under *Owner decisions* (Q23). Nothing here is blocked on a
question.
**Amended 2026-11-11 with Q24** (four owner decisions taken while U5 and U6
ran), recorded under *Owner decisions — 2026-11-11 (Q24)*. Q24c amends R4
step 4.
**Amended 2026-11-16 (U7):** unit statuses, final migration numbers, what was
built where it differs from the text, and the *Not verified* items now
verified. Each is a dated note beside the text it amends; the original text
stays.
**Parent spec:** `2026-09-23-phase-3-context-design.md` (steps 5, 6 and 7, and
Q22). This spec amends that spec's decision 13 and Q6, and the parent gains a
Q23 entry pointing here, in the same commit. *(Verified 2026-11-16: the Q23
entry landed in `a308f7c`, this spec's own commit.)*
**Branch:** every unit branches from, and its pull request targets, **`master`**.
**Phase:** 3 — context. **No order reaches a broker, and nothing built here is
an input to anything that could place one.**

---

## Problem

Steps 5, 6 and 7 have all merged: PR #4 (step 5), PR #7 (step 6) and PR #6
(step 7), with `master` at `c2fbf5c`. A check on 2026-11-09 of `master`, the
unmerged branches and the live `corollary.db` (opened read-only) found four
things left.

**1. An owner decision of 2026-10-22 never merged.** Branch `phase3-fixes`
carries two commits that are not on `master`:

- `2ff1343`: grade a label from the story's `published_at`, not
  `max(published_at, labeled_at)`, on dividend- and split-adjusted bars.
- `4a6f2d7`: link Finnhub's "Yahoo" copies of Motley Fool stories to their
  Massive twins. Finnhub stamps them exactly 20 minutes late, so 575 stories
  a fortnight were shown twice and graded twice.

`git merge-tree` reports the branch merges into `c2fbf5c` cleanly.

**2. The first real audit ran without it.** Run 2 ran at 2026-11-09 23:02:16Z,
trigger `scheduled` (the catch-up for the missed Saturday, Nov 7). It ran on
`master`, so it used the superseded reference rule and split-only prices. It
moved:

- `rules` from `unaudited` to **`active`**: 1h 26/39 (66.6%), 1d 24/39 (61.5%).
- `massive` from `unaudited` to **`demoted`**: 1h 26/55 (47.2%), 1d 23/55
  (41.8%).

`sentiment_demoted` was delivered to the bell and to Discord at 23:02:21Z.
Under the old rule, every graded rules label had been fetched more than an
hour after publication (median 3.0 hours), so its "1h" window measured hours
1–4 after the story. For Massive the median lag was 1.2 hours, with 31 of 55
over an hour.

**3. A source can be judged on one session.** Every label run 2 graded was
written between 2026-11-02 20:49Z and 2026-11-03 08:19Z, because the app did
not run from Nov 4 to Nov 8. Decision 13's ≥30 floor assumed labels would
arrive over about 20 trading days. In practice both sources cleared it in one
news cycle, so one day's market could set the flag Phase 4's scanner will read.

**4. No economic release has a prior or an actual.** All 20 stored `economic`
rows are null in both. The parent spec never said which FRED series and
transform is a release's headline figure. There is also a structural trap:
the release-dates refresh upserts each row by comparing **and overwriting**
`prior` and `actual` (`corollary/data/calendar.py`, `_form_of_input` and
`_apply`). A value written into those columns would be nulled again by the
next morning's refresh.

---

## Constraints verified against the vendors

Each item says how it was verified. **Read** means read from the vendor's
public page or documentation. **Probed** means observed against the live
database or service. No keyed FRED call was made: this planning session cannot
read `.env`.

### The fifteen FRED series

**Read, 2026-11-09**, from each series page on `fred.stlouisfed.org`
(keyless). For every series, the page names the same release as
`econ_release_times.csv`.

| `release_id` | Release | Series | Units on FRED | Freq | SA |
|---|---|---|---|---|---|
| 10 | Consumer Price Index | `CPIAUCSL` | Index 1982-84=100, 3 dp | M | SA |
| 50 | Employment Situation | `PAYEMS` | Thousands of persons | M | SA |
| 46 | Producer Price Index | `PPIFIS` | Index Nov 2009=100, 3 dp | M | SA |
| 192 | JOLTS | `JTSJOL` | Level in thousands | M | SA |
| 11 | Employment Cost Index | `ECIALLCIV` | Index Dec 2005=100, 3 dp | Q | SA |
| 188 | Import and Export Price Indexes | `IR` | Index 2000=100, **1 dp** | M | **NSA** |
| 53 | Gross Domestic Product | `A191RL1Q225SBEA` | % change from preceding period, SAAR, 1 dp | Q | SA |
| 54 | Personal Income and Outlays | `PCEPILFE` | Index 2017=100, 3 dp | M | SA |
| 51 | International Trade | `BOPGSTB` | **Millions** of dollars | M | SA |
| 9 | Advance Retail Sales | `RSAFS` | Millions of dollars | M | SA |
| 27 | New Residential Construction | `HOUST` | Thousands of units, SAAR | M | SA |
| 97 | New Residential Sales | `HSN1F` | Thousands, SAAR | M | SA |
| 229 | Construction Spending | `TTLCONS` | Millions of dollars, SAAR | M | SA |
| 13 | G.17 Industrial Production | `INDPRO` | Index 2017=100, 4 dp | M | SA |
| 291 | Existing Home Sales | `EXHOSLUSM495S` | **Number of units**, SAAR; *"Copyrighted: Citation Required"* (NAR) | M | SA |

### FRED's observations endpoint

**Read**, from `fred.stlouisfed.org/docs/api/fred/series_observations.html`:

- **`realtime_start` and `realtime_end`** bound the real-time period. Both
  default to today. *"Vintage dates are used to download data as it existed on
  these specified dates in history."*
- **`units`** takes `lin`, `chg`, `ch1`, `pch`, `pc1`, `pca`, `cch`, `cca` or
  `log`.
- **`output_type`**: `1` observations by real-time period, `2` and `3` by
  vintage date, `4` initial release only.
- *"Your query will be limited to a specific number of vintage dates depending
  on the series_id, file_type and output_type."*
- **Missing values:** the parent spec's FRED subsection holds. A missing
  observation is the string `"."`, and it is handled before `Decimal()`, never
  caught after it.

### The live database

**Probed, 2026-11-09, read-only** (`file:corollary.db?mode=ro`):

- **Alembic head `0016`.**
- **Runs.** `audit_run` 1 (manual, 2026-11-03, graded 0) and 2 (as in
  *Problem*, graded 226, 85 bar requests, no faults, no discovery-cap skips).
- **Labels.** `sentiment_label` holds 195 rows, all `labeled_at` between
  2026-11-02 20:49Z and 2026-11-03 08:19Z.
- **Articles.** `news_article` holds 2,917 rows, ingested on Nov 2, Nov 3 and
  Nov 9 only.
- **Economic rows.** Each one's `vendor_id` is `"<release_id>:<YYYY-MM-DD>"`,
  for example `"229:2026-11-02"`.

### The code, on `master` at `c2fbf5c`

**Read:**

- `label_outcome`'s primary key is `(label_id, window)`.
- `audit_run.status` is CHECK-constrained to `running | succeeded | failed`.
- An `audit_log` row in the `sentiment` category carries only `at`, `field`
  (the source), `previous_value` and `new_value`: no reason and no run id.
  `sentiment_source_status` holds only the latest reason.
- `sentiment_source_status` requires `changed_at`, `reason` and
  `audit_run_id` to be NULL exactly when the status is `unaudited`.
- **`phase3-fixes` links Motley Fool twins at ingest only.** It makes no pass
  over articles already stored.

### Not verified

Each item is carried as a check at implementation time.

- **Whether an observation in an `output_type=1` response carries its own
  `realtime_start`.** Decision R7's "new on release day" test depends on it.
  U6's keyed probe records the response shape first. If the field is absent,
  R7 falls back to comparing the vintage on the release day against the
  vintage on the day before.
  **Verified 2026-11-10 (U6a's keyed probe), and the answer reshaped R7's
  query.** Every row carries `realtime_start` and `realtime_end`, but FRED
  **clips** each row's real-time period to the window asked for. A
  single-day query (`realtime_start = realtime_end = D`) therefore stamps
  every row `D` and cannot say which value is new. The working query is
  **one request with `realtime_start = D-1` and `realtime_end = D`**:
  - **The release-day vintage** is the rows whose `realtime_end == D`.
  - **New or revised on D** is a row starting on `D` with either no row for
    that date ending on `D-1`, or one whose value differs **by `Decimal`
    comparison**. FRED's number formatting is not canonical (`'159128'`
    against `'159128.0'` is one number, not a revision).
  - **The prior is read from that same vintage, not the latest one.**
    Without the `realtime_end == D` filter, PAYEMS' prior for the
    2026-11-06 release came out stale (159044), giving **+249K instead of
    +165K**. `corollary/data/econ_values.py` carries the rule, and a test pins
    the filter.
  See R7's amended *fetch* and *which observation* paragraphs.
- **How soon after a release FRED posts it.** The parent spec assumed about 10
  minutes. R8's retry schedule exists so the figure does not matter.
- **Whether a figure computed from FRED's rounded levels equals the agency's
  published headline.** `IR` is published to one decimal place, so its monthly
  change in particular may differ from BLS's printed figure by 0.1. Each
  value is labelled as *computed from FRED's levels*. This is disclosed, not
  corrected.
- **The agencies' rounding mode.** Display uses `ROUND_HALF_UP`, as is
  conventional, but it is not confirmed for any agency.
- **Whether `EXHOSLUSM495S` answers a vintage query.** It is NAR-copyrighted,
  and its history on FRED may be limited. If the vintage query refuses, that
  release shows *"not available from FRED"* with the vendor's reason, and the
  other fourteen are unaffected.
  **Verified 2026-11-10 (U6a's probe): it refuses.** Every vintage query
  answers HTTP 400, *"The series does not exist in ALFRED but may exist in
  FRED"*. The release is served `unavailable` with that reason, as planned.
- **How many stored Motley Fool duplicates exist beyond Nov 2–3.** R2's
  re-link reports the count.
  **Verified 2026-11-10:** the live re-link added **92** twin links (198
  linked in all); a second run added none. See U2.

---

## Owner decisions — 2026-11-09 (Q23)

Put to the owner on 2026-11-09 and answered the same day. The owner's words
are quoted. These are **owner decisions, not assumptions**.

- **Q23a — Void run 2 and re-grade it.** Answer: *"b"*, to the option *"Void
  today's run as graded under a superseded rule, record that in the audit log,
  and re-grade."* Carried by decisions R4 and R5.
- **Q23b — Eligibility needs at least five sessions.** Answer: *"yes, require 5
  sessions"*. Carried by decision R3.
- **Q23c — The FRED mapping is the planner's proposed default.** Answer: *"use
  your defaults"*. That means the fifteen series under *Constraints*, CPI as
  the monthly change, and the **first print kept** rather than revised.
  Carried by decisions R6 and R7.
- **Q23d — Hand verification of the central-bank seed is deferred.** Answer:
  *"ignore for now"*. The parent spec's step 7 notes stand: ECB 2026 from
  2026-10-10 only; the ECB's "14:15 CET" read as Frankfurt local time; BoE and
  BoJ rows date-only. Out of scope here.
- **Q23e — The engine was resumed.** Answer: *"resume right now"*. Done at
  2026-11-09 23:38:05Z through `POST /api/engine/resume` on a briefly started
  `headless_app`. It ended the halt of 2026-09-22 16:48:30Z (the
  `trade_updates` subscription timeout). `operator_resume` reached the bell
  and Discord (HTTP 204). Recorded here; not a work item.

**Planner resolutions.** Taken on evidence by this spec, not by the owner, and
the owner can override either:

- **P1 — Step 5's mean-versus-median question is closed.** Step 5's
  re-measurement gave the rules tier 2.4 directional labels a session on the
  watch universe by the mean, and 1.5 by the median. Q22 left open which one
  decides decision 13's second risk. That risk was that a source producing
  under ~1.5 a session on the watch universe could never become eligible. Two
  things retire it:
  - Transitions act on the **combined** figure (Q22, owner-confirmed), and run
    2 gave rules 39 graded labels from a single session.
  - Eligibility is now bounded by sessions (Q23b), not by a per-session rate
    on the watch universe.
- **P2 — A story fetched long after publication is graded from publication,
  with no cap on fetch lag.** This covers the backlog that arrives after
  downtime, such as Nov 4–8. The owner's 2026-10-22 wording is *"even if
  Corollary fetched it late"*. Q7's purpose still holds: no label is produced
  with knowledge of its outcome, because a label is a function of the headline
  text alone. The cost: a five-day backlog can span five sessions, and so can
  satisfy Q23b on its own. A fetch-lag cap (for example, skip any story
  fetched more than N hours after publication) is the owner's to ask for.

---

## Owner decisions — 2026-11-11 (Q24)

Put to the owner on 2026-11-11, after U5's re-grade (run 3) and U2's live
re-link raised them, and answered the same day (Q24c after a second
explanation). The owner's words are quoted. These are **owner decisions, not
assumptions**. Q24a, Q24b and Q24c are built on `phase3-rem-grading`
(`2f01ce9`, `29711a7`); Q24d merged with U6 in PR #15 (`0ec68c1`).

- **Q24a — Backfill missing tradeability verdicts.** Answer: *"1 backfill"*.
  Run 3 wrote 612 labels `off_watch_untradeable`, and up to 181 of them had
  no verdict on or before their session at all, because verdicts are written
  only by the daily refresh, for its own date, and none exist for Nov 4–9.
  Built as **decision 27** in `corollary/data/news/audit.py`:
  - **What is backfilled.** At most `VERDICT_BACKFILL_CAP = 100` `(ticker,
    session)` pairs a run, oldest session first, each computed with
    `refresh_tradeability(session_date=S)`, which reads only sessions
    strictly before S. Only where no verdict on or before S exists: a stale
    but existing verdict is decision 25's to use and is never replaced.
  - **A pair no verdict reached** (a vendor error, no asset directory, a
    deferral) is held pending up to the 28-day trailing window, then written
    **`off_watch_unverdicted`**, a new membership skip reason (migration
    0020).
  - **`off_watch_untradeable` is written only on a failing verdict.**
  - **An unreadable cached row is no verdict on the audit path**
    (`signals.verdict_histories`). Movers keeps its fail-closed reading of
    the same row (decision 21).
  - **"No completed session before S" counts as a failing verdict**, as it
    does in the daily refresh.
  - **Named residual look-ahead,** accepted and documented rather than
    patched: the ADV bars are split-adjusted, so a split between S and the
    run rescales S's figures; `has_options`, the standard-root check and the
    ticker's symbol are the vendor's **current** answers. No point-in-time
    source exists for any of them, and the daily refresh reads them the same
    way for its own date.
  - **Backfilled verdicts enter the shared decision-21 cache**, so Movers can
    read them. *Carried to Phase 4:* the scanner must require
    `session_date == today` and never read a backfilled old-session verdict
    as current.
- **Q24b — Grade a twin group from its earliest stamp.** Answer: *"use the
  earliest timestamp"*. U2's live re-link found the late Finnhub "Yahoo" copy
  canonical in 66 of the 92 pairs, so a per-copy stamp measured the 1h window
  from minute 20 to minute 80. Built as **decision 26** in `audit.py`: a
  label's `published_at` is the earliest in its duplicate group, and every
  derived instant (both windows, the reference session, R3's label session)
  follows from it. *Known transition, recorded rather than patched:* a label
  with one window already written under its copy's stamp has its other
  window graded from the group's earliest.
- **Q24c — A void restores `changed_at` exactly.** Answer: *"option A
  then"*. A restored `active`/`demoted` status takes `changed_at` = the
  **`started_at`** of the earlier run that made the transition, which is what
  that run wrote, not that audit row's `at` (the run's finish, minutes
  later, which would start a since-demotion window late). **This amends R4
  step 4.** Built in `corollary/data/news/audit_void.py`.
- **Q24d — Widen the weekday runner's hours to cover R8.** Answer: *"winden
  the hours"* [sic]. Built in `scripts/weekday_session.py`:
  - **Trigger:** Task Scheduler at 08:30 local on weekdays (the machine is on
    Eastern time), plus at logon.
  - **Stop:** the later of the calendar close + 30 min and **18:05 ET**
    (`EARLIEST_STOP_ET`). That covers R8's 08:40 first attempts through its
    18:00 retries, half-days included.
  - **Runway:** it launches only with at least **60 minutes** (`MIN_RUNWAY`)
    before its stop, so the start-up catch-ups can finish. After 17:05 ET it
    launches nothing (`TOO_LATE`).
  - **The trigger's `StartBoundary` must carry no UTC offset**
    (`YYYY-MM-DDT08:30:00`). The old one carried `-05:00`, which fires an
    hour late once daylight saving starts. The weekday trigger was fixed on
    2026-11-14. The weekly-audit trigger carries the same offset and has
    not been changed; that waits on the owner.

## Owner decisions — 2026-11-16 (Q25)

Put to the owner on 2026-11-16, after live audit run 5's re-grade, and
answered the same day. Its `verdict_backfill` detail needed **2117**
`(ticker, session)` pairs across **1596** tickers. It attempted and stored
**100** (`VERDICT_BACKFILL_CAP`), held **2017**, and had **0** faults, which
left **2585** off-watch labels pending. At 100 pairs a weekly run, the oldest
held labels (session Nov 2) would pass decision 27's 28-day hold around Dec 1
and be written `off_watch_unverdicted`, never graded. The options were:
(b) raise the cap, (c) drain with extra off-hours audit runs, and (d) have
the daily refresh write a verdict for every off-watch label's ticker.

- **Answer:** *"implement d and have choose one of b or c which ever is best
  suited and most efficient for the task"*.
- **d — the daily refresh fills decision 27's need.** Built in
  `corollary/engine/scheduler.py`
  (`_tradeability` → `_off_watch_verdict_backfill`). After its own date's
  check, the `tradeability_cache` job reads the audit's need
  (`audit_runner.load_verdicts_needed`, the run's own loaders and
  `plan_audit`) and fills it oldest first, through the audit's backfill
  (`audit_runner.backfill_verdicts`) and `refresh_tradeability(session_date=S)`.
  - **Bound.** `TRADEABILITY_BACKFILL_BOUND = MAX_TICKERS_PER_RUN` (100),
    less today's `root_requests`, so the job's worst case on `paper-api.` is
    what it was before Q25. IPO lookups are bounded the same way.
  - **Only outside a regular session** (ruling P1, after the rules audit).
    The calendar's `nyse_session_containing(at)` is checked first, before
    the deps, the watch universe and the label plan; an open session is a
    logged stand-aside naming its date and the rule. It is asked **again
    before each session's refresh** (re-audit finding 4): the job passes
    `backfill_verdicts` a `stop` that answers while `nyse_session_containing`
    does, so a 09:29 run that reaches 09:30 keeps what it stored, asks
    nothing more, and logs `tradeability_backfill_stopped` with the rule, the
    session, the instant and the counts.
  - **Clear of the weekly audit's slot.** It stands aside within
    `TRADEABILITY_BACKFILL_AUDIT_CLEARANCE` (one job interval, 15 min) of a
    scheduled Saturday 10:00 ET slot, before or after. *Not in the rulings;
    added with L3 below and flagged for the owner:* the job now holds the
    run lock, and a scheduled run that finds it held skips for the week.
    *Amended 2026-11-20 (owner request):* a scheduled run refused by the
    backfill now waits, polling every `SENTIMENT_AUDIT_LOCK_POLL` (30 s), up
    to `SENTIMENT_AUDIT_LOCK_WAIT` (30 min) -- well inside the 2 h
    `RUN_DEADLINE` of `scripts/weekly_audit.py` -- logging each refusal with
    the rule, the holder, the elapsed wait and the instant. Freed in time,
    it is an ordinary scheduled run; still held at the bound, it skips for
    the week as before, and the skip says how long it waited. Refused by
    another audit run, it still skips at once. The start-up catch-up does
    not wait: the backfill stands aside until the catch-up has settled, so
    the catch-up cannot meet its lock.
  - **Never before the start-up catch-up has settled** (re-audit finding 1).
    The job has no catch-up of its own and first runs at the next 15-min
    grid slot, which can fall seconds after start while the sentiment
    audit's catch-up still polls for the paper positions -- or exactly on a
    deferred catch-up's close, since every close is on the grid (re-audit
    finding 3). `ContextServices.audit_catch_up_settled`, a `CatchUpGate`
    (an `asyncio.Event` behind corollary code, because the rule-9 isolation
    scan admits only corollary objects in the services), is cleared by
    `Scheduler.start()` for a scheduler that
    holds the `sentiment_audit` job (`ScheduledJob.catch_up_settled`) and
    set in a `finally` around `_sentiment_audit_catch_up`, whether it ran,
    skipped, deferred, failed or was cancelled. Until then the backfill
    stands aside, logged with the rule, checked just before it would try the
    lock. It is created set, so services with no audit job wired (a test's,
    the drain script's) never block the backfill.
  - **Never without an asset directory** (re-audit finding 1). With
    `services.assets.current()` `None` every attempted pair would fault
    `refresh_skipped` and check nothing, so the backfill stands aside, logged,
    before the lock -- no lock held for nothing, no 100 WARNING faults.
  - **It holds the audit's run lock** (audit finding L3), not an advisory
    check. `audit_runner.try_run_lock("tradeability_cache")` tries
    `_RUN_LOCK` without blocking and holds it for the watch universe, the
    need read and the backfill; if a run holds it, the job stands aside and
    logs it. **The holder is recorded** (re-audit finding 2): a run refused
    meanwhile raises `AuditAlreadyRunning` with `holder =
    "tradeability_cache"`, so the scheduled or catch-up run's skip reason and
    the manual `POST`'s 409 message and logged `inputs.holder` name the
    daily job's backfill -- which grades no label -- rather than "another
    audit run grading the same labels". The route's code and envelope are
    unchanged. The lock is a
    `threading.Lock` that every taker only *tries*, so holding it across
    awaits never blocks the event loop or the heartbeat. The upsert in
    `store_tradeability` is unchanged, because decision 27's damaged-row
    replacement depends on it. **Remaining residual:** the lock is
    per-process, so a second process (the drain script) is excluded by its
    own refusal, below, and not by the lock. Between a job's backfill and a
    run's, the only difference left in the same pair's row is the vendor's
    *current* answers -- the bars are the same point in time (sessions
    before S); `has_options` and the standard root are asked again.
    **The start-up race is closed** (re-audit findings 1 and 3): a catch-up
    waiting on the positions refresher, or deferred to the close, can no
    longer find the lock held by this job, because the job does not try the
    lock until the catch-up has settled. A manual `POST` can still meet the
    job's lock, and is refused with the holder named.
  - A stale but existing verdict is never replaced (decision 25).
  - A skip stays a skip unless the backfill **stored** a verdict (audit L1).
  - The asset directory's staleness is logged once a run on the job's path,
    by today's check (ruling P2); the audit's run keeps its own line.
  - Movers' fail-closed reading (decision 21) is unchanged. **Known
    residual (audit L4, recorded rather than patched, since decision 21 is
    the owner's):** `signals.latest_verdicts` takes the newest row on or
    before today with no age bound, so a verdict this job backfills for an
    old S can stand in for today's when today's check defers or errors.
    This is the Q24a carry above, now reached daily: the Phase 4 scanner must
    require `session_date == today`.
  - It is never a rule-9 input: a failure is logged with the rule, the
    inputs and the timestamp, and the job's own outcome is unchanged.
  - **Drain rate.** The job runs every 15 min (`TRADEABILITY_EVERY`, an
    `EveryInterval` grid) while the app is up. The app is up from 08:30 ET to
    the later of close + 30 min and 18:05 ET (Q24d). On a regular day that is
    **3 or 4 off-session runs before the open** (08:45, 09:00, 09:15, and
    08:30 only when the scheduler is already up at that instant -- a grid job
    runs at the first slot *after* start; 09:30 is in the session) and **9
    after the close** (16:00 through 18:00; the close's own instant is outside
    the session), so **12 or 13 runs** a weekday. A 13:00 half-day has 21
    after the close. At most 100 pairs a run, less today's own root requests,
    that is up to about **1200-1300 pairs a trading day**, so the 2117-pair
    backlog would clear in about two trading days
    without the script. Saturday's headless window (`weekly_audit.py`) adds
    about **none**: it is up only around the 10:00 slot, which the clearance
    above keeps the job out of.
- **b once, not c, and not a permanent raise.** Built as
  `scripts/backfill_verdicts.py --max-pairs N [--dry-run]`, run once
  off-hours. It writes no `audit_run` row and no grade; the next audit run
  grades. Its refusals each log the rule, the inputs and a timestamp, as
  JSON lines on stderr alongside the backfill's own (audit L5):
  - **A regular session open** (exit 3), per the calendar: at launch, and
    again before each session's refresh (audit M1), so a drain launched at
    09:22 stops at the first session boundary after 09:30 and keeps what it
    stored. `--dry-run` refuses the same way.
  - **The app is running** (exit 5, audit M2): anything answering
    `GET /api/health` on 127.0.0.1:8000. Alpaca's 200/min is per account,
    and the script's own limiter admits 190 a minute on top of the app's.
    Stop the app first. Asked at launch and **again before each session's
    refresh** (re-audit finding 5), once a session and never once a pair,
    because `weekday_session.py` (08:30 ET) and `weekly_audit.py` (Saturday
    09:45 ET) can launch the headless app mid-drain: the drain then stops,
    keeps what it stored, prints the partial counts and exits 5.
  - **No Alpaca keys** (exit 6), naming the variables only (rule 6).
  - **No IPO-date source** (exit 7, audit L6): without `FINNHUB_API_KEY` a
    partial window is stored `ipo_date_unavailable` for a past session,
    permanently. `--dry-run` says whether one is configured.
  - **An unexpected error** (exit 8, re-audit finding 6): logged with its
    type and the rule, never its message or a traceback, which could carry
    vendor text. No other exit status is returned.
  - **Why not c.** c would take about 21 audit runs, each making about 1313
    bar requests, and would leave 21 run rows and 21 transition evaluations.
  - **Why b once.** It is one pass of at most about 2117 standard-root
    requests on the `paper-api.` bucket, fewer in practice because a
    ticker's root answer is asked once for all its sessions.
  - **Why not a permanent raise.** The runner keeps `VERDICT_BACKFILL_CAP =
    100`. A start-up catch-up during market hours then cannot spend the
    trading bucket the order path will share from Phase 6.
- **Decision 27 is amended** in `corollary/data/news/audit.py`: the daily
  refresh now covers its need too.

## Owner decisions — 2026-11-17 (Q26)

Found on 2026-11-16 while chasing a hanging test. `Scheduler.aclose` cancels
each job task once and then waits with no bound. Every job re-raises at once
except the sentiment audit. Its `_on_worker` waits on the first cancel for
its thread to finish the step in flight, then records the run `failed`. The
docstrings said a *second* cancel is honoured "so shutdown is never held
hostage", but nothing sent one: not `aclose`, and not the lifespan's handling
of a manual run. The unattended runners (`weekday_session.py`,
`weekly_audit.py`) kill the server `STOP_GRACE` (60 s) after asking it to
stop, so a slow step could cost the cleanup after it: the session drain, the
notice flush, `runtime.aclose()`, and the rule 9 alerts' Discord grace. The
options were:

- **(i)** keep it, and document it;
- **(ii)** bound `Scheduler.aclose` for every job;
- **(iii)** bound only the audit job and the manual run.

- **Answer:** *"lets do iii for now"*. The owner chose (iii) **for now**, so
  (ii) stays the general option. Wrapping `ContextSessions.drain` in an outer
  `asyncio.wait_for` was proposed separately and **not** approved.
- **The bound.** `AUDIT_SHUTDOWN_GRACE = 20 s` (`engine/scheduler.py`), with
  its reasoning stated there. Each threaded step is one SQLite transaction,
  and a contended lock fails after the 5 s busy timeout, so a step plus its
  failure write takes about 10 s; 20 s is twice that. The rest of shutdown is
  bounded at about 15 s (the 10 s drain and the 5 s Discord grace), which
  leaves about 25 s of the 60 s `STOP_GRACE`.
  `await_cancelled_audit` waits that long after the first cancel. If the task
  is still running, it logs `sentiment_audit_shutdown_grace_expired` and
  cancels it again, with that log line's correlation id as the cancel
  message. The log line carries the rule, the job, the grace, the wait and
  the timestamp. `Scheduler.aclose` applies it only to a job with
  `ScheduledJob.shutdown_grace`, which only `sentiment_audit` sets. The
  lifespan applies it to the manual run's task. Every other job is still
  cancelled once and awaited with no bound.
- **After the second cancel nothing is written.** `_on_worker` raises
  `WorkerAbandoned`, a `CancelledError`. The run logs
  `sentiment_audit_run_abandoned` (run id, stage, step, trigger, the run's
  correlation id and the cancel message), skips its failure write and
  releases the lock. Its row stays `running`, and the next run closes it past
  `ABANDONED_RUN_AFTER` (2 h), as it would after a process that died
  mid-run -- except in the two cases below. The failure write is skipped
  because the abandoned thread may still hold SQLite's write lock or be about
  to commit. A write there would wait out the busy timeout, and could land
  before the step's own commit. The abandonment line names the step.
- **When the second cancel lands in the failure write (audit L1).** If the
  held step ends just before the grace runs out, the run is already
  recording its failure when the second cancel comes. It lands in that
  write's wait, which treats it as a first cancel and waits for the
  `_fail_run` thread. So the row is closed `failed` by the runner's own
  write, and shutdown waits for that one write: a single UPDATE, bounded in
  practice by the 5 s SQLite busy timeout, so about the grace plus 5 s at
  worst. The ruling was **no third cancel**; the docstrings and the
  `sentiment_audit_shutdown_grace_expired` rule say this instead, and
  `test_a_step_ending_just_at_the_grace_leaves_shutdown_waiting_on_the_failure_write`
  pins it.
- **Known residual: an abandoned close (audit L3).** The abandoned step may
  still commit. If it was `_close_run`, the run can end `succeeded` with its
  source-status transitions standing and their notices never queued, because
  `_emit_all` never runs. Before Q26 the failure write blocked behind that
  commit and logged `sentiment_audit_failure_not_applied`. The ruling was
  **no new mechanism**: it is narrow and not money-bearing.
  `sentiment_audit_run_abandoned` names the step and says so in its message
  and rule, and
  `test_an_abandoned_close_can_still_commit_succeeded_and_its_notices_are_never_queued`
  pins it.
- **The abandoned thread is not stopped.** On Python 3.14, `asyncio.run`
  (uvicorn's runner) waits up to 300 s for the default executor at exit. The
  interpreter's `concurrent.futures` exit hook then joins its threads with no
  bound. So process exit still waits for the step to end, and the runners'
  kill at `STOP_GRACE` ends it if it is still running. The difference is that
  the lifespan's cleanup has run by then. The kill loses only the step's
  uncommitted transaction, which SQLite discards. Nothing half-commits.
  There is no cooperative stop flag, because the steps have no boundary
  inside them: each is one database function on one thread, and the thread
  ends when the step does.
- **Rule 9 is untouched.** Shutdown never calls `halt()` and never writes
  `engine_state`, and the watchdog and heartbeat keep their shutdown order.

---

## Decisions

Each one records what it rules out.

### R1. `phase3-fixes` merges as it stands, first

Both commits change which labels are graded and from when. The dedup fix
decides which copy of a syndicated story is the graded one, and the
reference-instant fix decides where its window starts. Everything else in this
spec assumes both. After the merge, the live app must be restarted so the
weekday and weekly runners load the new code.

*Rejected:*
- **Cherry-picking one commit.** A re-grade with the new reference rule but no
  dedup fix counts each Motley Fool story twice.
- **Re-implementing on a fresh branch.** The branch is reviewed, tested and
  merges cleanly.

### R2. Stored articles are re-linked once, before the re-grade

`corollary/data/news/ingest.py` gains `relink_stored(session, *, since)`.

- **What it does.** It runs decision 3's canonical matcher, including
  `SYNDICATION_SKEWS`, over every stored article published since `since` that
  has no `canonical_id`.
- **What it never does.** It sets `canonical_id` only. It never deletes a row,
  never changes a row that is already linked, and never rewrites a timestamp.
- **Idempotent.** It returns the links it made. A second run makes none.
- **How it runs.** `scripts/relink_articles.py` calls it and prints the
  count.

*Rejected:*
- **Leaving stored duplicates alone.** The re-grade would then count
  syndicated stories twice, which step 6's "one label per story, ticker and
  source" exists to prevent.
- **Doing the re-link inside a migration.** Data logic in a migration cannot be
  tested against the matcher it duplicates, and it would freeze today's
  matcher into the schema history.

### R3. A source is eligible only across at least five sessions (Q23b)

Every eligibility check in decision 13 gains a session floor beside its label
floor:

| Transition | Label floor | Session floor | Counted over |
|---|---|---|---|
| `unaudited → active` / `unaudited → demoted` / `active → demoted` | ≥30 graded directional labels in each window | **≥5 distinct sessions in each window** | the trailing 28 calendar days |
| `demoted → active` | ≥100 in each window | **≥5 distinct sessions in each window** | labels graded since the demotion |

- **What a label's session is.** The regular session, from `calendars.py`,
  that contains the label's **reference instant**. A story published after the
  close is referenced at the next open, so it counts toward that session. A
  half-day is a session.
- **What is counted.** Sessions are counted per source, window and basis, over
  exactly the labels the label floor counts.
- **The constant.** `MIN_ELIGIBLE_SESSIONS = 5`, beside the existing floors in
  `corollary/data/news/audit.py`.
- **What is stored.** `sentiment_accuracy` gains `sessions` (nullable integer,
  ≥0), so the figure a transition acted on is the figure stored. NULL means
  the row was written before Q23b and carries no session count. No reader may
  treat NULL as eligible.

**Known limit, recorded rather than changed:** the rule is literal. Twenty-six
labels on one day plus one on each of four other days passes. A cap on any one
session's share would be a rule the owner did not ask for.

*Rejected:*
- **A per-session share cap** (above).
- **Counting calendar days.** Weekends and holidays are not trading days.
  Sessions are what `calendars.py` defines and what the windows are measured in.
- **Applying the floor to demotion only.** Re-promotion has the same
  correlation problem. The owner's answer was about eligibility in general.

*Amended 2026-11-16, as built (U3, PR #13):* the column is migration **0017**
(`0017_accuracy_sessions.py`), with no backfill of pre-Q23b rows. The reader
that serves sessions and `sessionsNeeded` is
`corollary/api/routes/sentiment.py`, not `settings.py` as *Module layout*
says; the route path is unchanged. Under Q24b, a label's session follows from
its twin group's earliest stamp.

### R4. Voiding a run: archive, restore, record — through a script, never a route

Amends parent Q6 (*"the weekly audit run is the **only** writer"*). The audit
remains the only thing that **decides** a status. Voiding may only restore a
status that an earlier, non-voided run decided, or the initial `unaudited`.
It cannot set a status no run computed.

**What voiding run N does,** in one transaction:

1. **Marks the run.** `audit_run` gains `voided_at` and `void_reason`, both or
   neither. `status` keeps recording what the run did. Whether it stands is a
   separate fact.
2. **Archives its grades.** Every `label_outcome` row with `audit_run_id = N`
   moves to a new table, `label_outcome_voided`. It has the same columns, plus
   `voided_at`, and is keyed `(label_id, window, audit_run_id)`. The move frees
   `label_outcome`'s primary key, so the next run grades those labels afresh.
   Nothing is deleted.
3. **Leaves its accuracy rows alone.** Run N's `sentiment_accuracy` rows stay
   where they are. **Every reader excludes voided runs:** the latest run that
   Settings shows, the since-demotion basis, and any query for "the last run".
4. **Restores each source that run N moved.** The earlier status comes from
   run N's `audit_log` row for that source (`previous_value`):
   - **Back to `unaudited`:** `changed_at`, `reason` and `audit_run_id` set to
     NULL, as the existing CHECK requires.
   - **Back to `active` or `demoted`:** `changed_at` is the `at` of that
     source's previous non-voided `sentiment` audit row. `audit_run_id` is that
     row's run, joined the way `SentimentSourceStatus` documents. `reason` is
     *"restored when run N was voided: <void reason>; the original reason text
     is not retained"*. `audit_log` never stored the original reason, and this
     spec does not invent one.
     *Amended 2026-11-11 by Q24c:* `changed_at` is that row's **run's
     `started_at`**, not the row's `at`. A run stamps its transitions'
     `changed_at` with its start, and its audit row's `at` is its finish, so
     restoring `at` would have moved the since-demotion basis minutes late.
     The restore now reproduces exactly what the earlier run wrote.
   - **A source run N did not move** is untouched.
5. **Records it.** One `audit_log` row per restored source: category
   `sentiment`, previous value = run N's status, new value = the restored one.
   If `ck_audit_log_sentiment_row` refuses that shape, the migration widens it
   for exactly this shape.
6. **Notifies,** after the commit: one `sentiment_audit_voided`, severity
   `info`, `account` null, naming the run, the reason and each source's
   restoration. It is seeded **bell on, Discord on**, the same channels as
   `sentiment_repromoted`. It changes what Phase 4 may read, and Q8 is *"any
   action i do should be put into the discord."*

**Refusals.** The tool refuses, writes nothing, emits nothing, and logs the
rule, the inputs and the time (rule 8's spirit), when any of these holds:

- the run does not exist
- the run is `running`, or any run is `running`
- the run is already voided
- the run is not the **latest non-voided terminal run** (voiding an older run
  would orphan the later runs that built on its statuses)
- the reason is empty

**Where it lives.** Only as `scripts/void_audit_run.py --run-id N --reason
"…"`, launched with `uv run --env-file .env python scripts/void_audit_run.py
…` so the Discord webhook comes from the launcher (CLAUDE.md). It may run with
the app up: it is one short transaction under the session's `busy_timeout`.

*Rejected:*
- **An API route or a Settings button.** That would become the reinstate
  control Q6 refused: a click that undoes demotions.
- **Deleting the outcomes.** The record of what a notified demotion rested on
  would disappear.
- **Re-grading in place.** `label_outcome` holds one row per label and window,
  so overwriting it erases the voided grade.
- **Overriding the status without voiding the run.** It would state a status no
  run computed.
- **Notifying through `sentiment_repromoted`.** A void is not a re-promotion,
  and a Discord line saying Massive was "re-promoted" would be false.

*Amended 2026-11-16, as built (U4, PR #13):*
- **Migration 0018** (`0018_audit_void.py`) adds `voided_at` and
  `void_reason` with **`ADD COLUMN` plus a column CHECK**, not the batch
  rebuild *Database* describes. A rebuild's `DROP TABLE audit_run` fails
  under `PRAGMA foreign_keys=ON` once `label_outcome`, `sentiment_accuracy`
  or `sentiment_source_status` holds a row pointing at it, which the live
  database does. SQLite enforces a column CHECK naming another column exactly
  as it does a table CHECK, and a test upgrades a referenced `audit_run`.
  `ck_audit_log_sentiment_row` was widened (a batch rebuild of `audit_log`,
  which nothing references) for the `active|demoted → unaudited` restore row.
- **Parent Q6's "only writer"** stands amended as this decision says, and
  so does `audit_runner.py`'s module docstring, which still calls the runner
  the **only writer** of `audit_run`, `label_outcome`,
  `sentiment_accuracy`, `sentiment_source_status` and the `sentiment` audit
  rows. Read it with R4: `audit_void.py` also writes them when it voids a
  run, and only to restore a status a run decided. *(The docstring itself
  does not yet say so; that is a code edit, outside U7.)*
- **Step 4 is amended by Q24c** (above).

### R5. Run 2 is voided and the audit re-run, in this order

1. R1 merged, and the live app restarted on it.
2. R2's re-link run against the live database.
3. R3 deployed.
4. Run 2 voided with the reason *"graded under the reference rule the owner
   replaced on 2026-10-22, before that change merged (Q23a, 2026-11-09)"*.
5. A manual audit run through `POST /api/settings/sentiment/audit` (hourly
   limit, Discord notice per decision 20).

**The expected outcome is stated in advance,** so a different one is read as a
finding. Every label run 2 graded was on a story published between 2026-10-30
07:07Z and 2026-11-03 08:19Z. Under the new reference rule those fall in
**three sessions** (Oct 30, Nov 2 and Nov 3), below R3's five. **From run 2's
labels alone, both sources stay `unaudited`.** If a source is eligible in the
re-grade, the eligibility must come from labels collected on Nov 9 or later,
and the run's `sentiment_accuracy.sessions` shows which sessions they span.
That is the rule working, not a fault.

*Rejected:* voiding before R1 and R3 are live. The re-grade would then run
under the same rule that is being voided.

*Amended 2026-11-16, as run (U5):* steps 1–4 ran in order on 2026-11-10/11;
run 2 was voided at 2026-11-11 00:59:52Z with 348 outcomes archived. Step 5
was **not** a manual `POST`: `weekly_audit.py`'s start-up catch-up re-graded
the voided Nov 7 slot as **run 3** (trigger `scheduled`), which made a
manual run redundant. The outcome matched the expectation: both sources
stayed `unaudited`, Massive on 3 sessions and rules on 4. A further void and
re-grade under Q24 is planned (see *Order of work*, U5).

### R6. An economic release's figures live in their own table, keyed to the calendar row

`econ_release_value(release_id, release_date, series_id, period, actual,
prior, unit_label, vintage_date, fetched_at)`:

- **Key.** Primary key `(release_id, release_date)`, which joins to
  `calendar_event.vendor_id = "<release_id>:<release_date>"`.
- **Columns.** `actual` and `prior` are `Money` (TEXT, exact `Decimal`).
  `period` is the observation date the figure describes.
- **Written once, never updated.** Q23c keeps the first print.
- **How the API uses it.** `GET /api/calendar` fills an economic row's `prior`
  and `actual` from this table. `calendar_event.prior` and `.actual` stay null
  on economic rows.

*Rejected:*
- **Writing into `calendar_event.prior` and `.actual`.** The daily dates
  refresh would null them through `_apply` (*Problem*, point 4).
- **Teaching the refresh to carry the values through.** That is two producers
  on one row, and the next producer to touch it would have to remember too.
- **Storing FRED's current value.** Revisions would rewrite a release's figure
  after the fact, against Q23c.

*Amended 2026-11-16, as built (U6, PR #15):*
- **Migration 0019** (`0019_econ_release_value.py`) adds `econ_release_value`
  as above and a second table, **`econ_release_fetch`**: the last attempt
  per release (`attempted_at`, an `outcome` of `figure | not_posted |
  no_value | refused | error`, and a `detail`). It gives R8's `unavailable`
  state the vendor's reason to quote while the value table stays
  write-once. Neither table has a foreign key to `calendar_event`; the join
  is by value, on read.
- **`unitLabel` is the existing `unit` field.** `GET /api/calendar` serves
  the seed's `unit_label` in `CalendarEventItem.unit`, which the panel
  already rendered beside `prior`/`actual`, rather than a new `unitLabel`
  field (*API*, below). `valueState` and `valueNote` are new, as specified.

### R7. The figure is FRED's release-day vintage, transformed in exact `Decimal` by a seed

**The seed.** `corollary/data/seeds/econ_release_series.csv`, with columns
`release_id, series_id, transform, scale, unit_label, decimals`. It is
validated whole, together with `econ_release_times.csv`: every release must
have exactly one series row, and an unknown transform or an unparseable scale
refuses both seeds. That follows the existing calendar-seed rule.

| `release_id` | Series | `transform` | `scale` | `unit_label` | `decimals` |
|---|---|---|---|---|---|
| 10 | `CPIAUCSL` | `pct_change` | 1 | `% m/m` | 1 |
| 50 | `PAYEMS` | `change` | 1 | `K` | 0 |
| 46 | `PPIFIS` | `pct_change` | 1 | `% m/m` | 1 |
| 192 | `JTSJOL` | `level` | 1 | `K` | 0 |
| 11 | `ECIALLCIV` | `pct_change` | 1 | `% q/q` | 1 |
| 188 | `IR` | `pct_change` | 1 | `% m/m, NSA` | 1 |
| 53 | `A191RL1Q225SBEA` | `level` | 1 | `% q/q SAAR` | 1 |
| 54 | `PCEPILFE` | `pct_change` | 1 | `% m/m, core PCE` | 1 |
| 51 | `BOPGSTB` | `level` | 0.001 | `$B` | 1 |
| 9 | `RSAFS` | `pct_change` | 1 | `% m/m` | 1 |
| 27 | `HOUST` | `level` | 1 | `K SAAR` | 0 |
| 97 | `HSN1F` | `level` | 1 | `K SAAR` | 0 |
| 229 | `TTLCONS` | `pct_change` | 1 | `% m/m` | 1 |
| 13 | `INDPRO` | `pct_change` | 1 | `% m/m` | 1 |
| 291 | `EXHOSLUSM495S` | `level` | 0.000001 | `M SAAR` | 2 |

*Amended 2026-11-16, as built (U6):* the seed gained a seventh column,
**`frequency`** (`monthly | quarterly`; `ECIALLCIV` and `A191RL1Q225SBEA` are
quarterly, the rest monthly). It is a deliberate extension of the column list
above: the fetch's lookback must be known before the request, and
`econ_values.py` checks that x(t−1) and x(t−2) are each exactly one period
earlier at that frequency. `decimals` is also capped at 4, the stored
precision.

**The fetch.** `fred/series/observations` with `series_id`,
`realtime_start = realtime_end = release_date`, `file_type=json`, and an
`observation_start` far enough back to hold three observations (four months
for monthly series, a year for quarterly). Because this is the vintage, a
fetch weeks late still returns exactly what was known on release day.
*Amended 2026-11-16, from U6a's probe:* the query is
**`realtime_start = D-1`, `realtime_end = D`**, still one request, because
FRED clips each row's real-time period to the window and a single-day window
cannot show what changed (see *Not verified*). `output_type=1` and `units=lin` are sent
explicitly, and a response whose echo differs is refused. The four-month and
one-year lookbacks are a **floor**. The lookback was widened to **6 months
for monthly series and 15 for quarterly** (`fred.VINTAGE_LOOKBACK_MONTHS`),
still one request.

**Which observation.** The release's observation is the latest-dated one
**new or revised on `release_date`**, by its `realtime_start` (see *Not
verified* for the fallback). If there is none, FRED has not posted the release
yet. The **prior** is the observation immediately before it **in the same
vintage**, so a revision published that day is reflected in it.
*Amended 2026-11-16, as built:*
- **The release-day vintage** is the rows with `realtime_end == D`, and the
  prior is read **from that vintage**. Without that filter PAYEMS' prior came
  out stale (159044, giving +249K instead of +165K).
- **New or revised** is decided by `Decimal` comparison against the row
  ending `D-1`, never by string.
- **x(t) must also be the newest-dated observation in the D vintage.** A day
  that only revises an older month is not the release, so it reads as not
  posted and the catch-up retries.

**The transforms,** computed in `Decimal` with every input parsed from text:

- `level` gives *x*ₜ.
- `change` gives *x*ₜ − *x*ₜ₋₁.
- `pct_change` gives (*x*ₜ / *x*ₜ₋₁ − 1) × 100.

The same transform applies to the prior, one period earlier. Then `scale` is
applied. The stored value is quantized to 4 decimal places (`ROUND_HALF_EVEN`),
and display rounds to `decimals` (`ROUND_HALF_UP`, see *Not verified*). A `"."`
anywhere in the observations needed means no value; it is never treated as
zero.

**Labelling.** Every value is labelled *computed from FRED's levels* together
with the series ID, so a 0.1 difference from the agency's printed figure has an
explanation on screen.

*Rejected:*
- **FRED's own `units=pch`.** It is server-computed and server-rounded, and its
  rounding cannot be seen or pinned in a test.
- **`output_type=4` (initial release only).** It returns the prior as it was
  first printed, not the revised prior the agency's change is computed against.
- **CPI year-on-year** (Q23c: monthly).
- **`UNRATE` for the jobs report.** The payroll change is the headline the
  owner took as the default.

### R8. First prints are fetched on a release-day schedule, with a daily catch-up

A new context job, `econ_first_prints`, runs in the scheduler:

- **Release day:** at the release's ET time plus 10 minutes, then every 30
  minutes until 18:00 ET, until the value lands.
- **Daily at 10:00 ET:** fills every economic row in the stored calendar
  window whose release time has passed and which has no value. The vintage
  makes a late fill exact.
- **Never a rule 9 producer** (parent decision 1). It is included in the
  `risk`-marked isolation test, which runs the shipped job set.
- **Budget:** at most ~20 releases a month, each one request per attempt,
  against FRED's 120/min. Negligible.

**The display state is derived on read, never stored:**

| State | When |
|---|---|
| `upcoming` | before the release time |
| `awaiting` | released, no value yet, and within 7 calendar days |
| `filled` | a value is stored |
| `unavailable` | no value after 7 days; *"not published on FRED within 7 days"*, or the vendor's reason when the series refused (for example, NAR licensing) |

The daily catch-up keeps trying `unavailable` rows while they remain in the
stored window. The 7 days only changes what the row says.

*Amended 2026-11-16, as built (U6, PR #15):* the job's rule lives in
`corollary/data/econ_first_prints.py` and the store in
`corollary/data/econ_store.py`; `engine/scheduler.py` only wakes it. First
attempts fall at 08:40, 09:25 and 10:10 ET for the seed's three release
times. The release-day grid is not gated on the NYSE session (the jobs report
can land on Good Friday). The 10:00 ET catch-up runs on NYSE trading days
only. Every attempt happens only while the backend runs, so Q24d widened the
weekday runner to 18:05 ET.

*Rejected:*
- **A single fetch at +10 minutes.** FRED's posting lag is not verified, and a
  release that misses it would stay empty for good.
- **Polling every release hourly.** It spends requests on rows already filled.
- **A stored state column.** It would drift from the value table it describes.

### R9. Settings shows sessions and voided runs; there is still no control

For each source, window and basis, `GET /api/settings/sentiment` adds:

- `sessions` and `sessionsNeeded`, beside the existing label counts
- `voidedRuns`: id, `voidedAt` and reason for each voided run

The last run it reports is the latest **non-voided** run.

Settings renders:

- *"2 of 5 sessions"* beside *"31 of 30 labels"*, as a sample-size fact, not a
  caution colour, exactly as the existing insufficient-sample state does
- one line per voided run: *"Run 2 voided on Nov 9: graded under the replaced
  reference rule."*

There is **no void button and no re-promote button** (R4, parent Q6).
`web/src/lib/settings.ts` still computes no eligibility of its own. The
server's figure is the one shown.

---

## Design

### Module layout

```
corollary/
├── data/
│   ├── news/
│   │   ├── audit.py              # + MIN_ELIGIBLE_SESSIONS, session counting (R3) — pure
│   │   ├── audit_runner.py       # + writes sentiment_accuracy.sessions; excludes voided runs
│   │   ├── audit_void.py         # R4: void a run — checks pure, one store function
│   │   └── ingest.py             # + relink_stored (R2)
│   ├── providers/fred.py         # + vintage observations (R7)
│   ├── econ_values.py            # R7: the transform (pure) and the value store
│   ├── calendar.py               # unchanged producers; read joins econ_release_value
│   └── seeds/
│       ├── econ_release_series.csv   # R7
│       └── calendar_seeds.py     # + validates the two econ seeds together
├── engine/scheduler.py           # + econ_first_prints (R8)
└── api/routes/
    ├── calendar.py               # prior/actual/unit/state from econ_release_value
    └── settings.py               # sessions, voidedRuns, non-voided last run
scripts/
├── relink_articles.py            # R2
└── void_audit_run.py             # R4
```

*Amended 2026-11-16, as built:* the sentiment readout's route is
`api/routes/sentiment.py`, not `settings.py` (R3's note). The econ half split
three ways: `data/econ_values.py` (the pure transform),
`data/econ_store.py` (the value and fetch tables) and
`data/econ_first_prints.py` (R8's due-attempt rule and the fill run). Q24d
changed `scripts/weekday_session.py`.

### Database

One migration per unit that adds schema. Numbers are assigned at merge, after
head `0016` (Q22's rule).

- `sentiment_accuracy` + `sessions` INTEGER NULL, `CHECK (sessions IS NULL OR
  sessions >= 0)` (R3).
- `audit_run` + `voided_at` (`UtcDateTime`) and `void_reason` (TEXT), with a
  both-or-neither CHECK. This is a batch rebuild, because SQLite cannot add a
  table CHECK in place (R4).
- `label_outcome_voided`: `label_outcome`'s columns and CHECKs, plus
  `voided_at`. Primary key `(label_id, window, audit_run_id)` (R4).
- `notification_route` + the `sentiment_audit_voided` rows (bell on, Discord
  on). If needed, `ck_audit_log_sentiment_row` is widened for the restore row
  (R4).
- `econ_release_value` (R6).

*Amended 2026-11-16 — final numbers, assigned at merge (Q22's rule):*

| Revision | File | What | Unit |
|---|---|---|---|
| `0017` | `0017_accuracy_sessions.py` | `sentiment_accuracy.sessions` (R3) | U3 |
| `0018` | `0018_audit_void.py` | the void tool: `audit_run` void columns (`ADD COLUMN` + column CHECK, not a rebuild; R4's note), `label_outcome_voided`, the widened audit-log CHECK, the `sentiment_audit_voided` routes (R4) | U4 |
| `0019` | `0019_econ_release_value.py` | `econ_release_value` and `econ_release_fetch` (R6's note) | U6d |
| `0020` | `0020_unverdicted_skip.py` | the `off_watch_unverdicted` skip reason on `label_outcome` and `label_outcome_voided` (Q24a) | Q24a |

`0019` was written as a provisional `0017` on `phase3-rem-econ`. `0020` was
written as `0019` on `phase3-rem-grading`, and renumbered at merge because
PR #15 landed first. Head is **`0020`**; the live database is at `0019` until
the grading branch deploys.

### API

- **`GET /api/calendar`:** each `economic` row gains `unitLabel`, `valueState`
  (`upcoming | awaiting | filled | unavailable`) and `valueNote` (*"computed
  from FRED's levels: CPIAUCSL"*, or the reason it is unavailable).
  `prior`/`actual` come from `econ_release_value`. The blanket *"Prior and
  actual for economic releases are not filled yet"* reason string is removed.
  `consensus` stays `"unavailable"` (parent Q3).
  *Amended 2026-11-16, as built:* `unitLabel` is served as the existing
  `unit` field (R6's note). `prior` and `actual` are the stored figure rounded
  server-side to the seed's `decimals` (`ROUND_HALF_UP`), and are null unless
  `valueState` is `filled`.
- **`GET /api/settings/sentiment`:** as R9 (served from
  `api/routes/sentiment.py`).

### Frontend

- **Calendar panel:** an economic row shows `actual` and `prior` with
  `unitLabel`, and the note on hover. `awaiting` and `unavailable` are designed
  states, not blanks. Consensus still reads *"not available"*.
- **Settings:** as R9. `NotificationEvent`, the label map and the severity map
  gain `sentiment_audit_voided` (`info`).

---

## Testing

The audit's eligibility rules and the void tool get the careful coverage,
because they set what Phase 4 may read. Everything else gets the standard
coverage.

**R3 — sessions, pure, one test per boundary:**
- 30 labels over 4 sessions: not eligible. Over 5: eligible.
- Each window is counted separately (5 sessions in 1h, 4 in 1d: not eligible).
- 100 since demotion over 4 sessions: no re-promotion.
- `unaudited → active` needs 5.
- An after-close story counts toward the next session.
- A half-day counts as one session.
- A NULL `sessions` row is never eligible.

**R4 — the void:**
- It moves exactly run N's outcomes to the archive, and the next run then grades
  those labels.
- It restores `unaudited` with NULLs.
- It restores `active`/`demoted` from the earlier transition, with the
  restored-reason text.
- It leaves an unmoved source alone.
- It writes one `audit_log` row per restored source and one notification.
- Every refusal (nonexistent, running, already voided, not latest, empty
  reason) writes nothing, emits nothing and logs its rule.
- A voided run is absent from `GET /api/settings/sentiment`'s last run and from
  the since-demotion basis.

**R2 — the re-link:**
- It links a recorded Motley Fool pair.
- It never touches a row that is already linked, and never deletes.
- A second run links nothing.

**R7 — the figures, on keyed fixtures** recorded through the launcher (U6's
probe):
- A past CPI release's vintage, with the expected `pct_change` hand-computed.
- `change` (`PAYEMS`), and `level` with `scale` (`BOPGSTB`).
- A vintage requested for the day before posting, which gives no value.
- `"."` in the needed observations, which gives no value.
- `EXHOSLUSM495S`'s answer, whatever it is.
- The seed pair refused for a missing release row, an extra row and an unknown
  transform.

**R6 and R8:**
- After a value is stored, the release-dates refresh runs and the API still
  serves the value.
- `valueState` at each boundary, including exactly 7 days.
- The new job is in the rule 9 isolation test (`risk`).

**Frontend:** Settings renders the sessions fact and the voided-run line, with
no control. The calendar renders all four value states.

**Before every engine change:** `uv run python -m pytest -m risk`, `uv run
python -m mypy corollary`, and `npm run typecheck` and `npm run test` in
`web/`. *(Amended 2026-11-16: was `uv run mypy corollary`, whose console shim
Application Control blocks in a fresh worktree; see CLAUDE.md.)*

---

## Doc amendments

*Applied 2026-11-16 in U7, each marked below. None was struck.*

- **Parent spec** — the first one is made in this spec's own commit; the rest
  land in U7:
  - **Q23**, pointing here. *Applied in `a308f7c`, verified 2026-11-16. U7
    added a Q24 entry beside it.*
  - **Decision 13's eligibility** gains the session floor (R3). *Applied.*
  - **Q6's** "only writer" is amended by R4. *Applied, in Q6, decision 13 and
    the API section's "No reinstate route" line.*
  - **Step 5's status:** the mean-versus-median question is closed (P1).
    *Applied.*
  - **Step 6's status:** run 2 happened, and was voided by Q23a. "The run" is
    met by the re-grade run. *Applied: the re-grade is run 3.*
  - **Step 7's status:** actuals come through this spec. *Applied.*
  - **Migration numbers:** steps 5–7 read their final numbers (`0013`, `0014`,
    `0015`), no longer "provisional". Step 7's text still says `0013`.
    *Applied: step 7's `calendar_event` is `0015`, confirmed against
    `corollary/db/migrations/versions/`.*
- **PRD §10:** the notification table gains `sentiment_audit_voided`.
  *Applied.*
- **PRD §8.3:** economic releases show the first print, computed from FRED's
  levels. *Applied, naming the release-day vintage.*
- **CLAUDE.md:** the `alembic upgrade head` comment says *"0004 is head"*. It
  is stale (head `0016` today) and will be again after this spec. *Applied:
  head `0020`.*
- *Added 2026-11-16, from what U3–U6 and Q24 found, and applied in U7:*
  - **CLAUDE.md, Commands:** `uv run mypy corollary` becomes `uv run python
    -m mypy corollary`, and the paragraph after the block no longer says
    `uv run mypy` is fine. In a fresh worktree its shim is blocked by the
    same `os error 4551`.
  - **CLAUDE.md, Commands:** the weekday runner's line reads *"until the later
    of the calendar close + 30 min and 18:05 ET (R8)"* (Q24d).
  - **This spec:** *Testing*'s "before every engine change" line takes the
    same `python -m mypy` form.

---

## Order of work

Each unit gives **Status**, **Depends on**, **Files** and **Done when**, like
the parent's list.

*Statuses amended 2026-11-16 (U7).* The original "not started" stays beside
each, struck through by the dated status that replaces it.

**U1. Merge `phase3-fixes`.**
- *Status:* ~~not started~~ **merged in PR #13** (`c7cda12`, 2026-11-11),
  through `phase3-rem-audit`; the live app was restarted on it for U5.
  *Depends on:* nothing.
- *Files:* the branch as it stands. Its spec edit merges cleanly.
- *Done when:* merged into `master`; `pytest`, `pytest -m risk` and `mypy` pass
  on the merge; the live app is restarted on it.

**U2. Re-link stored articles.**
- *Status:* ~~not started~~ **merged in PR #14** (`91ab671`, 2026-11-11).
  The re-link ran against the live database on 2026-11-10 with the app
  stopped: **92 twin links added, 198 linked in all**, no rows otherwise
  changed; a dry run before and a run after on merged code both found
  nothing more. In 66 of the 92 pairs the late Finnhub copy is the canonical
  row, which raised Q24b. *Depends on:* U1.
- *Files:* `corollary/data/news/ingest.py`, `scripts/relink_articles.py`, tests.
- *Done when:* R2's tests pass, and the script has run against the live
  database and reported its count.

**U3. The five-session floor.**
- *Status:* ~~not started~~ **merged in PR #13** (migration `0017`).
  *Depends on:* U1 (it shares `audit.py` and `audit_runner.py`).
- *Files:* `audit.py`, `audit_runner.py`, `db/models.py`, a migration
  (`sentiment_accuracy.sessions`), `api/routes/settings.py`, `api/schemas.py`,
  `web/src/pages/Settings.tsx`, `web/src/lib/settings.ts`, tests.
- *Done when:* R3's tests pass and Settings shows sessions.

**U4. The void tool.**
- *Status:* ~~not started~~ **merged in PR #13** (migration `0018`). Q24c's
  exact `changed_at` restore is on `phase3-rem-grading` (`29711a7`), not yet
  merged. *Depends on:* U3 (shared readers and the migration chain).
- *Files:* `corollary/data/news/audit_void.py`, `scripts/void_audit_run.py`, a
  migration (`audit_run` void columns, `label_outcome_voided`, the route rows,
  the audit-log CHECK if needed), `api/routes/settings.py`,
  `web/src/lib/notifications.ts`, `web/src/lib/types.ts`, tests.
- *Done when:* R4's tests pass, and a voided run is excluded everywhere R4 names.

**U5. Void run 2 and re-grade (R5). Operational, launched by the owner.**
- *Status:* ~~not started~~ **done 2026-11-11.** Run 2 was voided at
  00:59:52Z (348 outcomes archived; Massive `demoted → unaudited`, rules
  `active → unaudited`; `sentiment_audit_voided` reached the bell and
  Discord). Run 3 was the re-grade, by the start-up catch-up rather than a
  manual `POST` (R5's note). Both sources stayed `unaudited`, at **3**
  (Massive) and **4** (rules) sessions, as R5 expected.
  *Status note, 2026-11-16:* on **2026-11-15 03:52 UTC, run 4** ran: the
  Saturday start-up catch-up, because the 10:00 ET slot was missed while the
  machine was off or asleep. It moved **rules to `active`** (7 sessions) and
  **Massive to `demoted`** (6 sessions). It graded under the **pre-Q24
  rules**, on `master` without Q24a or Q24b. **A re-grade under Q24 is
  planned once the grading branch deploys:** void run 4, then run 3 (newest
  first, since the tool voids only the latest standing run), relink, restart.
  *Depends on:* U1 to U4 merged and the app restarted.
- *Files:* none. The spec's statuses are updated once it is done.
- *Done when:*
  - run 2 is voided
  - `sentiment_audit_voided` is delivered to the bell and Discord
  - the manual run's row is written
  - each source's status matches R5's stated expectation, or the run's
    `sessions` figures explain why not

**U6. Economic first prints.**
- *Status:* ~~not started~~ **merged in PR #15** (`97f5046`), with Q24d.
  Deployed 2026-11-14: the live database is at `0019`, and first prints are
  filling. *Live done-when checks* (2026-11-16):
  - **Every past release in the stored window is `filled` or states why
    not: met.** All seven releases from Nov 2 to Nov 13 qualify. Six are
    `filled` (TTLCONS, JTSJOL, BOPGSTB, PAYEMS, CPIAUCSL, PPIFIS). Existing
    Home Sales is `unavailable`, with ALFRED's refusal as its reason.
  - **The dates refresh leaves stored values alone: met in the live
    database.** The refresh ran at 2026-11-16 07:12 UTC, after the values
    were stored at 2026-11-14 06:09 UTC. All six values are intact with
    their `fetched_at` unchanged, and `calendar_event.prior`/`.actual` stay
    NULL on every economic row. That refresh rewrote only three December
    rows, none of which held a value. So the case of rewriting a row that
    holds a value is shown by the tests alone, not yet live.
  - **The next release after deploy fills on its day: pending.** The first
    is 2026-11-17 at 08:30 ET (Retail Sales, Import and Export Prices), then
    G.17 at 09:15 ET.

  *Depends on:* nothing. It
  runs **in parallel** with U1 to U4.
- *Pre-flight:* a keyed probe through `--env-file .env` records vintage
  fixtures for `CPIAUCSL`, `PAYEMS`, `BOPGSTB` and `EXHOSLUSM495S`, and
  settles the `realtime_start` field question in *Not verified* before R7's
  rule is coded.
- *Files:* `data/providers/fred.py`, `data/econ_values.py`,
  `data/seeds/econ_release_series.csv`, `data/seeds/calendar_seeds.py`, a
  migration (`econ_release_value`), `engine/scheduler.py`,
  `api/routes/calendar.py`, `api/schemas.py`, the calendar panel, tests.
- *Done when:*
  - every past release in the stored window is `filled` or states why not
  - the next release after deploy fills on its day
  - the dates refresh leaves stored values alone, in test and in the live
    database

**U7. Doc amendments.**
- *Status:* ~~not started~~ **applied 2026-11-16 on `phase3-rem-grading`**,
  to land with Q24a–c. Every item under *Doc amendments* is applied; none
  struck. *Depends on:* U1 to U6.
- *Files:* the parent spec, `PRD.md`, `CLAUDE.md`. *(Amended 2026-11-16: and
  this spec, for its statuses and as-built notes.)*
- *Done when:* every item under *Doc amendments* is applied, or struck with a
  reason.

**Can start now, in parallel:** U1 and U6. After U1, U2 and U3 in parallel.
U4 after U3. U5 after U2 and U4. U7 last.

---

## Out of scope

- **Steps 8 and 9.** Still paused under Q20.
- **Hand verification of the central-bank seeds** (Q23d).
- **A fetch-lag cap on grading** (P2). It is the owner's to ask for.
- **A per-session share cap** (R3's known limit).
- **Any UI control that voids, demotes or re-promotes** (R4, parent Q6).
- **Re-grading any run but run 2.** Run 1 graded nothing. *(Amended
  2026-11-16: Q24a and Q24b change what is graded, so voiding runs 4 and 3
  and re-grading under Q24 is now planned. See U5's status note.)*
- **Consensus figures for economic releases** (parent Q3: not bought).
- **Releases before the stored calendar window.** No ALFRED backfill.
- **Any Phase 4 consumer** of the sentiment flag. How the scanner reads it
  stays the parent spec's carried obligation.
- **No order path of any kind.**
