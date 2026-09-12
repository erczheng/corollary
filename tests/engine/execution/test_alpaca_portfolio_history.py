"""``AlpacaBroker.portfolio_history()`` -- the one trading endpoint that sends
money as bare JSON numbers.

Everything else on ``paper-api.alpaca.markets`` returns money as a string
(``"avg_entry_price": "8.21"``), which is the easy case: it parses straight to
``Decimal`` and no float can exist on the path. ``GET
/v2/account/portfolio/history`` does not -- ``"base_value": 8413.04`` and
``"equity": [8425.21, ...]`` are JSON numbers, so a parser written on the
entirely reasonable belief that *"the trading API sends strings"* would put
the whole equity curve through doubles and nothing would say so.

``corollary.wire.decode_json`` is the only sanctioned entry point for a
response body for exactly this reason, and ``as_decimal`` **raises** on a
``float`` rather than converting one. The first test below is the one that
would fail if ``httpx``'s ``.json()`` were ever used here: it carries more
significant figures than a double holds, so the value cannot survive the wrong
path even by luck.

The parallel-array shape is the second trap. ``timestamp``, ``equity``,
``profit_loss`` and ``profit_loss_pct`` are four separate arrays that are only
a time series because their indices line up; a length mismatch is a silent
off-by-one across the whole chart, so it raises here rather than zipping
short.
"""

import logging
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from corollary.engine.execution.interface import BrokerError

from .conftest import load_fixture, single

#: Twenty-three significant figures. A double holds about seventeen, so a
#: value that comes back exact cannot have been through one.
BEYOND_A_DOUBLE = "8425.2100000000000000001"

UNDOUBLEABLE_HISTORY = (
    '{"timestamp": [1786492800], "equity": [' + BEYOND_A_DOUBLE + "],"
    ' "profit_loss": [0], "profit_loss_pct": [0], "base_value": '
    + BEYOND_A_DOUBLE
    + ', "timeframe": "1D"}'
)


@pytest.mark.asyncio
async def test_a_value_no_double_can_hold_survives(make_broker) -> None:
    """The tripwire for the whole arrangement.

    If this file ever reaches ``response.json()`` instead of
    ``wire.decode_json``, the equity below comes back as
    ``8425.209999999999`` and this is the assertion that notices.
    """
    broker, _ = make_broker(lambda _request: (200, UNDOUBLEABLE_HISTORY))
    history = await broker.portfolio_history()

    assert history.base_value == Decimal(BEYOND_A_DOUBLE)
    assert history.points[0].equity == Decimal(BEYOND_A_DOUBLE)
    assert isinstance(history.points[0].equity, Decimal)


@pytest.mark.asyncio
async def test_the_recorded_curve_parses_exactly(make_broker) -> None:
    broker, _ = make_broker(single("portfolio_history"))
    history = await broker.portfolio_history()
    raw = load_fixture("portfolio_history")["body"]

    assert history.timeframe == "1D"
    assert history.base_value == Decimal("100000.000000")
    assert history.base_value_asof == date(2026, 8, 10)
    assert len(history.points) == len(raw["timestamp"]) == 22

    last = history.points[-1]
    assert last.equity == Decimal("99839.080000")
    assert last.profit_loss == Decimal("-160.920000")
    assert last.profit_loss_pct == Decimal("-0.001600")


@pytest.mark.asyncio
async def test_every_point_on_the_curve_is_decimal(make_broker) -> None:
    broker, _ = make_broker(single("portfolio_history"))
    history = await broker.portfolio_history()
    for point in history.points:
        for field in ("equity", "profit_loss", "profit_loss_pct"):
            value = getattr(point, field)
            assert value is None or isinstance(value, Decimal), field


@pytest.mark.asyncio
async def test_epoch_timestamps_become_aware_utc_datetimes(make_broker) -> None:
    """Left-labelled UNIX epoch seconds. Stored UTC, displayed Eastern."""
    broker, _ = make_broker(single("portfolio_history"))
    history = await broker.portfolio_history()

    assert history.points[0].at == datetime(2026, 8, 12, tzinfo=timezone.utc)
    assert history.points[-1].at == datetime(2026, 9, 11, tzinfo=timezone.utc)
    for point in history.points:
        assert point.at.tzinfo is not None
        assert point.at.utcoffset() == timezone.utc.utcoffset(None)


