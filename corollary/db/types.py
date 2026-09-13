"""Column types that keep three rules true where SQL would quietly break them.

Two of the three exist because SQLite's storage model violates a CLAUDE.md
convention without erroring. The third, ``ActivityId``, is not about storage at
all — the text is exactly what the vendor sent — but about a *sort order* that
looks right and skips rows.

``Money``
    CLAUDE.md: "Money as ``Decimal``, never ``float``. SQLAlchemy columns are
    ``Numeric``, not ``Float``, or the rule leaks at the database boundary."
    On SQLite that boundary is precisely where it leaks. SQLAlchemy's own
    ``Numeric`` emits ``SAWarning: Dialect sqlite+pysqlite does *not* support
    Decimal objects natively, and SQLAlchemy must convert from floating
    point`` — so a plain ``Numeric`` column round-trips every value through
    an IEEE double. Worse, SQLite applies NUMERIC *affinity* to the declared
    type, converting the text ``'7.5'`` to a REAL on the way in, so even
    binding a string would not save it.

    ``Money`` therefore stores the value as TEXT and converts back to
    ``Decimal`` on the way out. That is stricter than ``Numeric``, not looser:
    no float exists anywhere on the path. It is the one deliberate deviation
    from the letter of "columns are ``Numeric``", made to keep the intent.
    ``tests/db/test_models.py`` asserts on the *type* that comes back, since
    a float that compares equal is still the bug.

    **TEXT storage buys exactness and costs ordering, and the cost is the
    dangerous half.** SQLite compares TEXT lexicographically, so against the
    five seeded ceilings (7, 20, 8, 25, 40) the database answers
    ``MAX(value)`` with 8, ``MIN(value)`` with 20, ``WHERE value > '10'``
    with all five rows, and ``ORDER BY value`` with 20, 25, 40, 7, 8. Not one
    of those is an error; every one is a plausible number. The failure that
    matters is Phase 6's::

        select(RiskLimit).where(RiskLimit.value < computed_risk)

    which approves a 35%-of-account trade against a 40% ceiling and reports
    the 7% per-trade limit as unbreached, because ``'7' > '10'`` is true as
    text. So ``Money`` refuses to answer: the comparator raises
    ``MoneyComparisonError`` at the call site, and ``guard_money_sql``
    catches the two forms a comparator cannot see — a bare ``ORDER BY`` and
    an aggregate function. Load the rows and compare as ``Decimal`` in
    Python; ``corollary.db.seed.risk_limits`` is that read.

``ActivityId``
    Design decision 13: **the ingestion cursor is never ``MAX(activity_id)``.**
    A broker activity id is composite — a 17-digit stamp, ``::``, a UUID —
    and the shape is uniform across every activity type, which is exactly what
    makes it look orderable::

        FILL   20260910131125217::a9d576c2-…     real time, 13:11:25.217
        FEE    20260910000000000::a2a0c406-…     zeroed
        JNLC   20260805000000000::4b47d1d0-…     zeroed

    **Non-trade rows carry a date-only id with the time zeroed out** — every
    ``FEE``, every journal, and every ``OPEXP``, ``OPEXC`` and ``OPASN``. So
    within any one day all of them sort *below* every fill of that day. A
    cursor taken as ``MAX(activity_id)`` lands on the day's last **fill**, with
    that day's expiry and assignment rows sitting under it; the next pull asks
    for everything newer and never sees them again — not late, not duplicated,
    gone. Missing rows become missing realized trades become a wrong lifetime
    P&L, with nothing anywhere saying so. The first symptom is a terminal that
    believes you never win.

    There is **no carve-out for ``MAX``**. An earlier draft proposed one on the
    grounds that it is a legitimate resume cursor; it is precisely the
    opposite, and that is the whole reason this type exists. The guard costs a
    few lines of Python where one line of SQL would have fitted, and buys a
    resume point that cannot silently skip.

    Ordering is refused for a second, independent reason too: even ignoring
    the zeroed ids, the composite does not sort chronologically. Only the
    17-digit stamp half orders, **stamps repeat** (a stamp is a millisecond
    and two legs of one spread fill inside one), and the UUID half then breaks
    the tie arbitrarily. On the recorded account the first pair it inverts is
    the two legs of one vertical — wrong lot order into the FIFO matcher,
    wrong ``open_price`` out of it.

    Unlike ``Money`` this type changes nothing about storage: it is a
    ``TypeDecorator`` over ``String`` with no bind or result processing, so
    every dialect emits the DDL it emitted before and no migration is owed.
    All it adds is a comparator that refuses, plus ``guard_activity_id_sql``
    for the two forms a comparator never sees.

``UtcDateTime``
    CLAUDE.md: "All timestamps stored UTC." SQLite's DATETIME has no offset,
    and SQLAlchemy's SQLite dialect silently *drops* ``tzinfo`` when binding,
    so a ``datetime`` in ``America/New_York`` would be stored as an Eastern
    wall clock labelled nothing at all and read back as if it were UTC — a
    four- or five-hour error with no symptom. This type converts to UTC on
    the way in, refuses naive values outright, and re-attaches UTC on the way
    out.
"""

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import DateTime, Dialect, Numeric, String
from sqlalchemy.sql import operators as sa_operators
from sqlalchemy.sql import visitors
from sqlalchemy.sql.elements import ClauseElement
from sqlalchemy.sql.functions import Function
from sqlalchemy.types import TypeDecorator, TypeEngine

