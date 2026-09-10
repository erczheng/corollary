"""Column types that keep two CLAUDE.md conventions true on SQLite.

Both exist because SQLite's storage model quietly violates a rule the rest of
the codebase enforces, and does so without erroring.

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
    "Money",
    "MoneyComparisonError",
    "UtcDateTime",
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

#: SQL functions that order or arithmetically combine their argument. A
#: comparator never sees these — ``func.max(col)`` builds a ``Function``
#: without asking the column's type for permission — so they are caught at
#: execution by ``guard_money_sql`` instead.
#:
#: ``count`` is deliberately absent: counting rows asks nothing about the
#: value.
_BLOCKED_SQL_FUNCTIONS = frozenset({"min", "max", "sum", "avg", "total"})

_WHY = (
    "Money is stored as TEXT on SQLite (the only exact-decimal storage SQLite "
    "offers), so SQL compares it lexicographically: '7' > '10' is true, "
    "MAX() of (7, 20, 8, 25, 40) is 8, and ORDER BY gives 20, 25, 40, 7, 8. "
    "Load the rows and compare as Decimal in Python instead — see "
    "corollary.db.seed.risk_limits()."
)


def _describe(element: Any) -> str:
    """``table.column`` where that is knowable, else something honest."""
    table = getattr(getattr(element, "table", None), "name", None)
    key = getattr(element, "key", None)
    if table and key:
        return f"{table}.{key}"
    return str(key or "a Money column")


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
                f"'{symbol}' on {_describe(self.expr)} is not allowed. {_WHY}"
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


def _mentions_money(clause: Any) -> bool:
    if not isinstance(clause, ClauseElement):
        return False
    return any(
        isinstance(getattr(element, "type", None), Money)
        for element in visitors.iterate(clause)
    )


def guard_money_sql(clauseelement: Any) -> None:
    """Refuse a statement that would order or aggregate a ``Money`` column.

    The comparator above catches everything written as an operator, which is
    most of it. Two forms never reach a comparator:

    * ``select(...).order_by(RiskLimit.value)`` — a bare column in ORDER BY is
      not an operator call, so ``asc_op`` never fires. It is also the form
      people write, since ``.asc()`` is the explicit one.
    * ``func.max(RiskLimit.value)`` — a ``Function`` takes the column's type
      without consulting it, then hands the text result back through
      ``process_result_value``, which turns ``'8'`` into ``Decimal('8')``
      without complaint.

    Attached to every Engine in ``session.create_db_engine``, which is the
    only place this codebase builds one.
    """
    if not isinstance(clauseelement, ClauseElement):
        return

    for clause in getattr(clauseelement, "_order_by_clauses", ()):
        if _mentions_money(clause):
            raise MoneyComparisonError(
                f"ORDER BY on {_describe(clause)} is not allowed. {_WHY}"
            )

    for element in visitors.iterate(clauseelement):
        if not isinstance(element, Function):
            continue
        if element.name.lower() not in _BLOCKED_SQL_FUNCTIONS:
            continue
        if _mentions_money(element):
            raise MoneyComparisonError(
                f"{element.name.upper()}() over a Money column is not allowed. {_WHY}"
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
