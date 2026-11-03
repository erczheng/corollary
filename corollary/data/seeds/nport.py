"""The SPDR sector seed, built from SEC N-PORT and stored in the database.

Owner decision (Phase 3 step 4): decision 6's hand-downloaded State Street
seed is replaced by the eleven Select Sector SPDRs' quarterly NPORT-P filings
(:mod:`corollary.data.providers.sec`), each holding's CUSIP resolved to a
ticker by the broker's asset lookup. The runtime never writes into the source
tree: a snapshot is rows in ``spdr_holdings_snapshot`` / ``spdr_holding``
(migration 0010), and :func:`load_spdr_seed_from_db` serves the latest
accepted one through the unchanged :class:`~corollary.data.seeds.SpdrSeed`.

This lives beside the loader interface rather than in ``seeds/__init__``
because the providers import that package (``normalize_symbol``,
``EQUITY_SYMBOL_RE``); pulling the database and the SEC provider into it would
be an import cycle.

The owner's rules, as implemented
---------------------------------

* **A CUSIP that fails to resolve is logged with its fund, name and CUSIP,
  then skipped. Never guess a ticker from the name.** An EC line with no
  CUSIP at all (N-PORT writes ``000000000``; on the 2026-06-30 filings these
  are 29 foreign-domiciled members identified only by ISIN -- Linde is 14.06%
  of XLB) goes to the :class:`IsinResolver` seam instead. The default,
  :class:`NoIsinResolver`, resolves nothing, so each such line is logged
  with its fund, name, ISIN and weight and skipped. The owner's ISIN source
  is OpenFIGI (spec Q17): the ``spdr_holdings`` job passes
  :class:`~corollary.data.seeds.isin.OpenFigiIsinResolver`, which states and
  applies Q17's acceptance rule and caches accepted answers (migration
  0012). With no OpenFIGI provider, or no asset directory to judge answers
  against, the job's resolver raises :class:`IsinSourceUnavailable` and the
  build **aborts, storing nothing** (2026-10-07): a missing precondition is
  not a fact about the filing, and a stored refusal would suppress the
  start-up catch-up for a week. A refusal is stored only when the source
  answered -- with every ISIN answered and a fund still outside 90-110, the
  whole snapshot is refused on the weight band; the band is never lowered to
  let it through. A resolver with the optional
  :class:`BatchIsinResolver` ``prefetch`` hook is told every ISIN-only ISIN
  of the build first, so its lookups can be batched. Class shares are in the dot form
  (``BRK.B``) via :func:`~corollary.data.seeds.normalize_symbol`.
* **A resolved ISIN is still checked.** Its ticker must have the equity
  shape (:data:`~corollary.data.seeds.EQUITY_SYMBOL_RE`), and when the
  builder is given the broker's
  :class:`~corollary.data.providers.interface.AssetDirectory` it must be a
  symbol the broker lists; without a directory the shape alone is checked.
  Alpaca has no ISIN lookup, so there is no ``asset_by_cusip`` equivalent
  to cross-check against.
* **Validation fails closed.** All eleven funds present, at least
  :data:`MIN_EQUITIES_PER_FUND` resolved equities in each, each fund's
  resolved weights summing within [:data:`WEIGHT_BAND_LOW`,
  :data:`WEIGHT_BAND_HIGH`] inclusive -- or the **whole** snapshot is refused
  and the previous accepted one stays current. The refusal is recorded (rule
  and reason) and logged with its rule.
* **An NPORT-P/A is adopted, under the same validation** (owner decision,
  2026-09-30, replacing unit 4SEC-B2's refuse-on-amendment). For the quarter
  being loaded -- or the one already loaded -- every indexed amendment's
  document is fetched and attributed to its fund by ``genInfo/seriesId``;
  that fund's holdings come from its **latest-filed** amendment instead of
  its original, and the rebuilt snapshot passes or fails exactly the checks
  an original does (all eleven, >= 5, 90-110, the ISIN seam). An accepted
  rebuild of a loaded quarter is a new accepted row with the same report
  date and a later filed date; the loader already serves the newest accepted
  row of the latest report date. :attr:`SnapshotOutcome.adopted_amendments`
  names the funds whose amendment this snapshot adopted that the previous
  current one did not carry -- the ``spdr_holdings`` job notifies
  ``spdr_seed_amended`` from it, and only from an accepted outcome.
  **Fail closed, whole:** an amendment that cannot be attributed (its
  document cannot be fetched or read -- ``parse_nport_document`` refuses a
  document with no usable ``seriesId`` -- or reports another date), or two
  amendments for one fund filed the same day (no "latest" to pick), refuses
  the snapshot under :attr:`SnapshotRule.AMENDMENT`; a rebuild that fails
  validation is refused under its own rule. Either way the previous accepted
  snapshot stays served and nothing is notified. A Premium Income fund's
  amendment is attributed away and changes nothing. A loaded quarter whose
  sector amendments are all already adopted is ``unchanged``: nothing is
  re-resolved, nothing stored.
* **Aborts store nothing.** SEC refusing access (:class:`SecAccessRefused`,
  logged at ERROR -- never retried here), SEC being unreachable or answering
  garbage, or the asset lookup failing with anything other than "unknown
  CUSIP" (a 404 answers ``None``), or the ISIN resolver raising
  :class:`~corollary.data.providers.interface.ProviderError`, ends the build
  with nothing written; the next run tries again.

This is a **context** job: nothing here is a rule-9 producer, and nothing here
imports the engine runtime or a broker. The asset lookup is taken as a
:class:`CusipResolver` protocol, so this file names no vendor.

CUSIPs are resolved once per build (a CUSIP two funds hold is asked once) and
**not** cached across quarters: a CUSIP's ticker can change (a rename keeps
the CUSIP), and 474 lookups a quarter on the trading bucket is ~2.5 minutes.

A CUSIP that answers no active asset is followed through the vendor's
``name_change`` record to its successor CUSIP, when the resolver offers one
(:class:`CusipSuccessorResolver`, :func:`_follow_name_change`; 2026-11-02:
Exxon and ONEOK were re-CUSIPed and their filed CUSIPs named inactive
records, which put XLE at 73.87 and refused every build). Identity is by
CUSIP, never by ticker; mergers are not followed, and a cash-merged holding
(EA, WBD that quarter) stays unresolved for the weight band to judge.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Final, Literal, Protocol, TypeVar, runtime_checkable

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corollary.data.providers.interface import (
    AssetDirectory,
    CusipChange,
    EquityAsset,
    ProviderError,
    RateLimitedError,
)
from corollary.data.providers.sec import (
    NportDocument,
    NportFiling,
    NportFilings,
    SecAccessRefused,
    SecUnavailable,
    SecError,
    SectorFund,
    select_sector_documents,
)
from corollary.data.seeds import (
    EQUITY_SYMBOL_RE,
    NPORT_STALE_AFTER_DAYS,
    SPDR_FUNDS,
    SPDR_SECTORS,
    SeedError,
    SpdrHolding,
    SpdrSeed,
    normalize_symbol,
)
from corollary.db.models import SpdrHoldingRow, SpdrHoldingsSnapshot

logger = logging.getLogger(__name__)

#: Owner rule: fewer resolved equities than this in any fund refuses the snapshot.
MIN_EQUITIES_PER_FUND: Final = 5

#: How many times one CUSIP lookup answered 429 is asked again before the
#: build aborts (2026-11-02). A 429 is the server saying "not yet", not
#: "no". Before this, one 429 at lookup #233 of 474 threw the whole build away
#: on every start, so the seed was never built. Bounded, because a 429 that
#: outlasts three full windows is a key-sharing or plan problem, not a burst.
CUSIP_RATE_LIMIT_RETRIES: Final = 3

#: How many rate-limited retries one whole build may spend, across every
#: CUSIP (2026-11-02 audit). :data:`CUSIP_RATE_LIMIT_RETRIES` bounds one
#: CUSIP; without this the build had no total ceiling -- 474 CUSIPs x 3
#: retries x up to 61 s is hours, long enough to silently miss the job's
#: weekly slot. Ten waits of at most 61 s caps the stall near ten minutes.
#: Spent, the build aborts under ``cusip_lookup_failed`` and stores nothing.
SPDR_BUILD_RATE_LIMIT_RETRIES: Final = 10

#: The ``rule`` on a retry's WARNING. Deliberately not a :class:`SnapshotRule`
#: and not ``cusip_lookup_failed``: a retry has not failed, and a log query on
#: the failure rule must count failures only.
CUSIP_RATE_LIMIT_RETRY_RULE: Final = "cusip_rate_limit_retry"

#: After a 429, wait until the vendor's reset plus this margin: its clock and
#: ours agree to the second at best.
RATE_LIMIT_WAIT_MARGIN_SECONDS: Final = 1.0
#: No usable reset -- absent, unreadable, or at or before our "now": wait one
#: full window of the trading host's 200/min. A reset that looks past is
#: treated as unknown, not as "go at once": a local clock running ahead of
#: the vendor's makes *every* reset look past, and short waits would land
#: each retry in the same exhausted window and abort the build in seconds.
RATE_LIMIT_WAIT_DEFAULT_SECONDS: Final = 60.0
#: The longest wait: one window plus the margin. A reset header further out
#: than that is skew, not a window, and must not stall the job for an hour.
RATE_LIMIT_WAIT_MAX_SECONDS: Final = 61.0


def rate_limit_wait(reset_at: datetime | None, now: datetime) -> float:
    """Seconds to wait after a 429 before asking again. Pure.

    For a ``reset_at`` after ``now``: until it, plus
    :data:`RATE_LIMIT_WAIT_MARGIN_SECONDS`, capped at
    :data:`RATE_LIMIT_WAIT_MAX_SECONDS` -- so never under the margin. For no
    reset, or one at or before ``now``: :data:`RATE_LIMIT_WAIT_DEFAULT_SECONDS`.
    Alpaca's page asks clients not to retry before the reset, and this never
    does unless the header is past the cap.
    """
    if reset_at is None or reset_at <= now:
        return RATE_LIMIT_WAIT_DEFAULT_SECONDS
    wanted = (reset_at - now).total_seconds() + RATE_LIMIT_WAIT_MARGIN_SECONDS
    return min(RATE_LIMIT_WAIT_MAX_SECONDS, wanted)


@dataclass
class _RetryBudget:
    """One build's allowance of rate-limited retries. Mutable, and local to one build."""

    limit: int
    spent: int = 0