__all__ = [
    "ActivityId",
    "ActivityIdComparisonError",
    "Money",
    "MoneyComparisonError",
    "UtcDateTime",
    "guard_activity_id_sql",
    "guard_money_sql",
]

#: Wide enough for any percentage, dollar figure or count this app stores,
#: with the sign and the point. TEXT on SQLite has no fixed width anyway; the
#: length is here so a future non-SQLite dialect gets a sane VARCHAR.
_MONEY_TEXT_WIDTH = 40


class MoneyComparisonError(Exception):
    """A SQL operation on a ``Money`` column that would answer wrongly.

    Deliberately not a subclass of ``TypeError`` or ``ValueError``: this must
    not be swallowed by an ``except (TypeError, ValueError)`` written for the
    parsing around it. The whole value of the error is that it stops the
    process.
    """


class ActivityIdComparisonError(Exception):
    """A SQL operation on an ``ActivityId`` column that would skip rows.

    Not a subclass of ``MoneyComparisonError`` and not sharing a base with it,
    for the same reason neither subclasses ``ValueError``: the two failures
    have different remedies and an ``except`` written for one must not swallow
    the other. ``Money``'s remedy is "load the rows and compare as Decimal";
    this one's is "resume from the vendor's ``since_id``, sequence by
    ``Fill.at``".
    """


#: Every operator SQLite would evaluate against the stored *text*. The values
#: are what the operator looks like in the source, so the message names what
#: the caller actually typed.
#:
#: Equality is in the list, which is stricter than the ordering operators
#: strictly require: ``Decimal('7') == Decimal('7.0')`` is true, but ``'7'``
#: and ``'7.0'`` are different strings, so ``WHERE value = :v`` silently
#: misses rows. That is the same class of bug in a quieter coat.
#:
#: Arithmetic is in the list because SQLite coerces text to a number to
#: evaluate it, producing exactly the IEEE double this type exists to keep
#: out.
_BLOCKED_OPERATORS: dict[Any, str] = {
    sa_operators.lt: "<",
    sa_operators.le: "<=",
    sa_operators.gt: ">",
    sa_operators.ge: ">=",
    sa_operators.eq: "==",
    sa_operators.ne: "!=",
    sa_operators.between_op: "between()",
    sa_operators.in_op: "in_()",
    sa_operators.not_in_op: "not_in()",
    sa_operators.asc_op: "asc()",
    sa_operators.desc_op: "desc()",
    sa_operators.add: "+",
    sa_operators.sub: "-",
    sa_operators.mul: "*",
    sa_operators.truediv: "/",
    sa_operators.mod: "%",
    sa_operators.neg: "-",
}

