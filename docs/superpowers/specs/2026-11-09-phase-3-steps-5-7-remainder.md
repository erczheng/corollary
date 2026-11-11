# Phase 3, steps 5–7: what is left

**Date:** 2026-11-09
**Status:** Draft. The owner decisions it rests on were taken on 2026-11-09 and
are recorded under *Owner decisions* (Q23). Nothing here is blocked on a
question.
**Parent spec:** `2026-09-23-phase-3-context-design.md` (steps 5, 6 and 7, and
Q22). This spec amends that spec's decision 13 and Q6, and the parent gains a
Q23 entry pointing here, in the same commit.
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
- **How many stored Motley Fool duplicates exist beyond Nov 2–3.** R2's
  re-link reports the count.

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

**The fetch.** `fred/series/observations` with `series_id`,
`realtime_start = realtime_end = release_date`, `file_type=json`, and an
`observation_start` far enough back to hold three observations (four months
for monthly series, a year for quarterly). Because this is the vintage, a
fetch weeks late still returns exactly what was known on release day.

**Which observation.** The release's observation is the latest-dated one
**new or revised on `release_date`**, by its `realtime_start` (see *Not
verified* for the fallback). If there is none, FRED has not posted the release
yet. The **prior** is the observation immediately before it **in the same
vintage**, so a revision published that day is reflected in it.

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

### API

- **`GET /api/calendar`:** each `economic` row gains `unitLabel`, `valueState`
  (`upcoming | awaiting | filled | unavailable`) and `valueNote` (*"computed
  from FRED's levels: CPIAUCSL"*, or the reason it is unavailable).
  `prior`/`actual` come from `econ_release_value`. The blanket *"Prior and
  actual for economic releases are not filled yet"* reason string is removed.
  `consensus` stays `"unavailable"` (parent Q3).
- **`GET /api/settings/sentiment`:** as R9.

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
mypy corollary`, and `npm run typecheck` and `npm run test` in `web/`.

---

## Doc amendments

- **Parent spec** — the first one is made in this spec's own commit; the rest
  land in U7:
  - **Q23**, pointing here.
  - **Decision 13's eligibility** gains the session floor (R3).
  - **Q6's** "only writer" is amended by R4.
  - **Step 5's status:** the mean-versus-median question is closed (P1).
  - **Step 6's status:** run 2 happened, and was voided by Q23a. "The run" is
    met by the re-grade run.
  - **Step 7's status:** actuals come through this spec.
  - **Migration numbers:** steps 5–7 read their final numbers (`0013`, `0014`,
    `0015`), no longer "provisional". Step 7's text still says `0013`.
- **PRD §10:** the notification table gains `sentiment_audit_voided`.
- **PRD §8.3:** economic releases show the first print, computed from FRED's
  levels.
- **CLAUDE.md:** the `alembic upgrade head` comment says *"0004 is head"*. It
  is stale (head `0016` today) and will be again after this spec.

---

## Order of work

Each unit gives **Status**, **Depends on**, **Files** and **Done when**, like
the parent's list.

**U1. Merge `phase3-fixes`.**
- *Status:* not started. *Depends on:* nothing.
- *Files:* the branch as it stands. Its spec edit merges cleanly.
- *Done when:* merged into `master`; `pytest`, `pytest -m risk` and `mypy` pass
  on the merge; the live app is restarted on it.

**U2. Re-link stored articles.**
- *Status:* not started. *Depends on:* U1.
- *Files:* `corollary/data/news/ingest.py`, `scripts/relink_articles.py`, tests.
- *Done when:* R2's tests pass, and the script has run against the live
  database and reported its count.

**U3. The five-session floor.**
- *Status:* not started. *Depends on:* U1 (it shares `audit.py` and
  `audit_runner.py`).
- *Files:* `audit.py`, `audit_runner.py`, `db/models.py`, a migration
  (`sentiment_accuracy.sessions`), `api/routes/settings.py`, `api/schemas.py`,
  `web/src/pages/Settings.tsx`, `web/src/lib/settings.ts`, tests.
- *Done when:* R3's tests pass and Settings shows sessions.

**U4. The void tool.**
- *Status:* not started. *Depends on:* U3 (shared readers and the migration
  chain).
- *Files:* `corollary/data/news/audit_void.py`, `scripts/void_audit_run.py`, a
  migration (`audit_run` void columns, `label_outcome_voided`, the route rows,
  the audit-log CHECK if needed), `api/routes/settings.py`,
  `web/src/lib/notifications.ts`, `web/src/lib/types.ts`, tests.
- *Done when:* R4's tests pass, and a voided run is excluded everywhere R4 names.

**U5. Void run 2 and re-grade (R5). Operational, launched by the owner.**
- *Status:* not started. *Depends on:* U1 to U4 merged and the app restarted.
- *Files:* none. The spec's statuses are updated once it is done.
- *Done when:*
  - run 2 is voided
  - `sentiment_audit_voided` is delivered to the bell and Discord
  - the manual run's row is written
  - each source's status matches R5's stated expectation, or the run's
    `sessions` figures explain why not

**U6. Economic first prints.**
- *Status:* not started. *Depends on:* nothing. It runs **in parallel** with
  U1 to U4.
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
- *Status:* not started. *Depends on:* U1 to U6.
- *Files:* the parent spec, `PRD.md`, `CLAUDE.md`.
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
- **Re-grading any run but run 2.** Run 1 graded nothing.
- **Consensus figures for economic releases** (parent Q3: not bought).
- **Releases before the stored calendar window.** No ALFRED backfill.
- **Any Phase 4 consumer** of the sentiment flag. How the scanner reads it
  stays the parent spec's carried obligation.
- **No order path of any kind.**
