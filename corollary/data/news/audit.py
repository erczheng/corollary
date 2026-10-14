"""The sentiment self-audit's grading -- pure (Phase 3 step 6, decision 12).

The design's layout reads "``audit.py`` -- grading and transitions, pure, plus
a runner". This module is the **grading** half (unit 6.2). Transitions and
accuracy are unit 6.3; the runner that fetches bars and writes
``label_outcome`` rows is unit 6.4. Nothing here does I/O: no session, no
HTTP, no clock, no scheduler, and no vendor import. The runner drives it in
two calls:

1. :func:`plan_audit` -- given the labels to consider, the watch universe,
   the tradeability verdicts, the ``(label_id, window)`` keys already written
   and the run instant, decides what is graded, what is recorded as skipped,
   and which SIP ``1Min`` bars (:class:`BarRequest`, SPY included) the
   grading needs;
2. :func:`grade` -- given the plan and those bars, returns every
   :class:`LabelOutcomeRecord` to write, plus the keys still waiting on bars.

Decision 12, as the spec states it
----------------------------------

* **What is graded.** Every directional label on a ticker in the watch
  universe, uncapped; plus *discovery* labels -- directional labels on
  off-watch tickers that pass decision 21's tradeability filter -- for at most
  :data:`DISCOVERY_TICKER_CAP` tickers a run, taken in order of that run's
  directional label count, descending, ties broken by symbol.
* **Skipped labels** get a row with a ``skip_reason``
  (``off_watch_untradeable`` or ``discovery_cap``) and no prices. ``neutral``
  labels are graded for the record and excluded from accuracy.
* **What is correct.** The label's sign matches the sign of the ticker's
  return in excess of SPY's over the same window. ``MARKET`` labels grade
  against SPY's raw return, stored in ``benchmark_return`` *and*
  ``excess_return`` (unit 6.1's convention).
* **The reference price** is the last SIP trade at or before publication if
  published during the regular session, otherwise the next regular-session
  open. SPY's reference is taken at the same instant.
* **The windows are in regular-session minutes**: 1h is the reference plus
  60, 1d plus 390, from :func:`corollary.calendars.add_session_minutes`.
  Half-days are real.

Decisions the spec leaves open -- for the owner's review
--------------------------------------------------------

Each was set by the orchestrator for unit 6.2 unless marked otherwise, and
each is a parent-session assumption the owner can override.

1. **The reference instant is ``max(published_at, labeled_at)``.** Corollary
   must not be credited with a move that happened before it saw the story. A
   pre-0013 article re-fetched in overlap is labelled late; for a label seen
   live the two are near-equal and this is the spec's rule.
   **Amended (fifth pass): ``max`` holds unless ``published_at`` is more
   than :data:`VENDOR_STAMP_TOLERANCE` after ``labeled_at``**, and then the
   reference is ``labeled_at``. ``labeled_at`` is the *poll's* start
   (``label_stored`` is given the poll's ``now``, read before the fetch), so
   ``published_at`` can honestly fall a few seconds after it -- a story
   arriving on a later page of the same fetch. Using ``labeled_at`` there
   would put the reference before publication and credit the label with
   the minute before the story existed, which is the move decision 1
   exists to refuse. Far past ``labeled_at`` (a 2062 typo) the vendor stamp
   is an error, and ``labeled_at`` is when the story demonstrably existed.
   Decision 18's per-label catch covers any instant past the calendar's
   schedule either way. Flagged for the owner.
2. **In session, past the first minute, the reference price is the close of
   the last completed 1Min bar** -- ``bar.at + 1 minute <= instant`` --
   within that session. No look-ahead: a bar stamped 10:15 is not known
   until 10:16. With no completed bar there, there is no reference price -- a
   later bar would be a look-ahead. Before that, the reference instant is in
   ``[open, open + 1 minute)`` and the **fallback** applies (amended in the
   fourth pass, so that a stored ``reference_at`` is never earlier than the
   reference instant):

   * **Out of session** the reference instant moves to the next session's
     open, and **only at the open exactly** (that, or a label stamped at the
     open to the microsecond) is the open-stamped bar's *open* the price,
     with ``reference_at`` the open.
   * **Inside the first minute** (open < instant < open + 1 minute) the open
     print is from before the label was seen, so the reference waits for the
     first completed bar: the open-stamped bar's *close*, with
     ``reference_at`` = open + 1 minute.
   * Either way, with no open-stamped bar, the price is the session's first
     bar's open and ``reference_at`` its stamp -- but only when that stamp is
     within :data:`FALLBACK_MAX_LAG` of the open (and before the window's
     end). Later than that is no reference price: a label at 18:00 whose
     ticker first trades at 15:59 next session would otherwise get a
     one-minute "1d" window. The window waits and becomes
     ``price_unavailable`` after :data:`PRICE_UNAVAILABLE_AFTER`, as any
     missing price does.

   The record's ``reference_at`` is therefore always the instant the price
   is actually from. SPY is priced at that same instant by the same rules:
   the close of its last bar completed by then, or, at the open itself, the
   open of its bar stamped exactly at the open. SPY's first bar a minute
   late is a different instant, so the window waits; a ``MARKET`` or SPY
   label's own SPY series is held to :data:`FALLBACK_MAX_LAG` directly. The
   window's end is still measured from the label's reference instant
   (:func:`reference_at`), so a halted open makes the window honestly
   shorter rather than moving ``outcome_at``.
3. **The outcome price** is the close of the last bar completed by the
   window's end, within the session the window ends in. A window ending
   inside that session's first minute has no completed bar, so its price is
   the open of the bar stamped exactly at the open -- decision 2's mirror. A
   first bar stamped later is not the price at the window's end: no price.
4. **Returns** are ``outcome / reference - 1`` in ``Decimal`` at 28
   significant digits, each quantized to :data:`RETURN_EXPONENT` (twelve
   places, ``ROUND_HALF_EVEN``) **before** the subtraction, which is then
   exact. Every stored return therefore has twelve places, and the widest
   any realistic price pair can produce sits far inside ``Money``'s 40
   characters; a wider one is never written (decision 16). A quantized
   negative zero is stored as ``0E-12``, never ``-0E-12``, so no row reads
   "-0".
5. **An exact-zero excess on a directional label is incorrect.** The label
   claimed a move that did not happen. A directional graded row is never
   left with ``correct`` NULL -- unit 6.1 documents that it would drop out of
   every count. Neutral labels carry ``correct`` NULL.
6. **``price_unavailable``** is written only once the window's end is more
   than :data:`PRICE_UNAVAILABLE_AFTER` before the run and the bars are still
   missing. Sooner, the (label, window) produces no record and stays pending,
   so a transient vendor gap is retried rather than recorded.
7. **Maturity.** A (label, window) is gradable when its end plus
   :data:`MATURITY_DELAY` is at or before the run instant -- SIP historical
   is free past fifteen minutes. The two windows are independent records.
8. **Watch versus discovery** is classified by the watch universe passed in
   at run time; ``sentiment_label`` does not store membership.
9. **Tradeability** is a caller-supplied verdict map, ticker to
   ``TradeabilityResult.passes`` as ``tradeability.py`` computes it.
10. **The discovery cap** counts per run, over the labels considered this
    run. A capped ticker's labels get ``discovery_cap`` for every unwritten
    window -- both, on a label seen for the first time.
11. **Idempotency.** The caller passes the keys already written; nothing here
    re-emits one, so re-running a completed audit is a no-op.

Six more, taken in this unit where neither the spec nor the orchestrator
said (flagged in the unit's report; 14 and 16 were revised by the
orchestrator in the audit fix pass):

12. **Neutral off-watch labels ride with their ticker.** The discovery set is
    directional labels, but ``sentiment_accuracy`` reports a neutral count
    per scope, discovery included. So every off-watch tradeable ticker with
    a considered label is a candidate, ranked by ``(-directional count,
    symbol)``: a neutral-only ticker ranks after every directional one,
    never displaces one, and is graded only when the cap has room. A
    candidate's neutral labels are graded or capped with its directional
    ones. ``discovery_cap`` therefore appears only when the cap binds.
13. **A label on SPY itself grades raw, like ``MARKET``.** SPY in excess of
    SPY is zero by construction, which rule 5 would score wrong every time.
14. **An off-watch ticker missing from the verdict map stays pending** -- no
    record -- and is named in :attr:`AuditPlan.unverdicted_tickers`, with
    its label count in :attr:`AuditPlan.unverdicted_label_counts`, for the
    runner to surface. It waits until the run is more than
    :data:`PRICE_UNAVAILABLE_AFTER` past the end of its last unwritten
    window, and then every unwritten window is written
    ``off_watch_untradeable``: failing closed, since it was never shown to
    pass decision 21's filter, and a label left pending forever is reloaded
    by every run.
15. **``MARKET`` is always watch**, whatever universe is passed: the spec
    lists it as a member, and it can never pass a tradeability filter, so a
    universe that omitted it would skip every market label as untradeable.
16. **Vendor-data faults quarantine one (symbol, session), never the run.**
    Bars are indexed per (symbol, session date). Extended-hours bars are
    ignored entirely -- never read, never validated, never reported.
    Identical duplicates collapse to one. A non-finite or non-positive open
    or close, or two *different* bars for one symbol and minute, quarantine
    that (symbol, session): every lookup there sees no bars -- SPY's
    included -- and the fault is reported once in
    :attr:`GradeResult.bar_faults`. A window that needed the session waits
    like any other missing price and becomes ``price_unavailable`` after
    :data:`PRICE_UNAVAILABLE_AFTER`. A return too wide for ``Money`` is
    handled the same way, its fault named against the reference session.
    The one input that still raises is a **naive bar stamp**: the provider
    decodes every stamp aware (``corollary.wire``), so a naive one is a
    defect in this codebase, not vendor data.
17. **A considered label** is a label with at least one matured, unwritten
    window; a label with none is not looked at, so its ticker neither takes
    a cap slot nor contributes to a ranking.
18. **An unschedulable label never stops the run** (fourth pass, set by the
    orchestrator). When a label's reference or a window's end falls past the
    calendar's published schedule, :mod:`corollary.calendars` raises
    ``ValueError``; :func:`plan_audit` catches it **per label**, writes no
    record for it, and names it in :attr:`AuditPlan.unschedulable_label_ids`
    for the runner to surface. A label with every window already written is
    not scheduled at all, so it is never named. A naive instant is still
    refused loudly -- awareness is checked before the calendar is asked.

A note for unit 6.4 (the runner)
--------------------------------

``corollary.wire`` refuses a non-finite number at decode (``as_decimal`` and
``as_int``, wire.py:574 and :607), raising ``WireFormatError`` inside the
provider -- so a NaN bar never reaches :func:`grade` as a ``Bar`` for
decision 16 to quarantine. The runner must therefore **catch
``WireFormatError`` per** :class:`BarRequest` and treat it as a bar fault for
that (symbol, session): grade without those bars, so the windows that need
them wait and become ``price_unavailable`` in time, and surface the fault
beside :attr:`GradeResult.bar_faults`. Uncaught, one NaN bar fails the whole
fetch and with it every other ticker's grade.

Bars are assumed ``1Min`` and stamped at their **opening** minute, the
provider's convention (:class:`~corollary.data.providers.interface.Bar`).
"""