#: Every operator SQLite would evaluate against an activity id's *text*.
#:
#: The same ordering and comparison set as ``_BLOCKED_OPERATORS``, minus the
#: arithmetic. Decision 13's rule is "ordering, comparison and aggregation",
#: and on a ``String`` column SQLAlchemy routes ``+`` to ``concat_op`` rather
#: than ``add`` anyway — concatenating two ids is neither an ordering nor a
#: comparison, and blocking it would be inventing policy nobody decided.
#:
#: **Equality is in the list, and not for ``Money``'s reason.** ``'7'`` and
#: ``'7.0'`` spell one number two ways, so ``WHERE value = :v`` silently
#: misses rows; an activity id has exactly one spelling and a point lookup
#: would answer correctly. It is blocked anyway, for two reasons. First,
#: ``== cursor`` and ``>= cursor`` differ by one character on the same column,
#: and a guard that permits the first teaches that the column is ordinarily
#: comparable — the person who writes ``==`` today writes ``>=`` tomorrow
#: because the first one worked. Second, nothing needs it: idempotency is
#: ``uq_fill_activity_id``, which is DDL and untouchable by a comparator, plus
#: a Python dict keyed on the id in ``IngestService._write_fills``. Verified
#: before blocking — no SQL equality on ``activity_id`` exists in this
#: codebase. If one is ever genuinely needed, that is a decision to reopen out
#: loud, not a carve-out to add quietly.
_BLOCKED_ACTIVITY_ID_OPERATORS: dict[Any, str] = {
    sa_operators.lt: "<",
    sa_operators.le: "<=",
    sa_operators.gt: ">",
    sa_operators.ge: ">=",
    sa_operators.eq: "==",
    sa_operators.ne: "!=",
    sa_operators.between_op: "between()",
    sa_operators.in_op: "in_()",
    sa_operators.not_in_op: "not_in()",
    sa_operators.asc_op: "asc()",
    sa_operators.desc_op: "desc()",
}

#: SQL functions that order or arithmetically combine their argument. A
#: comparator never sees these — ``func.max(col)`` builds a ``Function``
#: without asking the column's type for permission — so they are caught at
#: execution by ``guard_money_sql`` / ``guard_activity_id_sql`` instead.
#:
#: ``count`` is deliberately absent: counting rows asks nothing about the
#: value.
_BLOCKED_SQL_FUNCTIONS = frozenset({"min", "max", "sum", "avg", "total"})

_MONEY_WHY = (
    "Money is stored as TEXT on SQLite (the only exact-decimal storage SQLite "
    "offers), so SQL compares it lexicographically: '7' > '10' is true, "
    "MAX() of (7, 20, 8, 25, 40) is 8, and ORDER BY gives 20, 25, 40, 7, 8. "
    "Load the rows and compare as Decimal in Python instead — see "
    "corollary.db.seed.risk_limits()."
)

_ACTIVITY_ID_WHY = (
    "Design decision 13: the ingestion cursor is never MAX(activity_id). An "
    "activity id is a 17-digit stamp, '::', a UUID — and non-trade rows carry "
    "a date-only stamp with the time zeroed (FEE, JNLC, OPEXP, OPEXC, OPASN: "
    "20260910000000000::...), so within a day every one of them sorts BELOW "
    "every fill of that day. A cursor taken as MAX() lands on the day's last "
    "fill and the next pull never sees those rows again — missing expiries "
    "and assignments become missing realized trades and a wrong lifetime P&L, "
    "silently. Nor is the id chronological at all: stamps repeat and the UUID "
    "half then breaks the tie arbitrarily, inverting the two legs of one "
    "vertical. Resume from the vendor's own since_id page token "
    "(corollary.engine.ingest), and sequence rows by Fill.at per symbol "
    "(ix_fill_account_at, ix_fill_symbol)."
)


def _describe(element: Any, fallback: str) -> str:
    """``table.column`` where that is knowable, else something honest."""
    table = getattr(getattr(element, "table", None), "name", None)
    key = getattr(element, "key", None)
    if table and key:
        return f"{table}.{key}"
    return str(key or fallback)