@pytest.mark.asyncio
async def test_the_cashflow_map_is_decimal_per_bucket(make_broker) -> None:
    """``cashflow_types=ALL``. The 92 cents of fees this account has paid."""
    broker, _ = make_broker(single("portfolio_history"))
    history = await broker.portfolio_history()

    assert set(history.cashflow) == {"FEE"}
    fees = history.cashflow["FEE"]
    assert len(fees) == 22
    assert all(value is None or isinstance(value, Decimal) for value in fees)
    assert fees[-1] == Decimal("-0.920000")


@pytest.mark.asyncio
async def test_a_null_equity_point_stays_null(make_broker) -> None:
    """``equity`` items are documented ``["number", "null"]``.

    A gap in the curve is a gap. Coercing it to zero would draw a line to the
    x-axis and assert the account was worth nothing that day.
    """
    body = (
        '{"timestamp": [1786492800, 1786579200], "equity": [100.5, null],'
        ' "profit_loss": [0, null], "profit_loss_pct": [0, null],'
        ' "base_value": 100.5, "timeframe": "1D"}'
    )
    broker, _ = make_broker(lambda _request: (200, body))
    history = await broker.portfolio_history()

    assert history.points[0].equity == Decimal("100.5")
    assert history.points[1].equity is None
    assert history.points[1].profit_loss is None


@pytest.mark.asyncio
async def test_mismatched_parallel_arrays_raise(make_broker) -> None:
    """Four arrays are a time series only because their indices line up."""
    body = (
        '{"timestamp": [1, 2, 3], "equity": [1, 2], "profit_loss": [1, 2, 3],'
        ' "profit_loss_pct": [1, 2, 3], "base_value": 1, "timeframe": "1D"}'
    )
    broker, _ = make_broker(lambda _request: (200, body))
    with pytest.raises(BrokerError) as raised:
        await broker.portfolio_history()
    assert "equity" in str(raised.value)


@pytest.mark.asyncio
async def test_an_absent_base_value_is_none_not_zero(make_broker) -> None:
    """Documented ``["number", "null"]``. A new account has no basis."""
    body = (
        '{"timestamp": [1786492800], "equity": [100], "profit_loss": [0],'
        ' "profit_loss_pct": [0], "base_value": null, "timeframe": "1D"}'
    )
    broker, _ = make_broker(lambda _request: (200, body))
    history = await broker.portfolio_history()
    assert history.base_value is None
    assert history.base_value_asof is None


@pytest.mark.asyncio
async def test_the_period_and_timeframe_are_sent(make_broker) -> None:
    broker, transport = make_broker(single("portfolio_history"))
    await broker.portfolio_history(period="3M", timeframe="1D")
    params = transport.params_for("/v2/account/portfolio/history")
    assert params["period"] == "3M"
    assert params["timeframe"] == "1D"


# --------------------------------------------------------------------------
# The cashflow map -- the *fifth* array, and the one that used to go unchecked
# --------------------------------------------------------------------------
#
# `PortfolioHistory.cashflow` is documented as "aligned index-for-index with
# `points`", and Alpaca documents each bucket as "accumulated value in dollar
# amount as of the end of each time window" -- one value per window, same as
# `equity`. The parser checked the other three arrays against `timestamp` and
# passed this one straight through, so a bucket of the wrong length was
# accepted silently and a consumer zipping it with `points` to date a fee or a
# deposit dated it to the wrong day.
#
# A mismatch **drops that bucket** rather than raising, which is the one place
# this endpoint differs from the three required arrays:
#
# * `equity`, `profit_loss` and `profit_loss_pct` are `required` in Alpaca's
#   schema and *are* the curve. Without them there is no answer to return.
# * `cashflow` is optional and annotates the curve. A bucket that cannot be
#   dated does not make the equity curve wrong, and raising would take the
#   whole chart down -- and, through rule 9's watchdog, halt the engine --
#   over a fee column.
#
# A bucket already goes missing for ordinary reasons (a `JNLC` dated outside
# the window is simply absent from the recorded fixture), so absence is
# already not an assertion of zero. Mis-dating is the failure that has no
# honest reading, and dropping is what removes it.


@pytest.mark.asyncio
async def test_a_short_cashflow_bucket_is_dropped_not_zipped(make_broker) -> None:
    """Three points, one fee. Which day was the fee on? Unanswerable."""
    body = (
        '{"timestamp": [1786492800, 1786579200, 1786665600],'
        ' "equity": [1, 2, 3], "profit_loss": [0, 0, 0],'
        ' "profit_loss_pct": [0, 0, 0], "base_value": 1, "timeframe": "1D",'
        ' "cashflow": {"FEE": [-0.92]}}'
    )
    broker, _ = make_broker(lambda _request: (200, body))
    history = await broker.portfolio_history()

    assert "FEE" not in history.cashflow
    # The curve itself is untouched: it was never the thing in doubt.
    assert len(history.points) == 3
    assert history.points[-1].equity == Decimal("3")