from bisect import bisect_right
from collections import defaultdict
from collections.abc import Collection, Iterable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Context, Decimal, Inexact, InvalidOperation
from enum import StrEnum
from types import MappingProxyType
from typing import Final

from corollary.calendars import (
    NYSE_TZ,
    add_session_minutes,
    nyse_next_open,
    nyse_session_close,
    nyse_session_containing,
    nyse_session_open,
)
from corollary.data.news.labels import Direction
from corollary.data.news.watchlist import MARKET_TICKER
from corollary.data.providers.interface import Bar

__all__ = [
    "BENCHMARK_SYMBOL",
    "DISCOVERY_TICKER_CAP",
    "FALLBACK_MAX_LAG",
    "MATURITY_DELAY",
    "PRICE_UNAVAILABLE_AFTER",
    "RETURN_EXPONENT",
    "VENDOR_STAMP_TOLERANCE",
    "WINDOW_SESSION_MINUTES",
    "AuditLabel",
    "AuditPlan",
    "BarFault",
    "BarRequest",
    "GradeResult",
    "GradingWindow",
    "LabelOutcomeRecord",
    "Scope",
    "SkipReason",
    "grade",
    "plan_audit",
    "reference_at",
    "reference_instant",
    "window_end",
]

#: The benchmark every non-market label's return is taken in excess of (Q5).
BENCHMARK_SYMBOL: Final = "SPY"