class Money(TypeDecorator[Decimal]):
    """An exact decimal. No float ever touches it, and SQL never orders it.

    Storage is TEXT on SQLite and NUMERIC elsewhere. Binding a ``float``
    raises rather than rounding: the caller has already lost precision by the
    time the value reaches here, and swallowing that is how a max-loss figure
    ends up a cent off in the direction nobody checks.

    The comparator refuses every operator SQLite would evaluate against the
    stored text. It refuses them on *every* dialect rather than only on
    SQLite, because the comparison is built long before a connection exists
    and a rule that applies sometimes is a rule nobody can rely on. See the
    ``load_dialect_impl`` note on the non-SQLite branch.
    """

    impl = Numeric
    cache_ok = True

    class comparator_factory(TypeDecorator.Comparator[Decimal]):  # noqa: N801
        """Raises instead of building a comparison SQLite would answer wrongly."""

        def _refuse(self, op: Any, other: tuple[Any, ...]) -> None:
            symbol = _BLOCKED_OPERATORS.get(op)
            if symbol is None:
                return
            # ``col == None`` and ``col != None`` compile to IS NULL / IS NOT
            # NULL, which ask nothing about the representation of a value.
            if op in (sa_operators.eq, sa_operators.ne) and other and other[0] is None:
                return
            raise MoneyComparisonError(
                f"'{symbol}' on {_describe(self.expr, 'a Money column')} is not "
                f"allowed. {_MONEY_WHY}"
            )

        def operate(self, op: Any, *other: Any, **kwargs: Any) -> Any:
            self._refuse(op, other)
            return super().operate(op, *other, **kwargs)

        def reverse_operate(self, op: Any, other: Any, **kwargs: Any) -> Any:
            self._refuse(op, (other,))
            return super().reverse_operate(op, other, **kwargs)

    def load_dialect_impl(self, dialect: Dialect) -> TypeEngine[Any]:
        if dialect.name == "sqlite":
            return dialect.type_descriptor(String(_MONEY_TEXT_WIDTH))
        # ---------------------------------------------------------------- #
        # Aspirational, not covered. Nothing in this project runs on a
        # non-SQLite dialect: the design spec chose SQLite with one writer,
        # and no test opens a Postgres or MySQL connection, so this branch
        # has never executed against a real server. It is kept because the
        # type must remain portable in principle, not because it is known to
        # work.
        #
        # It is also known to *diverge*. NUMERIC(20, 8) quantizes, so
        # Decimal('7') comes back as Decimal('7.00000000') — equal, but not
        # the same object the SQLite branch returns, and
        # ``test_risk_limit_value_round_trips_as_decimal`` asserts on str().
        # ``test_the_non_sqlite_branch_is_aspirational`` pins the divergence
        # rather than hiding it.
        #
        # A real port needs three things, none of which are done here:
        # a running server in the test matrix, a decision on that quantize,
        # and a decision on the comparator above — on a dialect with true
        # NUMERIC the lexicographic problem does not exist and refusing ``<``
        # would be pure cost.
        # ---------------------------------------------------------------- #
        return dialect.type_descriptor(Numeric(20, 8))

    def process_bind_param(self, value: Decimal | None, dialect: Dialect) -> Any:
        if value is None:
            return None
        if not isinstance(value, Decimal):
            raise TypeError(
                "money must be a Decimal, got "
                f"{type(value).__name__} ({value!r}) — see CLAUDE.md Conventions"
            )
        if dialect.name == "sqlite":
            # format(..., "f") rather than str(): str(Decimal('1E+2')) is
            # '1E+2', so the same number would have two spellings in the
            # column depending on how it was constructed. One canonical
            # spelling per value is the least a text column owes its
            # CHECK constraints and anyone reading the file with sqlite3.
            return format(value, "f")
        return value

    def process_result_value(self, value: Any, dialect: Dialect) -> Decimal | None:
        if value is None:
            return None
        if isinstance(value, Decimal):
            return value
        if isinstance(value, str):
            return Decimal(value)
        raise TypeError(
            f"money came back from the database as {type(value).__name__} "
            f"({value!r}); it must be an exact decimal"
        )


class ActivityId(TypeDecorator[str]):
    """A broker activity id. Stored as text; never ordered, compared or summed.

    **A ``String`` and nothing more, on purpose.** There is no
    ``load_dialect_impl``, no ``process_bind_param`` and no
    ``process_result_value``, so the DDL and the stored bytes are exactly what
    ``String(n)`` produced before — which is what lets ``fill.activity_id``
    adopt this type without a migration. The only thing added is refusal.

    It does not validate the id's shape either. ``<17-digit stamp>::<uuid>`` is
    what the vendor sends, but ``corollary.engine.ledger`` already handles an
    id with no separator at all, and a type that rejected one would turn a
    tolerable oddity into a write failure.

    See the module docstring for why ordering must raise, and
    ``_BLOCKED_ACTIVITY_ID_OPERATORS`` for why equality is in the list even
    though an id has exactly one spelling.
    """

    impl = String
    cache_ok = True

    class comparator_factory(TypeDecorator.Comparator[str]):  # noqa: N801
        """Raises instead of building a comparison that would skip rows."""

        def _refuse(self, op: Any, other: tuple[Any, ...]) -> None:
            symbol = _BLOCKED_ACTIVITY_ID_OPERATORS.get(op)
            if symbol is None:
                return
            # ``col == None`` and ``col != None`` compile to IS NULL / IS NOT
            # NULL, which ask nothing about the value. Moot on a NOT NULL
            # column, kept so this comparator reads identically to ``Money``'s.
            if op in (sa_operators.eq, sa_operators.ne) and other and other[0] is None:
                return
            raise ActivityIdComparisonError(
                f"'{symbol}' on {_describe(self.expr, 'an activity id column')} "
                f"is not allowed. {_ACTIVITY_ID_WHY}"
            )

        def operate(self, op: Any, *other: Any, **kwargs: Any) -> Any:
            self._refuse(op, other)
            return super().operate(op, *other, **kwargs)

        def reverse_operate(self, op: Any, other: Any, **kwargs: Any) -> Any:
            self._refuse(op, (other,))
            return super().reverse_operate(op, other, **kwargs)


