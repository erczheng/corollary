"""The ``composite_component_value`` store (step 8, U3).

Spec: ``docs/superpowers/specs/2026-11-17-phase-3-step-8-composite.md``, C1
and unit U3. Three functions, and the only code that writes the table:

* :func:`upsert_values` -- writes raw values, **only inside C1's recheck
  window**, all or nothing.
* :func:`load_window` -- every stored row on or after a date, for
  :func:`corollary.data.macro.composite.read_composite`.
* :func:`newest_dates` -- the newest stored session per component.

The recheck window (C1)
-----------------------

For component *c*, the **floor** is the NYSE session :data:`RECHECK_SESSIONS`
sessions before *c*'s newest stored date, counted on the calendar (offset 0
is the newest itself; with 5, the floor and the newest bound six sessions).
A value dated on or after the floor may be inserted or rewritten. A value
dated before it is **frozen** and is refused. A component with no stored
rows has no floor, which is how the empty-table history load works.

The caller passes ``newest_stored`` (what it read before computing), but it
cannot widen the window: the store reads the table's own newest per
component in the same session and anchors each floor on the **later** of the
two. A missing or stale ``newest_stored`` entry can only narrow the window,
never reopen frozen rows. The floor is anchored on what was stored *before*
the batch, so a batch carrying newer sessions does not move its own floor.

Rule 8: a value outside its window is logged at WARNING, one record per
refused value, with rule :data:`RECHECK_WINDOW_RULE` and its inputs
(component, date, newest_stored, table newest, floor). Then the whole batch
is refused with :class:`RecheckWindowRefused`, and **nothing** from it is
written. Never a silent skip.

Decimal
-------

``raw_value`` is a ``Money`` column: TEXT, read back as exact ``Decimal``, and
SQL comparison, ``ORDER BY``, ``MIN``/``MAX`` and arithmetic on it raise. So
nothing here compares or orders ``raw_value`` in SQL; windows and ordering go
by ``date`` only. A value must already be quantized to
:data:`~corollary.data.macro.components.RAW_QUANTUM` (C1: the stored text is
deterministic) and is stored in its quantized form, so ``0.5`` is written as
``0.5000000000``.

Rule 9
------

This module imports only :mod:`corollary.db`, :mod:`corollary.calendars` and
the pure siblings :mod:`corollary.data.macro.components` and
:mod:`corollary.data.macro.composite` -- a test parses the imports. It is used
by the context jobs (U4 to U6) and the read routes (U7), never by the engine
runtime, and it opens no socket.
"""

import logging
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta, timezone
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation, localcontext
from typing import Any, Final

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from corollary.calendars import nyse_session_close, nyse_sessions_between
from corollary.data.macro.components import PRECISION, RAW_QUANTUM, ComponentKey
from corollary.data.macro.composite import ComponentRow
from corollary.db.models import CompositeComponentValue

__all__ = [
    "RECHECK_SESSIONS",
    "RECHECK_WINDOW_RULE",
    "RecheckWindowRefused",
    "RefusedValue",
    "load_window",
    "newest_dates",
    "upsert_values",
]

logger = logging.getLogger(__name__)

#: C1: a run may rewrite each component's rows from this many sessions before
#: its newest stored row onward. Older rows are frozen.
RECHECK_SESSIONS: Final = 5

#: The rule name logged with every value refused for being outside the window.
RECHECK_WINDOW_RULE: Final = "composite_recheck_window"

#: The components C3 computes over the SPDR seed: the only two that carry
#: ``universe_as_of``, and they always do (the table CHECKs the same).
_UNIVERSE_COMPONENTS: Final = frozenset({ComponentKey.STRENGTH, ComponentKey.BREADTH})

#: Rows per INSERT statement. Seven bound parameters a row keeps a statement
#: far below SQLite's bound-parameter limit (999 on old builds) while a
#: two-year history load of all seven components runs to a few thousand rows.
_CHUNK_ROWS: Final = 100


@dataclass(frozen=True)
class RefusedValue:
    """One value refused for being dated before its component's floor."""

    component: ComponentKey
    session: date
    newest_stored: date | None
    table_newest: date | None
    floor: date


class RecheckWindowRefused(ValueError):
    """A batch held a value dated before its component's recheck floor (C1).

    Raised before anything from the batch is written. ``refused`` lists every
    offending value; each was also logged with :data:`RECHECK_WINDOW_RULE`.
    """

    def __init__(self, refused: Sequence[RefusedValue]) -> None:
        self.refused: tuple[RefusedValue, ...] = tuple(refused)
        first = self.refused[0]
        super().__init__(
            f"{len(self.refused)} value(s) dated before the recheck window, nothing "
            f"written; first: {first.component.value} on {first.session}, floor "
            f"{first.floor} ({RECHECK_SESSIONS} sessions before the newest stored row)"
        )