#: Decision 12: at most this many off-watch tickers graded per run.
#: *(Parent-session assumption the owner can override.)*
DISCOVERY_TICKER_CAP: Final = 250

#: Decision 12's windows, in regular-session minutes from the reference.
#: Keys in ``SENTIMENT_WINDOWS`` order; a test pins the two together.
WINDOW_SESSION_MINUTES: Final[Mapping[str, int]] = MappingProxyType({"1h": 60, "1d": 390})

#: Decision 2's fallback bound: a reference price from a session's first bar
#: is used only when that bar is stamped at most this long after the open.
#: *(Set by the orchestrator; the owner can override.)*
FALLBACK_MAX_LAG: Final = timedelta(minutes=30)

#: Decision 1 (fifth pass): how far ``published_at`` may honestly sit after
#: ``labeled_at`` -- the length of one poll's fetch, rate-limited pages and
#: all -- before the vendor's stamp is treated as an error and ``labeled_at``
#: is the reference instead. *(Orchestrator assumption the owner can
#: override.)*
VENDOR_STAMP_TOLERANCE: Final = timedelta(minutes=15)

#: Orchestrator decision 7: a window is gradable this long after it ends.
#: SIP historical bars are free on Basic once they are fifteen minutes old.
MATURITY_DELAY: Final = timedelta(minutes=15)

#: Orchestrator decision 6: how long after a window's end missing bars are
#: retried before the window is recorded ``price_unavailable``.
PRICE_UNAVAILABLE_AFTER: Final = timedelta(days=7)

#: Orchestrator decision 4: every stored return has exactly twelve places.
RETURN_EXPONENT: Final = Decimal("1E-12")

#: ``Money``'s TEXT width (``corollary.db.types``); a wider return is refused.
_MONEY_TEXT_WIDTH: Final = 40

#: One ``1Min`` bar: a bar stamped ``t`` is complete at ``t + _BAR``.
_BAR: Final = timedelta(minutes=1)

#: The division's context, explicit so no caller's global context changes a
#: grade: 28 significant digits, half-even, and every signal that would mean
#: a nonsense input trapped.
_DIVISION: Final = Context(prec=28, rounding=ROUND_HALF_EVEN, traps=[InvalidOperation])

#: The quantize to twelve places. It rounds, so ``Inexact`` is not trapped;
#: the precision is wide enough that the coefficient always fits.
_QUANTIZE: Final = Context(prec=80, rounding=ROUND_HALF_EVEN, traps=[InvalidOperation])