#: Owner rule: each fund's resolved EC weights (percent of net assets) must
#: sum within this band, **inclusive** at both ends.
WEIGHT_BAND_LOW: Final = Decimal("90")
WEIGHT_BAND_HIGH: Final = Decimal("110")

ACCEPTED: Final = "accepted"
REFUSED: Final = "refused"

Status = Literal["accepted", "refused", "unchanged", "aborted"]


class SnapshotRule(StrEnum):
    """Why a snapshot was refused, aborted, or a line skipped. Stored and logged."""

    # Refusals -- recorded, the previous snapshot stays current.
    MISSING_FUND = "missing_fund"  # no original NPORT-P for a sector fund's series
    # The other three ways ``select_sector_documents`` refuses a quarter, each
    # recorded under its own rule (unit 4SEC-B2; B1 filed them all as
    # ``missing_fund``, which sent the reader looking for the wrong problem).
    FILING_NOT_INDEXED = "filing_not_indexed"  # a document's filing is not in the index
    DUPLICATE_FILING = "duplicate_filing"  # two original NPORT-P for one series
    REPORT_DATE_MISMATCH = "report_date_mismatch"  # document repPdDate != index reportDate
    SELECTION_FAILED = "selection_failed"  # the selector refused for a cause none of the above
    AMENDMENT = "amendment"
    TOO_FEW_EQUITIES = "too_few_equities"
    WEIGHT_BAND = "weight_band"
    DUPLICATE_SYMBOL = "duplicate_symbol"
    SEED_INVARIANT = "seed_invariant"
    # Aborts -- nothing stored, retried next run.
    SEC_ACCESS_REFUSED = "sec_access_refused"
    SEC_UNAVAILABLE = "sec_unavailable"
    CUSIP_LOOKUP_FAILED = "cusip_lookup_failed"
    ISIN_LOOKUP_FAILED = "isin_lookup_failed"
    # The ISIN source cannot answer at all -- none configured, or its
    # precondition (the day's asset directory) not held yet. A fact about this
    # process, never about the filing, so it is never stored (2026-10-07).
    ISIN_SOURCE_UNAVAILABLE = "isin_source_unavailable"
    # Skipped lines -- logged, never guessed.
    NO_CUSIP = "no_cusip"  # no CUSIP and no well-formed ISIN: nothing to resolve by
    UNRESOLVED_CUSIP = "unresolved_cusip"
    UNRESOLVED_ISIN = "unresolved_isin"  # the ISIN resolver answered None
    UNUSABLE_SYMBOL = "unusable_symbol"
    UNKNOWN_SYMBOL = "unknown_symbol"  # an ISIN's ticker the asset directory does not list
    NON_POSITIVE_WEIGHT = "non_positive_weight"


class CusipResolver(Protocol):
    """The broker's CUSIP lookup: an asset, ``None`` for "unknown", or raises."""

    async def asset_by_cusip(self, cusip: str) -> EquityAsset | None: ...


@runtime_checkable
class CusipSuccessorResolver(CusipResolver, Protocol):
    """A :class:`CusipResolver` that can also say where a re-CUSIPed security went.

    ``cusip_name_changes`` answers every vendor ``name_change`` record naming
    ``cusip`` (as either side) processed in ``[start, end]``, or raises
    :class:`~corollary.data.providers.interface.ProviderError` /
    :class:`~corollary.data.providers.interface.RateLimitedError`. Optional,
    the way :class:`BatchIsinResolver` is: a resolver without it is never
    asked, and an unresolved CUSIP then stays unresolved.
    """

    async def cusip_name_changes(
        self, cusip: str, *, start: date, end: date
    ) -> tuple[CusipChange, ...]: ...


# The ``rule`` on each ``spdr_cusip_successor`` log line (2026-11-02). Not
# :class:`SnapshotRule` values: the holding line itself is still accepted or
# skipped under the existing rules (``unresolved_cusip`` when not accepted),
# and these say what the name-change fallback found on the way.
#: The successor was accepted: one name change, its new CUSIP an active
#: asset whose ticker is the name change's ``new_symbol``.
CUSIP_SUCCESSOR_ACCEPTED: Final = "cusip_successor_accepted"
#: No name change has the filed CUSIP as its ``old_cusip``.
CUSIP_SUCCESSOR_ABSENT: Final = "cusip_successor_absent"
#: Name changes from the filed CUSIP to more than one new CUSIP: refused.
CUSIP_SUCCESSOR_AMBIGUOUS: Final = "cusip_successor_ambiguous"
#: The successor CUSIP answers no active US equity. Not followed further.
CUSIP_SUCCESSOR_UNRESOLVED: Final = "cusip_successor_unresolved"
#: The successor CUSIP's asset carries a ticker other than the name change's:
#: the vendor's two records disagree, so neither is believed.
CUSIP_SUCCESSOR_SYMBOL_MISMATCH: Final = "cusip_successor_symbol_mismatch"

#: How far before the filing's report date the name-change window opens
#: (audit, 2026-11-02). A fund administrator can lag the vendor: a filing for
#: 2026-09-30 could still list ONEOK's 682680103, re-CUSIPed 2026-09-11, and
#: a window opening at the report date would miss that change. Looking back
#: adds no identity risk -- what is followed is still the vendor's own
#: CUSIP -> CUSIP record from the filed CUSIP, never a ticker -- so the bound
#: only limits how much history is read. Two quarters: a CUSIP stale for
#: longer than that is a filing problem, not a lag, and stays unresolved for
#: the weight band to judge.
NAME_CHANGE_LOOKBACK: Final = timedelta(days=180)


def name_change_successors(cusip: str, changes: Iterable[CusipChange]) -> list[CusipChange]:
    """The name changes that move ``cusip`` somewhere else, one per new CUSIP. Pure.

    Kept: rows whose ``old_cusip`` is ``cusip`` and whose ``new_cusip`` is
    not. A row naming ``cusip`` only as its ``new_cusip`` says where the
    security *came from*, not where it went -- the vendor's ``cusips=``
    filter returns those too (WBD's CUSIP came back on AT&T's 2022 spin-off).
    Two rows to the same new CUSIP are one successor (the first kept); the
    result is sorted by new CUSIP, so the same rows give the same answer.
    More than one entry means the vendor names two successors, and the
    caller refuses rather than choosing.
    """
    key = cusip.strip().upper()
    by_new: dict[str, CusipChange] = {}
    for change in changes:
        old, new = change.old_cusip.strip().upper(), change.new_cusip.strip().upper()
        if old == key and new != key and new not in by_new:
            by_new[new] = change
    return [by_new[new] for new in sorted(by_new)]