def _mentions(clause: Any, column_type: type[TypeEngine[Any]]) -> bool:
    if not isinstance(clause, ClauseElement):
        return False
    return any(
        isinstance(getattr(element, "type", None), column_type)
        for element in visitors.iterate(clause)
    )


def _refuse_ordering_and_aggregation(
    clauseelement: Any,
    *,
    column_type: type[TypeEngine[Any]],
    error: type[Exception],
    noun: str,
    why: str,
) -> None:
    """The two forms a comparator never sees, for one guarded column type.

    * ``select(...).order_by(RiskLimit.value)`` — a bare column in ORDER BY is
      not an operator call, so ``asc_op`` never fires. It is also the form
      people write, since ``.asc()`` is the explicit one.
    * ``func.max(RiskLimit.value)`` — a ``Function`` takes the column's type
      without consulting it, then hands the result straight back.

    One walk of the clause tree per guarded type. Parameterised rather than
    duplicated because getting *this* traversal subtly wrong in one of two
    copies is how a guard ends up half-attached.
    """
    if not isinstance(clauseelement, ClauseElement):
        return

    for clause in getattr(clauseelement, "_order_by_clauses", ()):
        if _mentions(clause, column_type):
            raise error(f"ORDER BY on {_describe(clause, noun)} is not allowed. {why}")

    for element in visitors.iterate(clauseelement):
        if not isinstance(element, Function):
            continue
        if element.name.lower() not in _BLOCKED_SQL_FUNCTIONS:
            continue
        if _mentions(element, column_type):
            raise error(f"{element.name.upper()}() over {noun} is not allowed. {why}")


def guard_money_sql(clauseelement: Any) -> None:
    """Refuse a statement that would order or aggregate a ``Money`` column.

    The comparator catches everything written as an operator, which is most of
    it; a bare ``ORDER BY`` and ``func.max()`` are caught here. ``MAX(value)``
    over the five seeded ceilings answers ``'8'``, and
    ``process_result_value`` turns that into ``Decimal('8')`` without
    complaint.

    Attached to every Engine in ``session.create_db_engine``, which is the
    only place this codebase builds one.
    """
    _refuse_ordering_and_aggregation(
        clauseelement,
        column_type=Money,
        error=MoneyComparisonError,
        noun="a Money column",
        why=_MONEY_WHY,
    )


def guard_activity_id_sql(clauseelement: Any) -> None:
    """Refuse a statement that would order or aggregate an ``ActivityId``.

    ``MAX(activity_id)`` is the one this exists for, and it is caught here
    rather than by the comparator because ``func.max(Fill.activity_id)`` never
    asks the column's type for permission. There is **no carve-out for
    ``MAX``** — see the module docstring.

    Attached to every Engine in ``session.create_db_engine`` alongside
    ``guard_money_sql``.
    """
    _refuse_ordering_and_aggregation(
        clauseelement,
        column_type=ActivityId,
        error=ActivityIdComparisonError,
        noun="an activity id column",
        why=_ACTIVITY_ID_WHY,
    )


class UtcDateTime(TypeDecorator[datetime]):
    """A timezone-aware instant, stored UTC and returned UTC.

    Naive datetimes are refused. There is no defensible reading of one here:
    market data is Eastern, the server clock is whatever the machine says, and
    guessing between them is a silent multi-hour error.
    """

    impl = DateTime
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> Any:
        if value is None:
            return None
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError(
                "timestamps must be timezone-aware; got a naive datetime "
                f"({value!r}). Storage is UTC — see CLAUDE.md Conventions"
            )
        return value.astimezone(timezone.utc).replace(tzinfo=None)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if not isinstance(value, datetime):
            raise TypeError(
                f"expected a datetime from the database, got {type(value).__name__}"
            )
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