#: The subtraction of two quantized returns, wide enough to be exact --
#: ``Inexact`` is trapped, so a rounded excess raises instead of being stored.
_EXACT: Final = Context(prec=80, rounding=ROUND_HALF_EVEN, traps=[InvalidOperation, Inexact])

_DIRECTIONAL: Final = frozenset({Direction.BULLISH, Direction.BEARISH})


class Scope(StrEnum):
    """Which graded set a label is in this run (decision 12's splits)."""

    WATCH = "watch"
    DISCOVERY = "discovery"


class SkipReason(StrEnum):
    """``label_outcome.skip_reason``; values equal ``LABEL_SKIP_REASONS``."""

    OFF_WATCH_UNTRADEABLE = "off_watch_untradeable"
    DISCOVERY_CAP = "discovery_cap"
    PRICE_UNAVAILABLE = "price_unavailable"


@dataclass(frozen=True, slots=True)
class AuditLabel:
    """The ``sentiment_label`` columns grading reads, plus the article's publication.

    ``published_at`` is the article's; ``labeled_at`` the label's. Both must
    be timezone-aware.
    """

    label_id: int
    ticker: str
    source: str
    direction: Direction
    published_at: datetime
    labeled_at: datetime


@dataclass(frozen=True, slots=True)
class GradingWindow:
    """One (label, window) to grade this run.

    ``symbol`` is the symbol whose bars are read: the ticker, or SPY when
    ``benchmark_only`` (a ``MARKET`` or SPY label, graded on SPY's raw
    return). ``reference_at`` is the instant the reference price is sought
    at, from :func:`reference_at`. The record's ``reference_at`` is the
    instant the price is actually from, and is never earlier than this one:
    equal to it past the first minute and at an out-of-session open, and on
    decision 2's other fallbacks later -- open + 1 minute inside the first
    minute, or the first bar's stamp. ``outcome_at`` is the window's end,
    where the outcome price is taken. Both UTC.
    """

    label: AuditLabel
    window: str
    scope: Scope
    symbol: str
    benchmark_only: bool
    reference_at: datetime
    outcome_at: datetime


@dataclass(frozen=True, slots=True)
class BarRequest:
    """``1Min`` SIP bars for ``symbol`` whose **opening stamp** is in ``[start, end)``.

    The runner fetches these and passes every bar to :func:`grade`. Each
    request lies inside **one session**: ``start`` is its open, and ``end``
    is the latest instant any window reads there -- the window's end in the
    session it ends in, and ``min(close, window end)`` in the reference
    session. So no request ever spans an overnight, and the last bar the
    grader can read in one is stamped before ``end``.
    """

    symbol: str
    start: datetime
    end: datetime


@dataclass(frozen=True, slots=True)
class LabelOutcomeRecord:
    """One ``label_outcome`` row, less ``audit_run_id`` (the runner's).

    ``scope`` is **not a column**: it is this run's classification, carried
    for unit 6.3's watch/discovery splits. ``None`` on the two membership
    skips, which belong to neither graded set.
    """

    label_id: int
    window: str
    scope: Scope | None
    reference_at: datetime | None
    reference_price: Decimal | None
    outcome_at: datetime | None
    outcome_price: Decimal | None
    benchmark_return: Decimal | None
    excess_return: Decimal | None
    correct: bool | None
    skip_reason: SkipReason | None


@dataclass(frozen=True, slots=True)
class AuditPlan:
    """What one run will grade, what it records as skipped, and the bars it needs.

    * ``skipped`` -- the membership skips, ready to write; they need no bars.
      :func:`grade` returns them too, so a runner that fetched nothing can
      still write these.
    * ``to_grade`` -- the matured, unwritten windows of graded labels.
    * ``bar_requests`` -- one per (symbol, session), sorted by
      ``(symbol, start)``.
    * ``discovery_capped_tickers`` -- for ``audit_run.detail``.
    * ``unverdicted_tickers`` -- off-watch tickers with no tradeability
      verdict among this run's considered labels (decision 14 above).
    * ``unverdicted_label_counts`` -- ``(ticker, count)`` for the same
      tickers, sorted by ticker: every considered label with no verdict,
      including any written ``off_watch_untradeable`` this run because its
      wait ran out.
    * ``unschedulable_label_ids`` -- sorted ids of labels with an unwritten
      window whose reference or window end is past the calendar's published
      schedule (decision 18). No record is written for them.
    """

    run_at: datetime
    skipped: tuple[LabelOutcomeRecord, ...]
    to_grade: tuple[GradingWindow, ...]
    bar_requests: tuple[BarRequest, ...]
    discovery_capped_tickers: tuple[str, ...]
    unverdicted_tickers: tuple[str, ...]
    unverdicted_label_counts: tuple[tuple[str, int], ...]
    unschedulable_label_ids: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class BarFault:
    """One (symbol, session) whose bars no lookup reads, and why (decision 16)."""

    symbol: str
    session_date: date
    reason: str


@dataclass(frozen=True, slots=True)
class GradeResult:
    """Every record to write, sorted by key, the keys still waiting on bars, and faults.

    ``awaiting_bars`` lists the windows with a missing price that are not yet
    :data:`PRICE_UNAVAILABLE_AFTER` old: no record, retried next run.
    ``bar_faults`` lists the quarantined (symbol, session) pairs, sorted, for
    the runner to log and surface.
    """

    records: tuple[LabelOutcomeRecord, ...]
    awaiting_bars: tuple[tuple[int, str], ...]
    bar_faults: tuple[BarFault, ...]


