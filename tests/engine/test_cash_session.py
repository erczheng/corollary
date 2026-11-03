"""The in-memory Cash session holder (rule 5).

The holder is the engine's whole record that a human confirmed Cash. It is
in memory only, so a restart has none -- the rule-5 test here is that a fresh
holder is inactive and that nothing about the holder reaches a disk.
"""

import asyncio
import dataclasses
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from corollary.engine.cash_session import (
    CashSession,
    CashSessionEndedWhileConfirming,
    CashSessionHolder,
    CashSessionReader,
)

AT = datetime(2026, 10, 23, 14, 30, tzinfo=timezone.utc)
BAL = Decimal("99728.08")


@pytest.mark.risk
def test_a_fresh_holder_has_no_session() -> None:
    """Rule 5: every cold start comes up in Paper."""
    holder = CashSessionHolder()
    assert holder.current() is None
    assert holder.active() is False


def test_confirm_then_end() -> None:
    holder = CashSessionHolder()
    previous = holder.confirm(
        account_number_masked="••••1234",
        balance=BAL,
        at=AT,
        correlation_id="c-1",
        generation=holder.generation,
    )
    assert previous is None
    session = holder.current()
    assert session == CashSession(
        confirmed_at=AT,
        account_number_masked="••••1234",
        balance=BAL,
        correlation_id="c-1",
    )
    assert holder.active() is True

    ended = holder.end()
    assert ended == session
    assert holder.current() is None
    assert holder.active() is False
    assert holder.end() is None


def test_a_reconfirm_replaces_and_returns_the_previous() -> None:
    holder = CashSessionHolder()
    holder.confirm(
        account_number_masked="••••1234",
        balance=BAL,
        at=AT,
        correlation_id="c-1",
        generation=holder.generation,
    )
    later = AT + timedelta(minutes=5)
    previous = holder.confirm(
        account_number_masked="••••1234", balance=BAL, at=later,
        correlation_id="c-2",
        generation=holder.generation,
    )
    assert previous is not None and previous.correlation_id == "c-1"
    current = holder.current()
    assert current is not None and current.confirmed_at == later


def test_a_naive_instant_is_refused() -> None:
    holder = CashSessionHolder()
    with pytest.raises(ValueError):
        holder.confirm(
            account_number_masked="••••1234",
            balance=BAL,
            at=datetime(2026, 10, 23, 14, 30),
            correlation_id="c-1",
            generation=holder.generation,
        )
    assert holder.active() is False


@pytest.mark.parametrize("masked", ["", "1234", "PA3ABCD1234", "••••"])
def test_only_a_masked_number_is_accepted(masked: str) -> None:
    """The holder never holds a full account number (rule 6)."""
    holder = CashSessionHolder()
    with pytest.raises(ValueError):
        holder.confirm(
            account_number_masked=masked, balance=BAL, at=AT, correlation_id="c-1",
            generation=holder.generation,
        )
    assert holder.active() is False


def test_the_reader_sees_the_holder_and_cannot_write() -> None:
    holder = CashSessionHolder()
    reader = CashSessionReader(holder)
    assert reader.active() is False
    holder.confirm(
        account_number_masked="••••1234",
        balance=BAL,
        at=AT,
        correlation_id="c-1",
        generation=holder.generation,
    )
    assert reader.active() is True
    assert reader.current() == holder.current()
    holder.end()
    assert reader.active() is False
    for writer in ("confirm", "end"):
        assert not hasattr(reader, writer)


def test_the_session_is_frozen() -> None:
    session = CashSession(
        confirmed_at=AT,
        account_number_masked="••••1234",
        balance=BAL,
        correlation_id="c-1",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        session.confirmed_at = AT + timedelta(days=1)  # type: ignore[misc]


def test_the_module_imports_nothing_a_context_job_may_not() -> None:
    """Unit 2's positions reader is handed a :class:`CashSessionReader`; the
    module defining it must be one the jobs' import scan may walk."""
    import ast
    import inspect

    import corollary.engine.cash_session as module

    tree = ast.parse(inspect.getsource(module))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not [name for name in imported if name.startswith("corollary")], imported


@pytest.mark.parametrize("balance", [Decimal("NaN"), Decimal("Infinity"), 1.5])
def test_a_balance_is_a_finite_decimal(balance: object) -> None:
    holder = CashSessionHolder()
    with pytest.raises(ValueError):
        holder.confirm(
            account_number_masked="••••1234",
            balance=balance,  # type: ignore[arg-type]
            at=AT,
            correlation_id="c-1",
            generation=holder.generation,
        )
    assert holder.active() is False


# --------------------------------------------------------------------------
# The generation: an end() during a confirmation wins (audit M1)
# --------------------------------------------------------------------------


def test_end_bumps_the_generation_whether_or_not_a_session_existed() -> None:
    holder = CashSessionHolder()
    start = holder.generation
    holder.end()
    assert holder.generation == start + 1
    holder.confirm(
        account_number_masked="••••1234",
        balance=BAL,
        at=AT,
        correlation_id="c-1",
        generation=holder.generation,
    )
    # A confirmation does not bump it: only an end can invalidate one.
    assert holder.generation == start + 1
    holder.end()
    assert holder.generation == start + 2


@pytest.mark.risk
def test_a_confirmation_read_before_an_end_is_refused() -> None:
    """Rule 5, fail-off: an end that lands while a confirmation is reading the
    live account must not be undone by that confirmation finishing."""
    holder = CashSessionHolder()
    read_at = holder.generation
    holder.end()
    with pytest.raises(CashSessionEndedWhileConfirming):
        holder.confirm(
            account_number_masked="••••1234",
            balance=BAL,
            at=AT,
            correlation_id="c-1",
            generation=read_at,
        )
    assert holder.active() is False
    assert holder.current() is None


@pytest.mark.risk
def test_a_stale_confirmation_leaves_an_existing_session_ended() -> None:
    holder = CashSessionHolder()
    holder.confirm(
        account_number_masked="••••1234",
        balance=BAL,
        at=AT,
        correlation_id="c-1",
        generation=holder.generation,
    )
    read_at = holder.generation
    holder.end()
    with pytest.raises(CashSessionEndedWhileConfirming):
        holder.confirm(
            account_number_masked="••••1234",
            balance=BAL,
            at=AT,
            correlation_id="c-2",
            generation=read_at,
        )
    assert holder.active() is False


def test_the_holder_carries_one_writer_lock() -> None:
    holder = CashSessionHolder()
    assert isinstance(holder.writer_lock, asyncio.Lock)
    assert holder.writer_lock is holder.writer_lock
    assert CashSessionHolder().writer_lock is not holder.writer_lock
