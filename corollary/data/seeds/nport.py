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
  with its fund, name, ISIN and weight and skipped: **fail closed until the
  owner picks an ISIN source**. On the real 2026-06-30 quarter that leaves
  XLB at 73.36 and the whole snapshot is refused on the weight band -- the
  band is not lowered to let it through. Class shares are in the dot form
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
* **An NPORT-P/A refuses the snapshot** (parent-session assumption, fail
  closed). :attr:`~corollary.data.providers.sec.SectorSelection.amended` is
  derived from the filing index, so an amendment the index lists but that
  cannot be attributed to a series flags every fund -- an unattributed P/A
  anywhere in the trust therefore blocks the update. Every indexed
  amendment's document is fetched precisely so that a Premium Income fund's
  amendment can be attributed away and does not.
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
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Final, Literal, Protocol

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from corollary.data.providers.interface import AssetDirectory, EquityAsset, ProviderError
from corollary.data.providers.sec import (
    NportDocument,
    NportFiling,
    NportFilings,
    SecAccessRefused,
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


#: ISO 6166: two-letter country, nine alphanumerics, one check digit. A line
#: whose ISIN is not this shape is treated as having none.
ISIN_RE: Final = re.compile(r"[A-Z]{2}[A-Z0-9]{9}[0-9]")


class IsinResolver(Protocol):
    """The seam for EC lines N-PORT filed with no CUSIP, only an ISIN.

    ``resolve`` answers a ticker for ``isin``, ``None`` for "cannot say"
    (the line is then logged and skipped, never guessed), or raises
    :class:`~corollary.data.providers.interface.ProviderError` when its
    source failed (the build aborts and stores nothing; the next run
    retries). ``fund`` is the first fund carrying that ISIN in fund order,
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


class NoIsinResolver:
    """The default :class:`IsinResolver`: resolves nothing, so every ISIN-only line is skipped.

    This is the fail-closed state until the owner chooses an ISIN source.
    """

    async def resolve(self, isin: str, *, fund: str) -> str | None:
        return None


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


# ------------------------------------------------------------------ the build


class _Abort(Exception):
    def __init__(self, rule: SnapshotRule, reason: str) -> None:
        super().__init__(reason)
        self.rule = rule
        self.reason = reason


async def build_spdr_snapshot(
    sec: NportSource,
    resolver: CusipResolver,
    session_factory: Callable[[], Session],
    *,
    isin_resolver: IsinResolver | None = None,
    directory: AssetDirectory | None = None,
    clock: Callable[[], datetime] = _utcnow,
) -> SnapshotOutcome:
    """Build, validate and store one snapshot from the latest N-PORT quarter.

    ``isin_resolver`` answers the EC lines with no CUSIP; ``None`` means
    :class:`NoIsinResolver` (resolve nothing). ``directory``, when given,
    must list every ticker an ISIN resolved to.

    Returns ``accepted`` (stored, now current), ``refused`` (recorded with
    its rule, the previous stays current), ``unchanged`` (the loaded
    snapshot is already this quarter's or newer; nothing fetched beyond the
    index) or ``aborted`` (nothing stored). Never raises for a vendor
    failure; a programming error still propagates.
    """
    try:
        return await _build(
            sec, resolver, isin_resolver or NoIsinResolver(), directory, session_factory, clock
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
) -> SnapshotOutcome:
    try:
        series = await sec.sector_fund_series()
        filings = await sec.latest_nport_filings()
    except SecAccessRefused as exc:
        raise _Abort(SnapshotRule.SEC_ACCESS_REFUSED, str(exc)) from None
    except SecError as exc:
        raise _Abort(SnapshotRule.SEC_UNAVAILABLE, str(exc)) from None

    current = _current_accepted(session_factory)
    if current is not None and filings.report_date <= current.report_date:
        if filings.amendments:
            logger.warning(
                "spdr snapshot: %d NPORT-P/A filed for %s, already loaded from the originals; "
                "the loaded snapshot stays",
                len(filings.amendments),
                filings.report_date.isoformat(),
                extra={
                    "event": "spdr_snapshot_amendment_after_load",
                    "rule": SnapshotRule.AMENDMENT.value,
                    "report_date": filings.report_date.isoformat(),
                    "accessions": [f.accession for f in filings.amendments],
                },
            )
        return SnapshotOutcome(status="unchanged", report_date=current.report_date)

    documents: list[tuple[NportFiling, NportDocument]] = []
    for filing in filings.filings:
        try:
            documents.append((filing, await sec.nport_holdings(filing.accession)))
        except SecAccessRefused as exc:
            raise _Abort(SnapshotRule.SEC_ACCESS_REFUSED, str(exc)) from None
        except SecError as exc:
            raise _Abort(SnapshotRule.SEC_UNAVAILABLE, str(exc)) from None

    index_filed = max((f.filing_date for f in filings.originals), default=filings.report_date)
    try:
        selection = select_sector_documents(series, documents, filings=filings)
    except SecError as exc:
        rule = selection_failure_rule(series, documents, filings) or SnapshotRule.SELECTION_FAILED
        return _refuse(session_factory, clock, filings.report_date, index_filed,
                       rule, str(exc), ())
    filed = max(fund.filing.filing_date for fund in selection.funds.values())
    if selection.amended:
        unattributed = [f.accession for f in selection.unattributed_amendments]
        reason = (
            f"NPORT-P/A filed for {', '.join(selection.amended)}"
            + (f" (unattributed: {', '.join(unattributed)})" if unattributed else "")
        )
        return _refuse(session_factory, clock, filings.report_date, filed,
                       SnapshotRule.AMENDMENT, reason, ())

    holdings, skipped = await _resolve(selection.funds, resolver, isin_resolver, directory)
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
               "at": built_at.isoformat()},
    )
    return SnapshotOutcome(
        status="accepted", report_date=filings.report_date, snapshot_id=snapshot_id,
        holdings=holdings, skipped=skipped, seed=seed,
    )


async def _resolve(
    funds: Mapping[str, SectorFund],
    resolver: CusipResolver,
    isin_resolver: IsinResolver,
    directory: AssetDirectory | None,
) -> tuple[dict[str, tuple[ResolvedHolding, ...]], tuple[SkippedLine, ...]]:
    """Resolve every EC line of the selected funds. Raises :class:`_Abort` on a lookup error.

    A line with a CUSIP is asked of the broker; a line with none but a
    well-formed ISIN is asked of ``isin_resolver``; anything else is
    skipped. Every skip is logged with fund, name, CUSIP, ISIN, weight and
    rule. Nothing is ever resolved by name.
    """
    cusip_answers: dict[str, EquityAsset | None] = {}
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
                    try:
                        cusip_answers[line.cusip] = await resolver.asset_by_cusip(line.cusip)
                    except ProviderError as exc:
                        raise _Abort(
                            SnapshotRule.CUSIP_LOOKUP_FAILED,
                            f"{etf} {line.name[:80]} CUSIP {line.cusip}: {exc}",
                        ) from None
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