# --------------------------------------------------------------------- #
# Instants
# --------------------------------------------------------------------- #


def _require_aware(instant: datetime, what: str) -> None:
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError(f"{what} must be timezone-aware, got {instant!r}")


def reference_instant(label: AuditLabel) -> datetime:
    """Decision 1 as amended, in UTC.

    ``max(published_at, labeled_at)`` -- never before the story was
    published, never before Corollary wrote the label -- except when
    ``published_at`` is more than :data:`VENDOR_STAMP_TOLERANCE` after
    ``labeled_at``: no poll fetches a story that far ahead of its own start,
    so the vendor's stamp is wrong and ``labeled_at`` is used. Both must be
    aware.
    """
    _require_aware(label.published_at, "published_at")
    _require_aware(label.labeled_at, "labeled_at")
    published = label.published_at.astimezone(timezone.utc)
    labeled = label.labeled_at.astimezone(timezone.utc)
    if published - labeled > VENDOR_STAMP_TOLERANCE:
        return labeled
    return max(published, labeled)


def reference_at(label: AuditLabel) -> datetime:
    """The instant the reference price is taken at: in session, the reference
    instant itself; out of session, the next session's open."""
    instant = reference_instant(label)
    if nyse_session_containing(instant) is not None:
        return instant
    return nyse_next_open(instant)


def window_end(reference: datetime, window: str) -> datetime:
    """``reference`` plus the window's session minutes (decision 12)."""
    _require_aware(reference, "reference")
    return add_session_minutes(reference, WINDOW_SESSION_MINUTES[window])


# --------------------------------------------------------------------- #
# The plan
# --------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Considered:
    label: AuditLabel
    reference_at: datetime
    #: Unwritten windows whose end plus the delay has passed, with that end.
    matured: tuple[tuple[str, datetime], ...]
    #: Every unwritten window, matured or not, with its end -- what a
    #: membership skip covers.
    unwritten: tuple[tuple[str, datetime], ...]


def _skip(label_id: int, window: str, reason: SkipReason, scope: Scope | None) -> LabelOutcomeRecord:
    return LabelOutcomeRecord(
        label_id=label_id,
        window=window,
        scope=scope,
        reference_at=None,
        reference_price=None,
        outcome_at=None,
        outcome_price=None,
        benchmark_return=None,
        excess_return=None,
        correct=None,
        skip_reason=reason,
    )


def _is_benchmark_only(ticker: str) -> bool:
    return ticker in (MARKET_TICKER, BENCHMARK_SYMBOL)


def _grading_windows(item: _Considered, scope: Scope) -> list[GradingWindow]:
    benchmark_only = _is_benchmark_only(item.label.ticker)
    symbol = BENCHMARK_SYMBOL if benchmark_only else item.label.ticker
    return [
        GradingWindow(
            label=item.label,
            window=window,
            scope=scope,
            symbol=symbol,
            benchmark_only=benchmark_only,
            reference_at=item.reference_at,
            outcome_at=end,
        )
        for window, end in item.matured
    ]


def _session_bounds(day: date) -> tuple[datetime, datetime]:
    session_open, session_close = nyse_session_open(day), nyse_session_close(day)
    if session_open is None or session_close is None:
        raise AssertionError(f"{day} is not a session")
    return session_open, session_close


def _reference_session(reference: datetime) -> date:
    """The session ``reference`` is in -- always one, by :func:`reference_at`."""
    day = nyse_session_containing(reference)
    if day is None:
        raise AssertionError(f"reference {reference} is outside every session")
    return day


def _outcome_session(outcome: datetime) -> date:
    """The session a window ends in: ``(open, close]``, so a close is its own day's."""
    day = outcome.astimezone(NYSE_TZ).date()
    session_open, session_close = _session_bounds(day)
    if not session_open < outcome <= session_close:
        raise AssertionError(f"window end {outcome} is outside its session")
    return day


def _bar_requests(to_grade: Iterable[GradingWindow]) -> tuple[BarRequest, ...]:
    """One range per (symbol, session): its open to the latest instant read there.

    Every range a window needs in one session starts at that session's open,
    so merging within a session is taking the latest end. Sessions are never
    merged with each other -- that would request the overnight.
    """
    ends: dict[tuple[str, date], datetime] = {}

    def need(symbol: str, day: date, end: datetime) -> None:
        key = (symbol, day)
        if key not in ends or end > ends[key]:
            ends[key] = end

    for item in to_grade:
        reference_day = _reference_session(item.reference_at)
        _, reference_close = _session_bounds(reference_day)
        outcome_day = _outcome_session(item.outcome_at)
        symbols = [BENCHMARK_SYMBOL] if item.benchmark_only else [item.symbol, BENCHMARK_SYMBOL]
        for symbol in symbols:
            need(symbol, reference_day, min(reference_close, item.outcome_at))
            need(symbol, outcome_day, item.outcome_at)
    return tuple(
        BarRequest(symbol, _session_bounds(day)[0], ends[(symbol, day)])
        for symbol, day in sorted(ends)
    )