#: ISO 6166: two-letter country, nine alphanumerics, one check digit. A line
#: whose ISIN is not this shape is treated as having none.
ISIN_RE: Final = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")


class IsinSourceUnavailable(Exception):
    """The ISIN source cannot answer anything this build: a precondition, not a fact.

    Raised by an :class:`IsinResolver` (``resolve`` or ``prefetch``) when it
    has no source to ask -- none configured (:class:`UnavailableIsinResolver`)
    -- or lacks what it needs to judge an answer, the day's asset directory.
    The builder aborts under :attr:`SnapshotRule.ISIN_SOURCE_UNAVAILABLE` and
    stores nothing (2026-10-07). Answering ``None`` instead would skip every
    ISIN-only line, push XLB below the band, and store a refusal that says
    something about the filing that is not true -- and a stored refusal
    suppresses the start-up catch-up for a week.
    """


class IsinResolver(Protocol):
    """The seam for EC lines N-PORT filed with no CUSIP, only an ISIN.

    ``resolve`` answers a ticker for ``isin``, ``None`` for "cannot say"
    (the line is then logged and skipped, never guessed), or raises
    :class:`~corollary.data.providers.interface.ProviderError` when its
    source failed (the build aborts and stores nothing; the next run
    retries). It raises :class:`IsinSourceUnavailable` when it cannot answer
    at all -- also an abort, nothing stored. ``None`` must only ever mean the
    source *answered* and the answer was not acceptable: a stored refusal is
    a fact about the filing. ``fund`` is the first fund carrying that ISIN in fund order,
    passed for the resolver's own logging and checks.

    **The holding's name is deliberately not a parameter** (unit 4SEC-B2,
    B1 audit). The owner's rule is "never guess a ticker from the name";
    passed a name, a resolver *could*, and the rule would rest on every
    implementation remembering it. Not passed, it cannot. Only identifiers
    cross this seam. Each ISIN is asked once per build. Anything other than
    ``ProviderError`` is a programming error and propagates, still before
    anything is stored.
    """

    async def resolve(self, isin: str, *, fund: str) -> str | None: ...


@runtime_checkable
class BatchIsinResolver(IsinResolver, Protocol):
    """An :class:`IsinResolver` that can be told every ISIN of a build up front.

    The builder still asks :meth:`IsinResolver.resolve` once per ISIN; when
    the resolver also has ``prefetch``, the builder first calls it **once**
    with every ISIN-only ISIN it is about to ask (fund order, first seen,
    deduplicated), so a resolver over a batched source --
    :class:`~corollary.data.seeds.isin.OpenFigiIsinResolver` -- makes a few
    batched requests instead of one per ISIN. Optional: a resolver without it
    (:class:`NoIsinResolver`, a test's fake) is simply asked per ISIN.
    ``prefetch`` raising
    :class:`~corollary.data.providers.interface.ProviderError` aborts the
    build exactly as ``resolve`` raising one does: nothing stored.
    """

    async def prefetch(self, isins: Sequence[str]) -> None: ...


class NoIsinResolver:
    """An :class:`IsinResolver` that answers "cannot say" for every ISIN.

    Every ISIN-only line is then skipped: on the real 2026-06-30 quarter XLB
    sits at 73.36 and the snapshot is refused on the weight band. The
    builder's default when no resolver is passed, and the tests' way to
    exercise the band. **Not used by the ``spdr_holdings`` job** (2026-10-07):
    a missing ISIN source is not a fact about the filing, so the job passes
    :class:`UnavailableIsinResolver` and the build aborts instead of storing a
    refusal.
    """

    async def resolve(self, isin: str, *, fund: str) -> str | None:
        return None


class UnavailableIsinResolver:
    """An :class:`IsinResolver` with no source behind it: every ask raises.

    The ``spdr_holdings`` job's seam when the app holds no OpenFIGI provider.
    ``prefetch`` raises too, so a quarter with ISIN-only lines aborts before
    a single CUSIP is looked up; a quarter with none never asks and builds
    normally. ``reason`` is carried into the abort's reason.
    """

    def __init__(self, reason: str) -> None:
        self.reason = reason

    async def prefetch(self, isins: Sequence[str]) -> None:
        raise IsinSourceUnavailable(self.reason)

    async def resolve(self, isin: str, *, fund: str) -> str | None:
        raise IsinSourceUnavailable(self.reason)


class NportSource(Protocol):
    """What the builder reads from SEC (:class:`~corollary.data.providers.sec.SecProvider`)."""

    async def sector_fund_series(self) -> dict[str, str]: ...

    async def latest_nport_filings(self) -> NportFilings: ...

    async def nport_holdings(self, accession: str) -> NportDocument: ...


@dataclass(frozen=True)
class ResolvedHolding:
    """One EC line resolved to a ticker. ``weight`` is ``pctVal``, exact.

    ``cusip`` is ``None`` for a line resolved through the :class:`IsinResolver`;
    ``isin`` is the filing's ISIN when it gave a well-formed one.
    """

    etf: str
    sector: str
    series_id: str
    accession: str
    cusip: str | None
    symbol: str
    name: str
    weight: Decimal
    isin: str | None = None


@dataclass(frozen=True)
class SkippedLine:
    """One EC line logged and skipped, with the rule that skipped it."""

    etf: str
    name: str
    cusip: str | None
    isin: str | None
    weight: Decimal
    rule: SnapshotRule


@dataclass(frozen=True)
class FundProblem:
    """One validation failure: the rule, the fund, and the inputs in words."""

    rule: SnapshotRule
    etf: str
    detail: str


@dataclass(frozen=True)
class SnapshotOutcome:
    """What one build did. ``holdings`` is filled once resolution ran, even if refused."""

    status: Status
    report_date: date | None = None
    rule: SnapshotRule | None = None
    reason: str | None = None
    snapshot_id: int | None = None
    holdings: Mapping[str, tuple[ResolvedHolding, ...]] = field(default_factory=dict)
    skipped: tuple[SkippedLine, ...] = ()
    seed: SpdrSeed | None = None
    #: ``etf -> accession`` of each NPORT-P/A this snapshot adopted that the
    #: previously current snapshot did not carry. Filled only on ``accepted``;
    #: empty for an original-only quarter, a refusal, and every other status.
    adopted_amendments: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class AmendmentChoice:
    """Which NPORT-P/A each sector fund takes, or why none can be adopted.

    ``chosen`` maps a ticker to its latest-filed amendment and document.
    ``problem`` is the refusal reason when any amendment is unattributable or
    ambiguous; ``chosen`` is then empty -- nothing is partially adopted.
    """

    chosen: Mapping[str, tuple[NportFiling, NportDocument]] = field(default_factory=dict)
    problem: str | None = None


@dataclass(frozen=True)
class SnapshotAttempt:
    """The latest recorded attempt, for the UI to say why the seed is what it is."""

    id: int
    report_date: date
    filed_date: date
    built_at: datetime
    status: str
    rule: str | None
    reason: str | None
    skipped_lines: int


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ------------------------------------------------------------------ validation


def validate_funds(funds: Mapping[str, Sequence[ResolvedHolding]]) -> list[FundProblem]:
    """Every owner rule a set of resolved funds breaks, in fund order. Empty = valid.

    Pure. Weights are summed as :class:`~decimal.Decimal` in Python -- never
    in SQL, where ``Money`` is text and refuses to compare.
    """
    problems: list[FundProblem] = []
    for etf in SPDR_FUNDS:
        held = funds.get(etf)
        if held is None:
            problems.append(FundProblem(SnapshotRule.MISSING_FUND, etf, f"{etf} has no holdings"))
            continue
        symbols = [h.symbol for h in held]
        doubled = sorted({s for s in symbols if symbols.count(s) > 1})
        if doubled:
            problems.append(FundProblem(
                SnapshotRule.DUPLICATE_SYMBOL, etf,
                f"{etf} resolves two CUSIPs to one symbol: {', '.join(doubled)}",
            ))
        if len(held) < MIN_EQUITIES_PER_FUND:
            problems.append(FundProblem(
                SnapshotRule.TOO_FEW_EQUITIES, etf,
                f"{etf} has {len(held)} resolved equities (minimum {MIN_EQUITIES_PER_FUND})",
            ))
        total = sum((h.weight for h in held), Decimal(0))
        if not WEIGHT_BAND_LOW <= total <= WEIGHT_BAND_HIGH:
            problems.append(FundProblem(
                SnapshotRule.WEIGHT_BAND, etf,
                f"{etf} resolved equity weights sum to {total} "
                f"(band {WEIGHT_BAND_LOW}-{WEIGHT_BAND_HIGH})",
            ))
    return problems