def _require_session(value: date, name: str) -> None:
    # ``datetime`` subclasses ``date``: an instant is not a session date.
    if type(value) is not date:
        raise TypeError(f"{name} must be a date, got {type(value).__name__} {value!r}")
    if nyse_session_close(value) is None:
        raise ValueError(f"{name} {value} is not an NYSE session")


def _floor(anchor: date) -> date:
    """The session :data:`RECHECK_SESSIONS` sessions before ``anchor``, on the calendar."""
    count = RECHECK_SESSIONS + 1
    sessions = nyse_sessions_between(anchor - timedelta(days=2 * count + 14), anchor)
    return sessions[-count]


def _quantized(row: ComponentRow) -> Decimal:
    """``row.raw_value`` in its stored form, or ``ValueError`` if it is not at RAW_QUANTUM."""
    value = row.raw_value
    try:
        with localcontext(prec=PRECISION, rounding=ROUND_HALF_EVEN):
            stored = value.quantize(RAW_QUANTUM)
    except InvalidOperation as exc:
        raise ValueError(
            f"{row.component.value} on {row.session}: raw_value {value} cannot be "
            f"quantized to RAW_QUANTUM {RAW_QUANTUM} in {PRECISION} digits"
        ) from exc
    if stored != value:
        raise ValueError(
            f"{row.component.value} on {row.session}: raw_value {value} is not "
            f"quantized to RAW_QUANTUM {RAW_QUANTUM} (C1); quantize before storing"
        )
    # One text per value: a negative zero is stored as zero.
    return stored.copy_abs() if stored == 0 else stored


def _validate(rows: Sequence[ComponentRow]) -> list[dict[str, Any]]:
    """Every check that needs no database, as the rows the INSERT will carry."""
    seen: set[tuple[date, ComponentKey]] = set()
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, ComponentRow):
            raise TypeError(f"values must be ComponentRow, got {type(row).__name__}")
        _require_session(row.session, "date")
        key = (row.session, row.component)
        if key in seen:
            raise ValueError(
                f"duplicate value in one batch: {row.component.value} on {row.session}"
            )
        seen.add(key)
        if not row.source.strip():
            raise ValueError(f"{row.component.value} on {row.session}: source is blank")
        wants_universe = row.component in _UNIVERSE_COMPONENTS
        if wants_universe != (row.universe_as_of is not None):
            raise ValueError(
                f"{row.component.value} on {row.session}: universe_as_of is set exactly "
                f"for strength and breadth, got {row.universe_as_of!r}"
            )
        out.append(
            {
                "date": row.session,
                "component": row.component.value,
                "raw_value": _quantized(row),
                "source": row.source,
                "members": row.members,
                "universe_as_of": row.universe_as_of,
                "computed_at": row.computed_at.astimezone(timezone.utc),
            }
        )
    return out


def newest_dates(session: Session) -> dict[ComponentKey, date]:
    """The newest stored date per component. A component with no rows is absent."""
    found = session.execute(
        select(CompositeComponentValue.component, func.max(CompositeComponentValue.date))
        .group_by(CompositeComponentValue.component)
    ).all()
    return {ComponentKey(component): newest for component, newest in found}