def plan_audit(
    labels: Iterable[AuditLabel],
    *,
    watch_universe: Collection[str],
    tradeable: Mapping[str, bool],
    written: Collection[tuple[int, str]],
    run_at: datetime,
) -> AuditPlan:
    """Classify the labels, apply the discovery cap, and list the bars needed.

    ``labels`` are the candidates the runner loaded -- at least every label
    with an unwritten window; extra ones cost nothing. ``tradeable`` maps an
    off-watch ticker to its decision 21 verdict. ``written`` holds the
    ``(label_id, window)`` keys already in ``label_outcome``. The result is
    independent of the order of every input.
    """
    _require_aware(run_at, "run_at")
    run_at = run_at.astimezone(timezone.utc)
    universe = frozenset(watch_universe) | {MARKET_TICKER}
    done = frozenset(written)

    by_id: dict[int, AuditLabel] = {}
    for item in labels:
        if item.label_id in by_id:
            raise ValueError(f"label_id {item.label_id} appears twice in one audit")
        by_id[item.label_id] = item

    considered: list[_Considered] = []
    unschedulable: list[int] = []
    for label_id in sorted(by_id):
        item = by_id[label_id]
        pending = [w for w in WINDOW_SESSION_MINUTES if (label_id, w) not in done]
        if not pending:
            continue
        # Awareness first, outside the catch: a naive instant is a defect
        # here and still raises. What the catch can then see is the
        # calendar's past-the-published-schedule refusal (decision 18).
        reference_instant(item)
        try:
            reference = reference_at(item)
            unwritten = tuple((w, window_end(reference, w)) for w in pending)
        except ValueError:
            unschedulable.append(label_id)
            continue
        matured = tuple((w, end) for w, end in unwritten if end + MATURITY_DELAY <= run_at)
        if matured:
            considered.append(_Considered(item, reference, matured, unwritten))

    skipped: list[LabelOutcomeRecord] = []
    to_grade: list[GradingWindow] = []
    unverdicted: dict[str, int] = {}
    candidates: dict[str, list[_Considered]] = defaultdict(list)

    def untradeable(entry: _Considered) -> None:
        skipped.extend(
            _skip(entry.label.label_id, w, SkipReason.OFF_WATCH_UNTRADEABLE, None)
            for w, _ in entry.unwritten
        )

    for entry in considered:
        ticker = entry.label.ticker
        if ticker in universe:
            to_grade.extend(_grading_windows(entry, Scope.WATCH))
            continue
        verdict = tradeable.get(ticker)
        if verdict is None:
            # Decision 14: pending while a verdict could still arrive, then
            # fail closed once even its last window is past retrying.
            unverdicted[ticker] = unverdicted.get(ticker, 0) + 1
            last_end = max(end for _, end in entry.unwritten)
            if run_at - last_end > PRICE_UNAVAILABLE_AFTER:
                untradeable(entry)
        elif verdict:
            candidates[ticker].append(entry)
        else:
            untradeable(entry)

    def directional_count(ticker: str) -> int:
        return sum(1 for e in candidates[ticker] if e.label.direction in _DIRECTIONAL)

    ranked = sorted(candidates, key=lambda ticker: (-directional_count(ticker), ticker))
    selected, capped = ranked[:DISCOVERY_TICKER_CAP], ranked[DISCOVERY_TICKER_CAP:]
    for ticker in selected:
        for entry in candidates[ticker]:
            to_grade.extend(_grading_windows(entry, Scope.DISCOVERY))
    for ticker in capped:
        for entry in candidates[ticker]:
            skipped.extend(
                _skip(entry.label.label_id, w, SkipReason.DISCOVERY_CAP, None)
                for w, _ in entry.unwritten
            )

    to_grade.sort(key=lambda g: (g.label.label_id, g.window))
    skipped.sort(key=lambda r: (r.label_id, r.window))
    return AuditPlan(
        run_at=run_at,
        skipped=tuple(skipped),
        to_grade=tuple(to_grade),
        bar_requests=_bar_requests(to_grade),
        discovery_capped_tickers=tuple(sorted(capped)),
        unverdicted_tickers=tuple(sorted(unverdicted)),
        unverdicted_label_counts=tuple(sorted(unverdicted.items())),
        unschedulable_label_ids=tuple(unschedulable),
    )


# --------------------------------------------------------------------- #
# Prices
# --------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class _Series:
    """One (symbol, session)'s bars, sorted by stamp, with the stamps alongside for bisect."""

    stamps: tuple[datetime, ...]
    bars: tuple[Bar, ...]