def selection_failure_rule(
    series_by_ticker: Mapping[str, str],
    documents: Sequence[tuple[NportFiling, NportDocument]],
    filings: NportFilings,
) -> SnapshotRule | None:
    """Which rule :func:`~corollary.data.providers.sec.select_sector_documents` refuses on.

    Pure. It mirrors the selector's own checks **in the selector's order** --
    every document's filing is in the index; then, per sector ticker in
    ``series_by_ticker`` order, exactly one original for its series (none is
    :attr:`SnapshotRule.MISSING_FUND`, more is
    :attr:`SnapshotRule.DUPLICATE_FILING`), and that original's document
    reports the index's date -- so the rule recorded is the cause of the
    message recorded beside it. ``None`` when none applies; the builder then
    records :attr:`SnapshotRule.SELECTION_FAILED` rather than guess.
    ``tests/data/seeds/test_nport_snapshot.py`` pins each cause.
    """
    indexed = set(filings.filings)
    if any(filing not in indexed for filing, _ in documents):
        return SnapshotRule.FILING_NOT_INDEXED
    originals: dict[str, list[tuple[NportFiling, NportDocument]]] = {}
    for filing, document in documents:
        if not filing.is_amendment:
            originals.setdefault(document.series_id, []).append((filing, document))
    for series_id in series_by_ticker.values():
        candidates = originals.get(series_id, [])
        if not candidates:
            return SnapshotRule.MISSING_FUND
        if len(candidates) > 1:
            return SnapshotRule.DUPLICATE_FILING
        filing, document = candidates[0]
        if document.report_date != filing.report_date:
            return SnapshotRule.REPORT_DATE_MISMATCH
    return None


def choose_amendments(
    series_by_ticker: Mapping[str, str],
    fetched: Sequence[tuple[NportFiling, NportDocument]],
    unreadable: Sequence[tuple[NportFiling, str]],
) -> AmendmentChoice:
    """Attribute the quarter's NPORT-P/A documents to sector funds; pick the latest per fund.

    Pure. ``fetched`` are the amendments whose documents were read;
    ``unreadable`` the ones that could not be fetched or parsed, with why
    (``parse_nport_document`` refuses a document with no usable
    ``seriesId``, so "no seriesId" arrives here). Fail closed, whole:

    * any unreadable amendment -- it could be any fund's -- is a problem;
    * an amendment attributed to a sector fund whose document reports a date
      other than its filing's is a problem;
    * two amendments for one fund sharing the latest filing date is a
      problem: there is no "latest" to adopt, and accession order is a filer
      agent's sequence, not a filing time.

    An amendment for any other series (a Premium Income fund's) is ignored.
    """
    if unreadable:
        named = "; ".join(f"{f.accession} ({why[:160]})" for f, why in unreadable)
        return AmendmentChoice(problem=f"NPORT-P/A that cannot be attributed to a fund: {named}")
    ticker_of = {series_id: ticker for ticker, series_id in series_by_ticker.items()}
    by_ticker: dict[str, list[tuple[NportFiling, NportDocument]]] = {}
    for filing, document in fetched:
        ticker = ticker_of.get(document.series_id)
        if ticker is None:
            continue
        if document.report_date != filing.report_date:
            return AmendmentChoice(problem=(
                f"NPORT-P/A {filing.accession} for {ticker} is listed for "
                f"{filing.report_date.isoformat()} but its document reports "
                f"{document.report_date.isoformat()}"
            ))
        by_ticker.setdefault(ticker, []).append((filing, document))
    chosen: dict[str, tuple[NportFiling, NportDocument]] = {}
    for ticker in sorted(by_ticker):
        candidates = by_ticker[ticker]
        latest = max(filing.filing_date for filing, _ in candidates)
        tied = sorted(f.accession for f, _ in candidates if f.filing_date == latest)
        if len(tied) > 1:
            return AmendmentChoice(problem=(
                f"{ticker} has {len(tied)} NPORT-P/A filed {latest.isoformat()} "
                f"({', '.join(tied)}); no latest amendment to adopt"
            ))
        [pick] = [(f, d) for f, d in candidates if f.filing_date == latest]
        chosen[ticker] = pick
    return AmendmentChoice(chosen=chosen)


# ------------------------------------------------------------------ the build


class _Abort(Exception):
    """Abort the build. ``inputs`` are logged structured on the abort line (rule 8)."""

    def __init__(
        self, rule: SnapshotRule, reason: str, inputs: Mapping[str, object] | None = None
    ) -> None:
        super().__init__(reason)
        self.rule = rule
        self.reason = reason
        self.inputs: dict[str, object] = dict(inputs or {})