def upsert_values(
    session: Session,
    values: Iterable[ComponentRow],
    *,
    newest_stored: Mapping[ComponentKey, date],
) -> int:
    """Insert or rewrite ``values``, only inside each component's recheck window. Does not commit.

    ``newest_stored`` is the caller's view of :func:`newest_dates`, read
    before it computed. Each component's floor is anchored on the later of
    that entry and the table's own newest, read here in the same session, so
    the caller can narrow the window but never widen it (module docstring).

    All or nothing. Raises before writing anything:

    * ``TypeError`` -- a value that is not a :class:`ComponentRow`, or a
      ``newest_stored`` key or date of the wrong type.
    * ``ValueError`` -- a date that is not an NYSE session (a value's, or a
      ``newest_stored`` entry's), a duplicate ``(date, component)`` in the
      batch, a ``raw_value`` not already at ``RAW_QUANTUM``, a blank
      ``source``, or ``universe_as_of`` set on the wrong component or
      missing on strength or breadth.
    * :class:`RecheckWindowRefused` (a ``ValueError``) -- any value dated
      before its component's floor, after logging each one with
      :data:`RECHECK_WINDOW_RULE` (rule 8).

    Writes with ``INSERT ... ON CONFLICT DO UPDATE`` on ``(date,
    component)``; ``computed_at`` comes from each row. Returns how many rows
    were written.
    """
    rows = list(values)
    for component, stored in newest_stored.items():
        if type(component) is not ComponentKey:
            raise TypeError(f"newest_stored keys must be ComponentKey, got {component!r}")
        _require_session(stored, f"newest_stored[{component.value}]")
    prepared = _validate(rows)
    if not prepared:
        return 0

    table_newest = newest_dates(session)
    floors: dict[ComponentKey, date | None] = {}
    refused: list[RefusedValue] = []
    for row in rows:
        component = row.component
        if component not in floors:
            anchors = [
                d
                for d in (newest_stored.get(component), table_newest.get(component))
                if d is not None
            ]
            floors[component] = _floor(max(anchors)) if anchors else None
        floor = floors[component]
        if floor is not None and row.session < floor:
            refused.append(
                RefusedValue(
                    component=component,
                    session=row.session,
                    newest_stored=newest_stored.get(component),
                    table_newest=table_newest.get(component),
                    floor=floor,
                )
            )
    if refused:
        for item in refused:
            logger.warning(
                "refused %s on %s: before its recheck floor %s; nothing from the "
                "batch was written",
                item.component.value,
                item.session,
                item.floor,
                extra={
                    "event": "composite_value_refused",
                    "rule": RECHECK_WINDOW_RULE,
                    "component": item.component.value,
                    "date": item.session.isoformat(),
                    "newest_stored": (
                        item.newest_stored.isoformat() if item.newest_stored else None
                    ),
                    "table_newest": (
                        item.table_newest.isoformat() if item.table_newest else None
                    ),
                    "floor": item.floor.isoformat(),
                    "recheck_sessions": RECHECK_SESSIONS,
                    "batch_size": len(rows),
                },
            )
        raise RecheckWindowRefused(refused)

    for start in range(0, len(prepared), _CHUNK_ROWS):
        statement = sqlite_insert(CompositeComponentValue).values(
            prepared[start : start + _CHUNK_ROWS]
        )
        statement = statement.on_conflict_do_update(
            index_elements=[CompositeComponentValue.date, CompositeComponentValue.component],
            set_={
                "raw_value": statement.excluded.raw_value,
                "source": statement.excluded.source,
                "members": statement.excluded.members,
                "universe_as_of": statement.excluded.universe_as_of,
                "computed_at": statement.excluded.computed_at,
            },
        )
        session.execute(statement)
    return len(prepared)


def load_window(
    session: Session,
    components: Iterable[ComponentKey],
    *,
    loaded_from: date,
) -> list[ComponentRow]:
    """Every stored row for ``components`` dated on or after ``loaded_from``, by (date, component).

    **No end cap, and no session count.** This departs from the spec's
    original ``load_window(session, components, end_session, sessions=252)``
    on purpose. :func:`corollary.data.macro.composite.read_composite` takes
    ``loaded_from`` and requires every stored row dated on or after it to be
    in its rows (its module docstring); its ``previous`` window needs 253
    sessions (:func:`~corollary.data.macro.composite.load_start`), and
    :class:`~corollary.data.macro.composite.IncompleteLoad` names the reload
    by a date, ``needed_from``. A 252-session count would silently short the
    load. U7 calls ``load_window(s, ComponentKey,
    loaded_from=load_start(expected_as_of(now)))`` and, on
    ``IncompleteLoad``, again from ``needed_from``.

    ``loaded_from`` need not be a session; it bounds the load without being
    in it. Ordered by ``date`` then the component's stored key -- never by
    ``raw_value``, which ``Money`` refuses to order.
    """
    if type(loaded_from) is not date:
        raise TypeError(
            f"loaded_from must be a date, got {type(loaded_from).__name__} {loaded_from!r}"
        )
    keys = sorted({ComponentKey(component).value for component in components})
    if not keys:
        return []
    found = session.scalars(
        select(CompositeComponentValue)
        .where(CompositeComponentValue.component.in_(keys))
        .where(CompositeComponentValue.date >= loaded_from)
        .order_by(CompositeComponentValue.date, CompositeComponentValue.component)
    ).all()
    return [
        ComponentRow(
            component=ComponentKey(record.component),
            session=record.date,
            raw_value=record.raw_value,
            source=record.source,
            members=record.members,
            universe_as_of=record.universe_as_of,
            computed_at=record.computed_at,
        )
        for record in found
    ]