def _validate(bars: Iterable[Bar]) -> _Series | str:
    """One (symbol, session)'s bars as a series, or the reason they are quarantined.

    Checked stamp by stamp in time order, and within a stamp as a set, so
    the reason reported does not depend on the order the bars arrived in.
    """
    by_stamp: dict[datetime, list[Bar]] = defaultdict(list)
    for item in bars:
        by_stamp[item.at.astimezone(timezone.utc)].append(item)
    stamps: list[datetime] = []
    kept: list[Bar] = []
    for stamp in sorted(by_stamp):
        group = by_stamp[stamp]
        when = stamp.isoformat()
        if any(not (b.open.is_finite() and b.close.is_finite()) for b in group):
            return f"non-finite open or close at {when}"
        if any(b.open <= 0 or b.close <= 0 for b in group):
            return f"non-positive open or close at {when}"
        first = group[0]
        if any(other != first for other in group[1:]):
            return f"conflicting duplicate bars at {when}"
        stamps.append(stamp)
        kept.append(first)
    return _Series(tuple(stamps), tuple(kept))


def _index(bars: Iterable[Bar]) -> tuple[dict[tuple[str, date], _Series], tuple[BarFault, ...]]:
    """Decision 16: the regular-session bars per (symbol, session), less the quarantined."""
    grouped: dict[tuple[str, date], list[Bar]] = defaultdict(list)
    for item in bars:
        _require_aware(item.at, f"bar {item.symbol} at")
        day = nyse_session_containing(item.at)
        if day is None:  # extended hours, a weekend, a holiday: never read
            continue
        grouped[(item.symbol, day)].append(item)
    series: dict[tuple[str, date], _Series] = {}
    faults: list[BarFault] = []
    for key in sorted(grouped):
        try:
            checked = _validate(grouped[key])
        except (ValueError, TypeError, ArithmeticError) as exc:
            # A value that cannot even be compared -- a signalling NaN in a
            # column grading never reads, say -- is a defect in the vendor's
            # data, not a reason to lose every other ticker's grade.
            checked = f"unreadable bar values ({type(exc).__name__})"
        if isinstance(checked, str):
            faults.append(BarFault(key[0], key[1], checked))
        else:
            series[key] = checked
    return series, tuple(faults)


def _completed_close(series: _Series | None, instant: datetime) -> Decimal | None:
    """The close of the last bar completed by ``instant`` -- stamped ``t``, known at ``t + 1m``."""
    if series is None:
        return None
    index = bisect_right(series.stamps, instant - _BAR) - 1
    return series.bars[index].close if index >= 0 else None


def _open_print(series: _Series | None, session_open: datetime) -> Decimal | None:
    """The open of the bar stamped exactly at the session's open, if there is one."""
    if series is None or not series.stamps or series.stamps[0] != session_open:
        return None
    return series.bars[0].open


def _price_at(series: _Series | None, instant: datetime, session_open: datetime) -> Decimal | None:
    """The outcome price at ``instant`` within one session (decision 3).

    The close of the last bar completed by ``instant``. Inside the session's
    first minute none has completed, and the price is the open of a bar
    stamped exactly at the open -- known by then, so no look-ahead; any later
    first bar is a different instant, so no price.
    """
    price = _completed_close(series, instant)
    if price is None and instant < session_open + _BAR:
        return _open_print(series, session_open)
    return price


def _benchmark_reference(
    series: _Series | None, instant: datetime, session_open: datetime
) -> Decimal | None:
    """SPY at the instant the ticker's reference price is from (decision 2).

    The close of its last bar completed by then, or, at the open itself, its
    open print. Never the open print at any later instant: that would be a
    price from before the reference.
    """
    if instant == session_open:
        return _open_print(series, session_open)
    return _completed_close(series, instant)


def _reference(
    series: _Series | None, reference: datetime, outcome: datetime, session_open: datetime
) -> tuple[datetime, Decimal] | None:
    """The instant the reference price is from, and the price (decision 2).

    Past the first minute: ``reference`` itself and the last completed bar's
    close. Otherwise ``reference`` is in ``[open, open + 1 minute)``, and:

    * at the open exactly (an out-of-session label, or one stamped at the
      open to the microsecond), the open print, at the open;
    * inside the first minute, the open-stamped bar's close, at open + 1
      minute -- the open print predates the label;
    * with no open-stamped bar, the first bar's open at its stamp, provided
      it is within :data:`FALLBACK_MAX_LAG` of the open and before the
      window's end.

    Every answer's instant is at or after ``reference``.
    """
    if reference >= session_open + _BAR:
        price = _completed_close(series, reference)
        return None if price is None else (reference, price)
    if series is None or not series.stamps:
        return None
    first_at, first = series.stamps[0], series.bars[0]
    if first_at == session_open:
        if reference == session_open:
            return session_open, first.open
        return session_open + _BAR, first.close
    if first_at < reference:
        return None  # an off-minute stamp inside (open, reference): never earlier
    if first_at - session_open > FALLBACK_MAX_LAG or first_at >= outcome:
        return None
    return first_at, first.open


# --------------------------------------------------------------------- #
# Returns and the grade
# --------------------------------------------------------------------- #


def _unsigned(value: Decimal) -> Decimal:
    """``-0E-12`` as ``0E-12``: a stored zero never reads "-0" (decision 4)."""
    return value.copy_abs() if value.is_zero() else value


def _return(reference: Decimal, outcome: Decimal) -> Decimal:
    """``outcome / reference - 1``, at 28 digits, quantized to twelve places."""
    raw = _DIVISION.subtract(_DIVISION.divide(outcome, reference), Decimal(1))
    return _unsigned(raw.quantize(RETURN_EXPONENT, rounding=ROUND_HALF_EVEN, context=_QUANTIZE))