@pytest.mark.asyncio
async def test_a_long_cashflow_bucket_is_dropped_too(make_broker) -> None:
    """Misaligned is misaligned. Truncating would date the survivors by luck."""
    body = (
        '{"timestamp": [1786492800], "equity": [1], "profit_loss": [0],'
        ' "profit_loss_pct": [0], "base_value": 1, "timeframe": "1D",'
        ' "cashflow": {"CSD": [0, 100]}}'
    )
    broker, _ = make_broker(lambda _request: (200, body))
    history = await broker.portfolio_history()
    assert "CSD" not in history.cashflow


@pytest.mark.asyncio
async def test_an_aligned_bucket_survives_a_broken_neighbour(make_broker) -> None:
    """Per bucket, not all-or-nothing.

    The permit half of the boundary: a bucket that *does* line up is kept
    whole, in order, beside one that does not. Dropping the map wholesale
    would lose a deposit that was perfectly well dated.
    """
    body = (
        '{"timestamp": [1786492800, 1786579200, 1786665600],'
        ' "equity": [1, 2, 3], "profit_loss": [0, 0, 0],'
        ' "profit_loss_pct": [0, 0, 0], "base_value": 1, "timeframe": "1D",'
        ' "cashflow": {"FEE": [-0.92], "CSD": [0, 0, 100], "DIV": [0, 0, 0]}}'
    )
    broker, _ = make_broker(lambda _request: (200, body))
    history = await broker.portfolio_history()

    assert set(history.cashflow) == {"CSD", "DIV"}
    assert history.cashflow["CSD"] == (Decimal("0"), Decimal("0"), Decimal("100"))


@pytest.mark.asyncio
async def test_an_empty_cashflow_bucket_beside_no_points_is_kept(
    make_broker,
) -> None:
    """Zero and zero line up. The boundary is a length check, not a truthiness one."""
    body = (
        '{"timestamp": [], "equity": [], "profit_loss": [],'
        ' "profit_loss_pct": [], "base_value": null, "timeframe": "1D",'
        ' "cashflow": {"FEE": []}}'
    )
    broker, _ = make_broker(lambda _request: (200, body))
    history = await broker.portfolio_history()
    assert history.cashflow["FEE"] == ()


@pytest.mark.asyncio
async def test_every_bucket_returned_lines_up_with_points(make_broker) -> None:
    """The invariant the interface's docstring states, asserted as one.

    A consumer will trust that docstring and zip against it. This is the
    boundary that has to make it true.
    """
    broker, _ = make_broker(single("portfolio_history"))
    history = await broker.portfolio_history()

    assert history.cashflow, "the recorded account has paid fees; guard the guard"
    for bucket, values in history.cashflow.items():
        assert len(values) == len(history.points), bucket


@pytest.mark.asyncio
async def test_dropping_a_bucket_is_logged_with_the_rule_and_both_lengths(
    make_broker, caplog
) -> None:
    """Rule 8's principle: a refusal that leaves no record is a silent one.

    The visible symptom of a dropped bucket is a fee column that is emptier
    than the account -- which is exactly the symptom of *no fees*, so the log
    line is the only thing that tells the two apart.
    """
    body = (
        '{"timestamp": [1786492800, 1786579200], "equity": [1, 2],'
        ' "profit_loss": [0, 0], "profit_loss_pct": [0, 0], "base_value": 1,'
        ' "timeframe": "1D", "cashflow": {"FEE": [-0.92]}}'
    )
    broker, _ = make_broker(lambda _request: (200, body))

    with caplog.at_level(logging.WARNING, logger="corollary.engine.execution.alpaca"):
        await broker.portfolio_history()

    refusals = [
        record
        for record in caplog.records
        if getattr(record, "rule", None) is not None
        and "cashflow" in str(getattr(record, "rule", ""))
    ]
    assert refusals, "a dropped bucket must leave a record naming the rule"
    inputs = refusals[0].inputs
    assert inputs["bucket"] == "FEE"
    assert inputs["values"] == 1
    assert inputs["timestamps"] == 2
    assert "FEE" in refusals[0].getMessage()