async def build_spdr_snapshot(
    sec: NportSource,
    resolver: CusipResolver,
    session_factory: Callable[[], Session],
    *,
    isin_resolver: IsinResolver | None = None,
    directory: AssetDirectory | None = None,
    clock: Callable[[], datetime] = _utcnow,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> SnapshotOutcome:
    """Build, validate and store one snapshot from the latest N-PORT quarter.

    ``isin_resolver`` answers the EC lines with no CUSIP; ``None`` means
    :class:`NoIsinResolver` (resolve nothing). ``directory``, when given,
    must list every ticker an ISIN resolved to.

    ``sleep`` is the wait after a CUSIP lookup answers 429
    (:func:`rate_limit_wait`): the lookup is asked again up to
    :data:`CUSIP_RATE_LIMIT_RETRIES` times, each retry logged, before the
    build aborts under :attr:`SnapshotRule.CUSIP_LOOKUP_FAILED`. The whole
    build may spend at most :data:`SPDR_BUILD_RATE_LIMIT_RETRIES` such
    retries across every CUSIP, then aborts the same way.

    Returns ``accepted`` (stored, now current), ``refused`` (recorded with
    its rule, the previous stays current), ``unchanged`` (the loaded
    snapshot is already this quarter's or newer; nothing fetched beyond the
    index) or ``aborted`` (nothing stored). Never raises for a vendor
    failure; a programming error still propagates.
    """
    try:
        return await _build(
            sec, resolver, isin_resolver or NoIsinResolver(), directory, session_factory,
            clock, sleep,
        )
    except _Abort as abort:
        logger.error(
            "spdr snapshot aborted (%s): %s; nothing stored, the loaded seed stays",
            abort.rule.value,
            abort.reason,
            extra={
                "event": "spdr_snapshot_aborted",
                "rule": abort.rule.value,
                "reason": abort.reason,
                "inputs": abort.inputs,
                "at": clock().isoformat(),
            },
        )
        return SnapshotOutcome(status="aborted", rule=abort.rule, reason=abort.reason)


async def _build(
    sec: NportSource,
    resolver: CusipResolver,
    isin_resolver: IsinResolver,
    directory: AssetDirectory | None,
    session_factory: Callable[[], Session],
    clock: Callable[[], datetime],
    sleep: Callable[[float], Awaitable[None]],
) -> SnapshotOutcome:
    try:
        series = await sec.sector_fund_series()
        filings = await sec.latest_nport_filings()
    except SecAccessRefused as exc:
        raise _Abort(SnapshotRule.SEC_ACCESS_REFUSED, str(exc)) from None
    except SecError as exc:
        raise _Abort(SnapshotRule.SEC_UNAVAILABLE, str(exc)) from None

    current = _current_accepted(session_factory)
    if current is not None and filings.report_date < current.report_date:
        return SnapshotOutcome(status="unchanged", report_date=current.report_date)
    reloading = current is not None and filings.report_date == current.report_date
    if reloading and not filings.amendments:
        assert current is not None
        return SnapshotOutcome(status="unchanged", report_date=current.report_date)

    # Amendments first: on a loaded quarter they decide whether there is any
    # work at all, before 22 originals and ~474 CUSIPs are asked again.
    amendment_docs: list[tuple[NportFiling, NportDocument]] = []
    unreadable: list[tuple[NportFiling, str]] = []
    for filing in filings.amendments:
        try:
            amendment_docs.append((filing, await sec.nport_holdings(filing.accession)))
        except SecAccessRefused as exc:
            raise _Abort(SnapshotRule.SEC_ACCESS_REFUSED, str(exc)) from None
        except (SecUnavailable, RateLimitedError) as exc:
            # An outage is not a fact about the amendment: abort under the
            # same rule the originals use, so a restart retries it instead of
            # a stored refusal suppressing the catch-up for a week.
            raise _Abort(SnapshotRule.SEC_UNAVAILABLE, str(exc)) from None
        except SecError as exc:
            # Unattributable, not an abort (owner decision 2026-09-30): an
            # amendment nobody can read could be any fund's, so the quarter is
            # refused whole and the previous snapshot stays.
            unreadable.append((filing, str(exc)))
    choice = choose_amendments(series, amendment_docs, unreadable)
    # What the previously current snapshot carried, per fund. A new quarter's
    # originals never match an old quarter's accessions, so every amendment a
    # new quarter adopts counts as adopted.
    carried = _carried_accessions(session_factory, current.id) if current is not None else {}
    adopted = {
        ticker: filing.accession
        for ticker, (filing, _) in choice.chosen.items()
        if carried.get(ticker) != filing.accession
    }
    index_filed = max((f.filing_date for f in filings.filings), default=filings.report_date)
    if reloading and choice.problem is None and not adopted:
        assert current is not None
        logger.info(
            "spdr snapshot: %d NPORT-P/A for %s, none a sector fund's that is not "
            "already adopted; the loaded snapshot stays",
            len(filings.amendments), filings.report_date.isoformat(),
            extra={
                "event": "spdr_snapshot_amendments_already_adopted",
                "report_date": filings.report_date.isoformat(),
                "accessions": [f.accession for f in filings.amendments],
                "carried": sorted(carried.values()),
            },
        )
        return SnapshotOutcome(status="unchanged", report_date=current.report_date)
    if choice.problem is not None:
        return _refuse(session_factory, clock, filings.report_date, index_filed,
                       SnapshotRule.AMENDMENT, choice.problem, ())

    documents: list[tuple[NportFiling, NportDocument]] = []
    for filing in filings.originals:
        try:
            documents.append((filing, await sec.nport_holdings(filing.accession)))
        except SecAccessRefused as exc:
            raise _Abort(SnapshotRule.SEC_ACCESS_REFUSED, str(exc)) from None
        except SecError as exc:
            raise _Abort(SnapshotRule.SEC_UNAVAILABLE, str(exc)) from None
    # Every amendment was read (an unreadable one refused above), so the
    # selector's own ``amended``/``unattributed`` flags are superseded by
    # ``choice``: it is handed the amendment documents only so that none is
    # reported as unfetched.
    documents.extend(amendment_docs)

    try:
        selection = select_sector_documents(series, documents, filings=filings)
    except SecError as exc:
        rule = selection_failure_rule(series, documents, filings) or SnapshotRule.SELECTION_FAILED
        return _refuse(session_factory, clock, filings.report_date, index_filed,
                       rule, str(exc), ())
    funds: dict[str, SectorFund] = dict(selection.funds)
    for ticker, (filing, document) in choice.chosen.items():
        funds[ticker] = SectorFund(ticker, series[ticker], filing, document)
    filed = max(fund.filing.filing_date for fund in funds.values())

    holdings, skipped = await _resolve(
        funds, resolver, isin_resolver, directory, report_date=filings.report_date,
        clock=clock, sleep=sleep,
    )
    problems = validate_funds(holdings)
    if problems:
        reason = "; ".join(f"{p.rule.value}: {p.detail}" for p in problems)
        for problem in problems:
            logger.warning(
                "spdr snapshot check failed (%s): %s",
                problem.rule.value,
                problem.detail,
                extra={"event": "spdr_snapshot_check_failed", "rule": problem.rule.value,
                       "etf": problem.etf, "detail": problem.detail},
            )
        return _refuse(session_factory, clock, filings.report_date, filed,
                       problems[0].rule, reason, skipped, holdings)

    try:
        seed = SpdrSeed(
            as_of=filings.report_date,
            rows=tuple(
                SpdrHolding(h.etf, h.sector, h.symbol, h.weight)
                for etf in SPDR_FUNDS for h in holdings[etf]
            ),
            filed_date=filed,
            stale_after_days=NPORT_STALE_AFTER_DAYS,
        )
    except SeedError as exc:
        return _refuse(session_factory, clock, filings.report_date, filed,
                       SnapshotRule.SEED_INVARIANT, str(exc), skipped, holdings)

    built_at = clock()
    with session_factory() as session, session.begin():
        snapshot = SpdrHoldingsSnapshot(
            report_date=filings.report_date, filed_date=filed, built_at=built_at,
            status=ACCEPTED, rule=None, reason=None, skipped_lines=len(skipped),
        )
        session.add(snapshot)
        session.flush()
        snapshot_id = snapshot.id
        for etf in SPDR_FUNDS:
            for h in holdings[etf]:
                session.add(SpdrHoldingRow(
                    snapshot_id=snapshot_id, etf=h.etf, symbol=h.symbol, sector=h.sector,
                    series_id=h.series_id, accession=h.accession, cusip=h.cusip,
                    isin=h.isin, name=h.name, weight=h.weight,
                ))
    invalidate_spdr_seed_cache()
    logger.info(
        "spdr snapshot %d accepted: report date %s, filed %s, %d holdings, %d lines skipped",
        snapshot_id, filings.report_date.isoformat(), filed.isoformat(),
        len(seed.rows), len(skipped),
        extra={"event": "spdr_snapshot_accepted", "snapshot_id": snapshot_id,
               "report_date": filings.report_date.isoformat(), "filed_date": filed.isoformat(),
               "holdings": len(seed.rows), "skipped_lines": len(skipped),
               "adopted_amendments": dict(adopted),
               "at": built_at.isoformat()},
    )
    return SnapshotOutcome(
        status="accepted", report_date=filings.report_date, snapshot_id=snapshot_id,
        holdings=holdings, skipped=skipped, seed=seed, adopted_amendments=adopted,
    )


_T = TypeVar("_T")

#: The ``lookup`` field on a rate-limit retry line: which question was asked.
LOOKUP_ASSET: Final = "asset"  # GET /v2/assets/{cusip}, trading host
LOOKUP_NAME_CHANGE: Final = "name_change"  # GET /v1/corporate-actions, data host


async def _lookup_cusip(
    resolver: CusipResolver,
    cusip: str,
    *,
    etf: str,
    name: str,
    budget: _RetryBudget,
    clock: Callable[[], datetime],
    sleep: Callable[[float], Awaitable[None]],
) -> EquityAsset | None:
    """One CUSIP's asset, through :func:`_rate_limited`."""
    return await _rate_limited(
        lambda: resolver.asset_by_cusip(cusip), cusip, lookup=LOOKUP_ASSET,
        etf=etf, name=name, budget=budget, clock=clock, sleep=sleep,
    )


async def _rate_limited(
    fetch: Callable[[], Awaitable[_T]],
    cusip: str,
    *,
    lookup: str,
    etf: str,
    name: str,
    budget: _RetryBudget,
    clock: Callable[[], datetime],
    sleep: Callable[[float], Awaitable[None]],
    window: tuple[date, date] | None = None,
) -> _T:
    """One lookup about ``cusip``, waiting out a 429 up to :data:`CUSIP_RATE_LIMIT_RETRIES` times.

    ``lookup`` names the question (:data:`LOOKUP_ASSET`,
    :data:`LOOKUP_NAME_CHANGE`) and is carried on the retry line and in
    every abort's ``inputs`` (rule 8), with ``window`` -- the ``[start, end]``
    a dated question asked about -- as ``window_start``/``window_end`` and
    stated in the abort's reason. Each
    retry also spends one of the build's ``budget``
    (:data:`SPDR_BUILD_RATE_LIMIT_RETRIES`), so the build as a whole has a
    ceiling and not only each CUSIP -- the name-change fallback draws on the
    same allowance as the asset lookups. Each retry is logged at WARNING under
    :data:`CUSIP_RATE_LIMIT_RETRY_RULE` with the CUSIP, its fund, the attempt,
    the budget, the vendor's reset and the wait. It carries no error text, so
    nothing a vendor echoed can reach the log through this line. Past either
    bound, or on any other :class:`ProviderError`, raises :class:`_Abort`.
    The build then stores nothing and the loaded seed stays. Nothing here
    can reach the engine's halt state: this is a context job.
    """
    asked = "" if lookup == LOOKUP_ASSET else f" ({lookup} lookup"
    if window is not None:
        asked += f", processed {window[0].isoformat()} to {window[1].isoformat()}"
    asked += ")" if asked else ""
    asked_inputs: dict[str, object] = {"cusip": cusip, "lookup": lookup, "etf": etf}
    if window is not None:
        asked_inputs["window_start"] = window[0].isoformat()
        asked_inputs["window_end"] = window[1].isoformat()
    attempt = 0
    while True:
        try:
            return await fetch()
        except RateLimitedError as exc:
            reset = exc.reset_at.isoformat() if exc.reset_at else None
            inputs: dict[str, object] = {
                **asked_inputs,
                "build_retry_budget": budget.limit,
                "build_retries_spent": budget.spent,
                "cusip_attempts": attempt + 1,
                "reset_at": reset,
            }
            if attempt >= CUSIP_RATE_LIMIT_RETRIES:
                raise _Abort(
                    SnapshotRule.CUSIP_LOOKUP_FAILED,
                    f"{etf} {name[:80]} CUSIP {cusip}{asked}: still rate limited after "
                    f"{CUSIP_RATE_LIMIT_RETRIES} retries: {exc}",
                    inputs,
                ) from None
            if budget.spent >= budget.limit:
                raise _Abort(
                    SnapshotRule.CUSIP_LOOKUP_FAILED,
                    f"{etf} {name[:80]} CUSIP {cusip}{asked}: rate limited with the build's "
                    f"retry budget of {budget.limit} spent: {exc}",
                    inputs,
                ) from None
            attempt += 1
            budget.spent += 1
            now = clock()
            wait = rate_limit_wait(exc.reset_at, now)
            logger.warning(
                "spdr snapshot: CUSIP lookup %s%s (%s) rate limited; waiting %.1f s "
                "for the server window, retry %d of %d (build: %d of %d)",
                cusip, asked, etf, wait, attempt, CUSIP_RATE_LIMIT_RETRIES,
                budget.spent, budget.limit,
                extra={
                    "event": "spdr_cusip_rate_limited",
                    "rule": CUSIP_RATE_LIMIT_RETRY_RULE,
                    "etf": etf,
                    "holding_name": name[:80],
                    "cusip": cusip,
                    "lookup": lookup,
                    "attempt": attempt,
                    "retry_bound": CUSIP_RATE_LIMIT_RETRIES,
                    "build_retries_spent": budget.spent,
                    "build_retry_budget": budget.limit,
                    "reset_at": reset,
                    "wait_seconds": wait,
                    "at": now.isoformat(),
                },
            )
            await sleep(wait)
        except ProviderError as exc:
            # The provider's message is already through its redaction (rule 6).
            raise _Abort(
                SnapshotRule.CUSIP_LOOKUP_FAILED,
                f"{etf} {name[:80]} CUSIP {cusip}{asked}: {exc}",
                asked_inputs,
            ) from None


async def _follow_name_change(
    resolver: CusipSuccessorResolver,
    cusip: str,
    *,
    etf: str,
    name: str,
    start: date,
    end: date,
    budget: _RetryBudget,
    clock: Callable[[], datetime],
    sleep: Callable[[float], Awaitable[None]],
) -> EquityAsset | None:
    """The active asset a re-CUSIPed holding became, or ``None``. Raises :class:`_Abort` on a lookup error.

    Asked only for a CUSIP the asset lookup left unresolved (2026-11-02:
    Exxon's and ONEOK's filed CUSIPs named inactive asset records after
    each was re-CUSIPed under its own ticker, and XLE fell to 73.87).
    **Identity is proved by CUSIP, never by ticker.** Asset objects carry
    no ``cusip`` (probed: absent on every record, including AAPL's), so a
    by-symbol lookup cannot tell the holding from an unrelated company that
    reused the ticker. The vendor's ``name_change`` record links the filed
    CUSIP to its successor CUSIP directly, and the successor is resolved
    with the same ``asset_by_cusip`` as every other line. Accepted only when:

    - exactly one new CUSIP is named from the filed one
      (:func:`name_change_successors`), else :data:`CUSIP_SUCCESSOR_ABSENT`
      or :data:`CUSIP_SUCCESSOR_AMBIGUOUS`;
    - that CUSIP answers an active US equity, else
      :data:`CUSIP_SUCCESSOR_UNRESOLVED` -- one hop only, never followed further;
    - its ticker is the name change's ``new_symbol``, else
      :data:`CUSIP_SUCCESSOR_SYMBOL_MISMATCH`: two vendor records that
      disagree are not evidence for either.

    Mergers are never followed: they are different corporate actions, not
    1:1 into the same security, and a cash-merged holding stays unresolved
    for the weight band to judge. Both requests go through
    :func:`_rate_limited` on the build's one retry budget. Every outcome is
    logged at WARNING as ``spdr_cusip_successor`` with its rule, the fund,
    both CUSIPs, both symbols and the time; no vendor error text, since an
    error never reaches this log line -- it aborts instead.
    """
    changes = await _rate_limited(
        lambda: resolver.cusip_name_changes(cusip, start=start, end=end), cusip,
        lookup=LOOKUP_NAME_CHANGE, etf=etf, name=name, budget=budget, clock=clock, sleep=sleep,
        window=(start, end),
    )
    successors = name_change_successors(cusip, changes)
    if not successors:
        _log_successor(CUSIP_SUCCESSOR_ABSENT, etf=etf, name=name, cusip=cusip,
                       change=None, successors=(), asset=None, start=start, end=end, clock=clock)
        return None
    if len(successors) > 1:
        _log_successor(CUSIP_SUCCESSOR_AMBIGUOUS, etf=etf, name=name, cusip=cusip,
                       change=None, successors=successors, asset=None,
                       start=start, end=end, clock=clock)
        return None
    [change] = successors
    asset = await _lookup_cusip(
        resolver, change.new_cusip, etf=etf, name=name, budget=budget, clock=clock, sleep=sleep,
    )
    if asset is None:
        rule = CUSIP_SUCCESSOR_UNRESOLVED
    elif normalize_symbol(asset.symbol) != normalize_symbol(change.new_symbol):
        rule = CUSIP_SUCCESSOR_SYMBOL_MISMATCH
    else:
        rule = CUSIP_SUCCESSOR_ACCEPTED
    _log_successor(rule, etf=etf, name=name, cusip=cusip, change=change,
                   successors=successors, asset=asset, start=start, end=end, clock=clock)
    return asset if rule == CUSIP_SUCCESSOR_ACCEPTED else None


def _log_successor(
    rule: str,
    *,
    etf: str,
    name: str,
    cusip: str,
    change: CusipChange | None,
    successors: Sequence[CusipChange],
    asset: EquityAsset | None,
    start: date,
    end: date,
    clock: Callable[[], datetime],
) -> None:
    """One ``spdr_cusip_successor`` line. Identifiers only: no vendor text beyond parsed fields.

    Every vendor field here was shape-checked when the provider decoded it
    (CUSIPs, equity symbols, a UUID-shaped action id), so none is free text
    (rule 6). ``conflicting`` carries every row in ``successors`` -- for an
    ambiguous refusal, the rows the vendor contradicts itself with, by the
    fields needed to dispute them (rule 8) -- in ``successor_cusips`` order.
    """
    successor_symbol = normalize_symbol(asset.symbol) if asset is not None else None
    conflicting = [
        {
            "action_id": s.action_id,
            "old_cusip": s.old_cusip,
            "new_cusip": s.new_cusip,
            "old_symbol": s.old_symbol,
            "new_symbol": s.new_symbol,
            "process_date": s.process_date.isoformat(),
        }
        for s in successors
    ]
    rows = "; ".join(
        f"{s.action_id} {s.old_cusip} {s.old_symbol or '-'} -> {s.new_cusip} "
        f"{s.new_symbol or '-'} on {s.process_date.isoformat()}"
        for s in successors
    )
    logger.warning(
        "spdr snapshot: %s %r CUSIP %s name-change fallback: %s (successor %s, %s -> %s, "
        "asset %s; name changes processed %s to %s: %s)",
        etf, name[:80], cusip, rule,
        change.new_cusip if change else None,
        change.old_symbol if change else None,
        change.new_symbol if change else None,
        successor_symbol, start.isoformat(), end.isoformat(), rows or "none",
        extra={
            "event": "spdr_cusip_successor",
            "rule": rule,
            "etf": etf,
            "holding_name": name[:80],
            "cusip": cusip,
            "successor_cusip": change.new_cusip if change else None,
            "successor_cusips": [s.new_cusip for s in successors],
            "conflicting": conflicting,
            "old_symbol": change.old_symbol if change else None,
            "new_symbol": change.new_symbol if change else None,
            "action_id": change.action_id if change else None,
            "successor_symbol": successor_symbol,
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "at": clock().isoformat(),
        },
    )


async def _resolve(
    funds: Mapping[str, SectorFund],
    resolver: CusipResolver,
    isin_resolver: IsinResolver,
    directory: AssetDirectory | None,
    *,
    report_date: date,
    clock: Callable[[], datetime],
    sleep: Callable[[float], Awaitable[None]],
) -> tuple[dict[str, tuple[ResolvedHolding, ...]], tuple[SkippedLine, ...]]:
    """Resolve every EC line of the selected funds. Raises :class:`_Abort` on a lookup error.

    A CUSIP lookup answered 429 is retried by :func:`_lookup_cusip`; every
    other lookup error aborts at once.

    A CUSIP the asset lookup leaves unresolved is followed through its
    vendor name change when ``resolver`` is a :class:`CusipSuccessorResolver`
    (:func:`_follow_name_change`), over name changes processed from
    :data:`NAME_CHANGE_LOOKBACK` before ``report_date`` -- the date the fund
    said it held that CUSIP, which can lag the change -- to the build's UTC
    day, which is never earlier than the ET day the vendor dates by.

    A line with a CUSIP is asked of the broker; a line with none but a
    well-formed ISIN is asked of ``isin_resolver``; anything else is
    skipped. Every skip is logged with fund, name, CUSIP, ISIN, weight and
    rule. Nothing is ever resolved by name.
    """
    if isinstance(isin_resolver, BatchIsinResolver):
        wanted = _isin_only_isins(funds)
        if wanted:
            try:
                await isin_resolver.prefetch(wanted)
            except IsinSourceUnavailable as exc:
                raise _Abort(
                    SnapshotRule.ISIN_SOURCE_UNAVAILABLE,
                    f"{len(wanted)} ISIN-only lines cannot be asked: {exc}",
                ) from None
            except ProviderError as exc:
                raise _Abort(
                    SnapshotRule.ISIN_LOOKUP_FAILED,
                    f"prefetching {len(wanted)} ISIN-only lines: {exc}",
                ) from None
    cusip_answers: dict[str, EquityAsset | None] = {}
    # One allowance for the whole build, across every CUSIP below.
    budget = _RetryBudget(SPDR_BUILD_RATE_LIMIT_RETRIES)
    successors = resolver if isinstance(resolver, CusipSuccessorResolver) else None
    window_start = report_date - NAME_CHANGE_LOOKBACK
    window_end = max(report_date, clock().astimezone(timezone.utc).date())
    isin_answers: dict[str, str | None] = {}
    resolved: dict[str, tuple[ResolvedHolding, ...]] = {}
    skipped: list[SkippedLine] = []
    for etf in SPDR_FUNDS:
        fund = funds.get(etf)
        if fund is None:
            continue
        sector = SPDR_SECTORS[etf]
        kept: list[ResolvedHolding] = []
        for line in fund.document.equity:
            isin = line.isin if line.isin is not None and ISIN_RE.fullmatch(line.isin) else None
            rule: SnapshotRule | None = None
            symbol = ""
            if line.pct_val <= 0:
                rule = SnapshotRule.NON_POSITIVE_WEIGHT
            elif line.cusip is not None:
                if line.cusip not in cusip_answers:
                    found = await _lookup_cusip(
                        resolver, line.cusip, etf=etf, name=line.name, budget=budget,
                        clock=clock, sleep=sleep,
                    )
                    if found is None and successors is not None:
                        found = await _follow_name_change(
                            successors, line.cusip, etf=etf, name=line.name,
                            start=window_start, end=window_end, budget=budget,
                            clock=clock, sleep=sleep,
                        )
                    cusip_answers[line.cusip] = found
                asset = cusip_answers[line.cusip]
                if asset is None:
                    rule = SnapshotRule.UNRESOLVED_CUSIP
                else:
                    symbol = normalize_symbol(asset.symbol)
                    if not EQUITY_SYMBOL_RE.fullmatch(symbol):
                        rule = SnapshotRule.UNUSABLE_SYMBOL
            elif isin is not None:
                if isin not in isin_answers:
                    try:
                        isin_answers[isin] = await isin_resolver.resolve(isin, fund=etf)
                    except IsinSourceUnavailable as exc:
                        raise _Abort(
                            SnapshotRule.ISIN_SOURCE_UNAVAILABLE,
                            f"{etf} {line.name[:80]} ISIN {isin} cannot be asked: {exc}",
                        ) from None
                    except ProviderError as exc:
                        raise _Abort(
                            SnapshotRule.ISIN_LOOKUP_FAILED,
                            f"{etf} {line.name[:80]} ISIN {isin}: {exc}",
                        ) from None
                answer = isin_answers[isin]
                if answer is None:
                    rule = SnapshotRule.UNRESOLVED_ISIN
                else:
                    symbol = normalize_symbol(answer)
                    if not EQUITY_SYMBOL_RE.fullmatch(symbol):
                        rule = SnapshotRule.UNUSABLE_SYMBOL
                    elif directory is not None and symbol not in directory:
                        rule = SnapshotRule.UNKNOWN_SYMBOL
            else:
                rule = SnapshotRule.NO_CUSIP
            if rule is not None:
                skipped.append(SkippedLine(etf, line.name, line.cusip, line.isin, line.pct_val, rule))
                logger.warning(
                    "spdr snapshot: %s %r (CUSIP %s, ISIN %s, weight %s) skipped: %s",
                    etf, line.name, line.cusip, line.isin, line.pct_val, rule.value,
                    extra={"event": "spdr_holding_skipped", "rule": rule.value, "etf": etf,
                           "holding_name": line.name, "cusip": line.cusip, "isin": line.isin,
                           "weight": str(line.pct_val), "accession": fund.filing.accession},
                )
                continue
            kept.append(ResolvedHolding(
                etf=etf, sector=sector, series_id=fund.series_id,
                accession=fund.filing.accession, cusip=line.cusip, symbol=symbol,
                name=line.name, weight=line.pct_val, isin=isin,
            ))
        resolved[etf] = tuple(kept)
    return resolved, tuple(skipped)


def _isin_only_isins(funds: Mapping[str, SectorFund]) -> list[str]:
    """Every ISIN :func:`_resolve` will ask the ISIN resolver, in the order it asks.

    The same selection as the loop: a positive weight, no CUSIP, and a
    well-formed ISIN. Fund order, first seen, deduplicated.
    """
    wanted: dict[str, None] = {}
    for etf in SPDR_FUNDS:
        fund = funds.get(etf)
        if fund is None:
            continue
        for line in fund.document.equity:
            if line.pct_val <= 0 or line.cusip is not None:
                continue
            if line.isin is not None and ISIN_RE.fullmatch(line.isin):
                wanted.setdefault(line.isin, None)
    return list(wanted)


def _refuse(
    session_factory: Callable[[], Session],
    clock: Callable[[], datetime],
    report_date: date,
    filed: date,
    rule: SnapshotRule,
    reason: str,
    skipped: tuple[SkippedLine, ...],
    holdings: Mapping[str, tuple[ResolvedHolding, ...]] | None = None,
) -> SnapshotOutcome:
    """Record a refused attempt (no holdings), log it with its rule, keep the previous."""
    built_at = clock()
    with session_factory() as session, session.begin():
        snapshot = SpdrHoldingsSnapshot(
            report_date=report_date, filed_date=filed, built_at=built_at,
            status=REFUSED, rule=rule.value, reason=reason, skipped_lines=len(skipped),
        )
        session.add(snapshot)
        session.flush()
        snapshot_id = snapshot.id
    invalidate_spdr_seed_cache()
    logger.warning(
        "spdr snapshot for %s refused (%s): %s; the previous snapshot stays current",
        report_date.isoformat(), rule.value, reason,
        extra={"event": "spdr_snapshot_refused", "rule": rule.value, "reason": reason,
               "report_date": report_date.isoformat(), "filed_date": filed.isoformat(),
               "snapshot_id": snapshot_id, "skipped_lines": len(skipped),
               "at": built_at.isoformat()},
    )
    return SnapshotOutcome(
        status="refused", report_date=report_date, rule=rule, reason=reason,
        snapshot_id=snapshot_id, holdings=dict(holdings or {}), skipped=skipped,
    )


# ------------------------------------------------------------------ the loader


def _current_accepted(session_factory: Callable[[], Session]) -> SnapshotAttempt | None:
    with session_factory() as session:
        row = session.scalars(
            select(SpdrHoldingsSnapshot)
            .where(SpdrHoldingsSnapshot.status == ACCEPTED)
            .order_by(SpdrHoldingsSnapshot.report_date.desc(), SpdrHoldingsSnapshot.id.desc())
            .limit(1)
        ).first()
        return None if row is None else _attempt(row)


def _carried_accessions(session_factory: Callable[[], Session], snapshot_id: int) -> dict[str, str]:
    """``etf -> accession`` a stored snapshot's holdings were built from.

    One filing per fund by construction (every row of a fund is resolved
    from one document); a fund with rows from two accessions is a
    corrupted snapshot, and is left out so that whatever is chosen now
    counts as new and is rebuilt rather than trusted.
    """
    with session_factory() as session:
        pairs = session.execute(
            select(SpdrHoldingRow.etf, SpdrHoldingRow.accession)
            .where(SpdrHoldingRow.snapshot_id == snapshot_id)
            .distinct()
        ).all()
    seen: dict[str, set[str]] = {}
    for etf, accession in pairs:
        seen.setdefault(etf, set()).add(accession)
    return {etf: next(iter(acc)) for etf, acc in seen.items() if len(acc) == 1}


@dataclass(frozen=True)
class StoredAmendments:
    """A stored accepted snapshot's dates and some funds' accessions: for the notice."""

    snapshot_id: int
    report_date: date
    filed_date: date
    #: ``etf -> accession`` as stored, for the funds asked about that have rows.
    accessions: Mapping[str, str]


def stored_snapshot_amendments(
    session_factory: Callable[[], Session], snapshot_id: int, etfs: Iterable[str]
) -> StoredAmendments | None:
    """Read back a stored **accepted** snapshot's dates and ``etfs``' accessions.

    Decision 20: the ``spdr_seed_amended`` notice quotes what was written,
    not what the build held in memory. ``None`` if the row is missing or not
    accepted. A fund whose rows carry two accessions is left out (see
    :func:`_carried_accessions`), so it can never match.
    """
    with session_factory() as session:
        row = session.get(SpdrHoldingsSnapshot, snapshot_id)
        if row is None or row.status != ACCEPTED:
            return None
        report_date, filed_date = row.report_date, row.filed_date
    wanted = set(etfs)
    carried = _carried_accessions(session_factory, snapshot_id)
    return StoredAmendments(
        snapshot_id=snapshot_id, report_date=report_date, filed_date=filed_date,
        accessions={etf: acc for etf, acc in carried.items() if etf in wanted},
    )


def _attempt(row: SpdrHoldingsSnapshot) -> SnapshotAttempt:
    return SnapshotAttempt(
        id=row.id, report_date=row.report_date, filed_date=row.filed_date,
        built_at=row.built_at, status=row.status, rule=row.rule, reason=row.reason,
        skipped_lines=row.skipped_lines,
    )


def latest_snapshot_attempt(session_factory: Callable[[], Session]) -> SnapshotAttempt | None:
    """The most recently recorded attempt, accepted or refused -- the UI's "why"."""
    with session_factory() as session:
        row = session.scalars(
            select(SpdrHoldingsSnapshot).order_by(SpdrHoldingsSnapshot.id.desc()).limit(1)
        ).first()
        return None if row is None else _attempt(row)


#: What decides whether a cached seed still holds: ``(id, built_at)`` of the
#: newest attempt of any status, and of the latest accepted one.
_CacheKey = tuple[tuple[int, datetime] | None, tuple[int, datetime] | None]

#: bind URL -> (the key when loaded, the seed). The key check makes the cache
#: self-invalidating even across processes; the builder also clears it on
#: every record.
#:
#: **Not the id alone** (unit 4SEC-B2, B1 audit): migration 0010's id column
#: has no ``sqlite_autoincrement``, so SQLite hands the id of a deleted
#: newest row to the next insert, and an id-only key would go on serving the
#: deleted snapshot. ``built_at`` rides with each id, so a reused id carries a
#: different key. The newest attempt covers a refusal changing
#: ``newer_report_date``; the latest accepted covers the served rows.
_SEED_CACHE: dict[str, tuple[_CacheKey, SpdrSeed | None]] = {}


def invalidate_spdr_seed_cache() -> None:
    """Forget every cached seed. The builder calls this after it records an attempt."""
    _SEED_CACHE.clear()


def load_spdr_seed_from_db(session_factory: Callable[[], Session]) -> SpdrSeed | None:
    """The latest accepted snapshot as a :class:`SpdrSeed`, or ``None`` if none ever was.

    ``as_of`` is the N-PORT report date, ``filed_date`` the filing date,
    ``stale_after_days`` :data:`~corollary.data.seeds.NPORT_STALE_AFTER_DAYS`,
    and ``newer_report_date`` the newest *refused* attempt's report date when
    it is newer than the loaded one ("a newer filing exists but has not been
    loaded"). Cached; two one-row queries per call -- ``(id, built_at)`` of
    the newest attempt and of the latest accepted one -- decide whether the
    cache still holds (see :data:`_SEED_CACHE` for why not ``max(id)``).
    """
    with session_factory() as session:
        bind = session.get_bind()
        url = str(bind.engine.url)
        newest = session.execute(
            select(SpdrHoldingsSnapshot.id, SpdrHoldingsSnapshot.built_at)
            .order_by(SpdrHoldingsSnapshot.id.desc())
            .limit(1)
        ).first()
        accepted = session.execute(
            select(SpdrHoldingsSnapshot.id, SpdrHoldingsSnapshot.built_at)
            .where(SpdrHoldingsSnapshot.status == ACCEPTED)
            .order_by(SpdrHoldingsSnapshot.report_date.desc(), SpdrHoldingsSnapshot.id.desc())
            .limit(1)
        ).first()
        key: _CacheKey = (
            None if newest is None else (newest[0], newest[1]),
            None if accepted is None else (accepted[0], accepted[1]),
        )
        cached = _SEED_CACHE.get(url)
        if cached is not None and cached[0] == key:
            return cached[1]
        seed = _read_seed(session)
    _SEED_CACHE[url] = (key, seed)
    return seed


class DatabaseSeedLoader:
    """:func:`load_spdr_seed_from_db` as the zero-argument loader the app state holds.

    ``create_app`` builds one before it knows its database when the engine
    is resolved in the lifespan, so the session factory may be bound after
    construction (:meth:`bind`, once). Called unbound it raises
    :class:`~corollary.data.seeds.SeedError` -- the watch routes answer that
    with a 503 naming it -- rather than serve "no seed" for a database it
    never read. It holds a session factory and nothing else, so handing it
    to the context jobs gives them no path to the app.
    """

    def __init__(self, session_factory: Callable[[], Session] | None = None) -> None:
        self._sessions = session_factory

    @property
    def bound(self) -> bool:
        return self._sessions is not None

    def bind(self, session_factory: Callable[[], Session]) -> None:
        if self._sessions is not None:
            raise RuntimeError("the SPDR seed loader is already bound to a database")
        self._sessions = session_factory

    def __call__(self) -> SpdrSeed | None:
        if self._sessions is None:
            raise SeedError(
                "the SPDR seed's database is not bound yet (the app has not started)"
            )
        return load_spdr_seed_from_db(self._sessions)


def _read_seed(session: Session) -> SpdrSeed | None:
    current = session.scalars(
        select(SpdrHoldingsSnapshot)
        .where(SpdrHoldingsSnapshot.status == ACCEPTED)
        .order_by(SpdrHoldingsSnapshot.report_date.desc(), SpdrHoldingsSnapshot.id.desc())
        .limit(1)
    ).first()
    if current is None:
        return None
    rows = session.scalars(
        select(SpdrHoldingRow)
        .where(SpdrHoldingRow.snapshot_id == current.id)
        .order_by(SpdrHoldingRow.etf, SpdrHoldingRow.symbol)
    ).all()
    newer = session.scalar(
        select(func.max(SpdrHoldingsSnapshot.report_date)).where(
            SpdrHoldingsSnapshot.status == REFUSED,
            SpdrHoldingsSnapshot.report_date > current.report_date,
        )
    )
    return SpdrSeed(
        as_of=current.report_date,
        rows=tuple(SpdrHolding(r.etf, r.sector, r.symbol, r.weight) for r in rows),
        filed_date=current.filed_date,
        newer_report_date=newer,
        stale_after_days=NPORT_STALE_AFTER_DAYS,
    )