def _check_width(value: Decimal, what: str) -> Decimal:
    if len(format(value, "f")) > _MONEY_TEXT_WIDTH:
        raise ValueError(f"{what} {value} is wider than Money's {_MONEY_TEXT_WIDTH} characters")
    return value


def _unstorable(symbol: str, day: date, exc: Exception) -> BarFault:
    reason = f"a return from these prices cannot be stored ({type(exc).__name__})"
    return BarFault(symbol, day, reason)


def _returns(
    item: GradingWindow,
    reference_day: date,
    benchmark_prices: tuple[Decimal, Decimal],
    own_prices: tuple[Decimal, Decimal],
) -> tuple[Decimal, Decimal] | BarFault:
    """``(benchmark_return, excess_return)``, or the fault that stops them being stored."""
    try:
        benchmark = _check_width(_return(*benchmark_prices), "benchmark return")
    except (ValueError, ArithmeticError) as exc:
        return _unstorable(BENCHMARK_SYMBOL, reference_day, exc)
    if item.benchmark_only:
        return benchmark, benchmark
    try:
        excess = _unsigned(_EXACT.subtract(_return(*own_prices), benchmark))
        return benchmark, _check_width(excess, "excess return")
    except (ValueError, ArithmeticError) as exc:
        return _unstorable(item.symbol, reference_day, exc)


def _is_correct(direction: Direction, excess: Decimal) -> bool | None:
    """Decision 5 above: zero is incorrect; neutral is NULL."""
    if direction is Direction.BULLISH:
        return excess > 0
    if direction is Direction.BEARISH:
        return excess < 0
    return None


def grade(plan: AuditPlan, bars: Iterable[Bar]) -> GradeResult:
    """Grade the plan's windows on ``bars`` -- every bar the runner fetched.

    Returns the plan's membership skips, every graded record, and a
    ``price_unavailable`` record for each window with a missing price whose
    end is more than :data:`PRICE_UNAVAILABLE_AFTER` before the run; any
    other window with a missing price is in ``awaiting_bars``. Bars outside
    the requested ranges, and extended-hours bars, are harmless. Faulty
    vendor bars quarantine their (symbol, session) and are reported in
    ``bar_faults``; they never raise (decision 16).
    """
    series, index_faults = _index(bars)
    faults: set[BarFault] = set(index_faults)
    records: list[LabelOutcomeRecord] = list(plan.skipped)
    awaiting: list[tuple[int, str]] = []

    for item in plan.to_grade:
        key = (item.label.label_id, item.window)
        reference_day = _reference_session(item.reference_at)
        reference_open, _ = _session_bounds(reference_day)
        outcome_day = _outcome_session(item.outcome_at)
        outcome_open, _ = _session_bounds(outcome_day)

        reference = _reference(
            series.get((item.symbol, reference_day)),
            item.reference_at,
            item.outcome_at,
            reference_open,
        )
        outcome_price = _price_at(
            series.get((item.symbol, outcome_day)), item.outcome_at, outcome_open
        )
        spy_reference: Decimal | None
        if reference is None:
            spy_reference = None
        elif item.benchmark_only:
            spy_reference = reference[1]
        else:
            # Decision 2: SPY at the instant the ticker's price is from.
            spy_reference = _benchmark_reference(
                series.get((BENCHMARK_SYMBOL, reference_day)), reference[0], reference_open
            )
        spy_outcome = (
            outcome_price
            if item.benchmark_only
            else _price_at(
                series.get((BENCHMARK_SYMBOL, outcome_day)), item.outcome_at, outcome_open
            )
        )

        returns: tuple[Decimal, Decimal] | BarFault | None = None
        if (
            reference is not None
            and outcome_price is not None
            and spy_reference is not None
            and spy_outcome is not None
        ):
            returns = _returns(
                item, reference_day, (spy_reference, spy_outcome), (reference[1], outcome_price)
            )
        if isinstance(returns, BarFault):
            faults.add(returns)
            returns = None
        if reference is None or outcome_price is None or returns is None:
            if plan.run_at - item.outcome_at > PRICE_UNAVAILABLE_AFTER:
                records.append(_skip(key[0], key[1], SkipReason.PRICE_UNAVAILABLE, item.scope))
            else:
                awaiting.append(key)
            continue

        benchmark, excess = returns
        records.append(
            LabelOutcomeRecord(
                label_id=key[0],
                window=key[1],
                scope=item.scope,
                reference_at=reference[0],
                reference_price=reference[1],
                outcome_at=item.outcome_at,
                outcome_price=outcome_price,
                benchmark_return=benchmark,
                excess_return=excess,
                correct=_is_correct(item.label.direction, excess),
                skip_reason=None,
            )
        )

    records.sort(key=lambda r: (r.label_id, r.window))
    return GradeResult(
        records=tuple(records),
        awaiting_bars=tuple(sorted(awaiting)),
        bar_faults=tuple(sorted(faults, key=lambda f: (f.symbol, f.session_date, f.reason))),
    )
