"""The economic first-print figure (U6c, spec R7), on the recorded U6a vintages.

Every expected value below is a **literal worked by hand**, with the working
in a comment, never recomputed here with the arithmetic the code uses -- a
test that repeats the code's formula agrees with the code by construction.

The windows are FRED's own ``realtime_start = D-1, realtime_end = D``
responses, recorded 2026-11-09 by ``tests/fixtures/record_fred_vintages.py``.
Where a test needs a response FRED was never asked for (a vintage before
posting, a ``"."``), it is derived from a recorded one, and says so.
"""

import copy
import dataclasses
import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any, Final

import httpx
import pytest

from corollary.data.econ_values import (
    COMPUTED_FROM_FRED,
    STORED_EXPONENT,
    Figure,
    NotPosted,
    NoValue,
    Refused,
    computed_label,
    first_print,
)
from corollary.data.providers.fred import (
    FredCredentials,
    FredProvider,
    FredRefusal,
    FredVintageWindow,
    VintageFrequency,
    parse_vintage_window,
)
from corollary.data.seeds.calendar_seeds import (
    MAX_DISPLAY_DECIMALS,
    EconReleaseSeries,
    EconTransform,
)
from corollary.ratelimit import HostRateLimiter

FIXTURE_DIR: Final = Path(__file__).resolve().parents[1] / "fixtures" / "fred"

CPI: Final = EconReleaseSeries(
    10, "CPIAUCSL", "pct_change", Decimal("1"), "% m/m", 1, "monthly"
)
PAYEMS: Final = EconReleaseSeries(50, "PAYEMS", "change", Decimal("1"), "K", 0, "monthly")
BOPGSTB: Final = EconReleaseSeries(
    51, "BOPGSTB", "level", Decimal("0.001"), "$B", 1, "monthly"
)

CPI_DAY: Final = date(2026, 10, 14)
PAYEMS_DAY: Final = date(2026, 11, 6)
BOP_DAY: Final = date(2026, 11, 4)


def envelope(name: str) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(
        (FIXTURE_DIR / f"p3_vintage_{name}.json").read_text(encoding="utf-8")
    )
    return loaded


def body(name: str) -> dict[str, Any]:
    payload: dict[str, Any] = copy.deepcopy(envelope(name)["body"])
    return payload


def window(
    series_id: str,
    payload: dict[str, Any],
    release_day: date,
    frequency: VintageFrequency = "monthly",
) -> FredVintageWindow:
    return parse_vintage_window(
        series_id, payload, release_day=release_day, frequency=frequency
    )


def recorded(sid: str, series_id: str, release_day: date) -> FredVintageWindow:
    return window(series_id, body(f"{sid}_window"), release_day)


def _series(
    transform: EconTransform, scale: str = "1", frequency: VintageFrequency = "monthly"
) -> EconReleaseSeries:
    return EconReleaseSeries(999, "TEST", transform, Decimal(scale), "u", 1, frequency)


#: FRED's echo of what every vintage query sends, as the recorded bodies carry it.
ECHO: Final = {"units": "lin", "output_type": 1}


def _synthetic(
    rows: list[tuple[str, str, str, str]], release_day: date = PAYEMS_DAY
) -> FredVintageWindow:
    """A window from ``(date, value, realtime_start, realtime_end)`` rows."""
    day_before = date.fromordinal(release_day.toordinal() - 1).isoformat()
    payload = {
        "realtime_start": day_before,
        "realtime_end": release_day.isoformat(),
        **ECHO,
        "count": len(rows),
        "observations": [
            {"date": d, "value": v, "realtime_start": s, "realtime_end": e}
            for d, v, s, e in rows
        ],
    }
    return window("TEST", payload, release_day)


B, D = "2026-11-05", "2026-11-06"  # D-1 and D for the synthetic windows


# --------------------------------------------------------------------------
# The three recorded releases
# --------------------------------------------------------------------------


def test_cpi_pct_change_from_the_recorded_window() -> None:
    """CPIAUCSL, released 2026-10-14: September's index, new that day.

    x(t) = 2026-09-01 = 336.298, x(t-1) = 2026-08-01 = 334.131,
    x(t-2) = 2026-07-01 = 332.813, all in the 2026-10-14 vintage.

    actual = (336.298 / 334.131 - 1) x 100 = 2.167 / 334.131 x 100
           = 216.7 / 334.131. Long division:
             334.131 x 0.6      = 200.4786    remainder 16.2214
             334.131 x 0.04     =  13.36524   remainder  2.85616
             334.131 x 0.008    =   2.673048  remainder  0.183112
             334.131 x 0.0005   =   0.1670655 remainder  0.0160465
             334.131 x 0.00004  =   0.01336524 remainder 0.00268126
           = 0.64854...; the fifth place is 4, so it rounds down: 0.6485.
    prior  = (334.131 / 332.813 - 1) x 100 = 1.318 / 332.813 x 100
           = 131.8 / 332.813:
             332.813 x 0.3      =  99.8439    remainder 31.9561
             332.813 x 0.09     =  29.95317   remainder  2.00293
             332.813 x 0.006    =   1.996878  remainder  0.006052
             332.813 x 0.0000   (0.0332813 > 0.006052)
             332.813 x 0.00001  =   0.00332813 remainder 0.00272387
           = 0.39601...; the fifth place is 1, so it rounds down: 0.3960.
    """
    figure = first_print(recorded("cpiaucsl", "CPIAUCSL", CPI_DAY), CPI)
    assert figure == Figure(
        series_id="CPIAUCSL",
        period=date(2026, 9, 1),
        actual=Decimal("0.6485"),
        prior=Decimal("0.3960"),
        vintage_date=CPI_DAY,
    )
    assert isinstance(figure, Figure)
    assert (str(figure.actual), str(figure.prior)) == ("0.6485", "0.3960")


def test_payems_change_uses_the_same_vintage_prior() -> None:
    """PAYEMS, released 2026-11-06: October's payrolls, new that day.

    In the 2026-11-06 vintage: Oct 159293, Sep 159128 (revised that day from
    159044), Aug 159068 (revised that day from 159015).

    actual = 159293 - 159128 = 165 -> 165.0000. The superseded September,
             159044, would give 249: the prior is taken in the same vintage.
    prior  = 159128 - 159068 = 60 -> 60.0000.
    """
    figure = first_print(recorded("payems", "PAYEMS", PAYEMS_DAY), PAYEMS)
    assert figure == Figure(
        series_id="PAYEMS",
        period=date(2026, 10, 1),
        actual=Decimal("165.0000"),
        prior=Decimal("60.0000"),
        vintage_date=PAYEMS_DAY,
    )
    assert isinstance(figure, Figure)
    assert (str(figure.actual), str(figure.prior)) == ("165.0000", "60.0000")


def test_bopgstb_level_with_scale() -> None:
    """BOPGSTB, released 2026-11-04: September's balance, new that day.

    actual = -105047.0 x 0.001 = -105.0470 ($B).
    prior  = August as revised that day, -105737.0 x 0.001 = -105.7370;
             the superseded -105572.0 is not used.
    """
    figure = first_print(recorded("bopgstb", "BOPGSTB", BOP_DAY), BOPGSTB)
    assert figure == Figure(
        series_id="BOPGSTB",
        period=date(2026, 9, 1),
        actual=Decimal("-105.0470"),
        prior=Decimal("-105.7370"),
        vintage_date=BOP_DAY,
    )
    assert isinstance(figure, Figure)
    assert (str(figure.actual), str(figure.prior)) == ("-105.0470", "-105.7370")


# --------------------------------------------------------------------------
# Not posted, no value, refused
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sid", "series", "release_day"),
    [("cpiaucsl", CPI, CPI_DAY), ("payems", PAYEMS, PAYEMS_DAY), ("bopgstb", BOPGSTB, BOP_DAY)],
)
def test_before_posting_is_not_posted(
    sid: str, series: EconReleaseSeries, release_day: date
) -> None:
    """**Derived, not recorded.** The recorded ``day_before`` vintage (``D-1``
    alone) laid out as it would appear clipped into a ``D-1 .. D`` window
    asked for before FRED posted: every value is valid on both days, so every
    row starts ``D-1`` and ends ``D``, and nothing starts on ``D``.
    """
    before = body(f"{sid}_day_before")
    day_before = before["realtime_start"]
    before["realtime_end"] = release_day.isoformat()
    for row in before["observations"]:
        assert (row["realtime_start"], row["realtime_end"]) == (day_before, day_before)
        row["realtime_end"] = release_day.isoformat()

    outcome = first_print(window(series.series_id, before, release_day), series)

    assert outcome == NotPosted(series_id=series.series_id, vintage_date=release_day)


def test_an_empty_window_is_not_posted() -> None:
    assert first_print(_synthetic([]), _series("level")) == NotPosted("TEST", PAYEMS_DAY)


def _payems_with(dated: str, realtime_start: str, value: str) -> FredVintageWindow:
    """**Derived.** The recorded PAYEMS window with one value replaced."""
    payload = body("payems_window")
    (row,) = [
        r
        for r in payload["observations"]
        if r["date"] == dated and r["realtime_start"] == realtime_start
    ]
    row["value"] = value
    return window("PAYEMS", payload, PAYEMS_DAY)


@pytest.mark.parametrize(
    ("dated", "realtime_start", "which"),
    [
        ("2026-10-01", D, "x(t)"),  # the release's own observation
        ("2026-09-01", D, "x(t-1)"),  # the actual's base, revised that day
        ("2026-08-01", D, "x(t-2)"),  # needed only for the prior
    ],
)
def test_a_missing_value_in_a_needed_observation_is_no_value(
    dated: str, realtime_start: str, which: str
) -> None:
    outcome = first_print(_payems_with(dated, realtime_start, "."), PAYEMS)
    assert isinstance(outcome, NoValue)
    assert outcome.series_id == "PAYEMS" and outcome.vintage_date == PAYEMS_DAY
    assert which in outcome.reason and dated in outcome.reason
    assert "'.'" in outcome.reason


def test_a_missing_value_nobody_needs_changes_nothing() -> None:
    # 2026-05-01 is five periods back; neither the actual nor the prior reads it.
    figure = first_print(_payems_with("2026-05-01", B, "."), PAYEMS)
    assert isinstance(figure, Figure)
    assert (figure.actual, figure.prior) == (Decimal("165.0000"), Decimal("60.0000"))


def test_a_value_withdrawn_to_missing_on_d_is_a_revision() -> None:
    # Was a number on D-1, is "." on D: the value changed, and the release's
    # observation is that one -- whose value is missing, so no value at all.
    outcome = first_print(
        _synthetic(
            [
                ("2026-07-01", "1", B, D),
                ("2026-08-01", "2", B, D),
                ("2026-09-01", "3", B, B),
                ("2026-09-01", ".", D, D),
            ]
        ),
        _series("level"),
    )
    assert isinstance(outcome, NoValue) and "x(t)" in outcome.reason


def test_a_text_only_revision_is_not_new() -> None:
    # '159128' on D against '159128.0' on D-1: FRED's formatting is not
    # canonical, and the same number in two spellings is not a revision.
    rows = [
        ("2026-07-01", "158882", B, D),
        ("2026-08-01", "159068", B, D),
        ("2026-09-01", "159128.0", B, B),
        ("2026-09-01", "159128", D, D),
    ]
    assert first_print(_synthetic(rows), _series("change")) == NotPosted("TEST", PAYEMS_DAY)

    # The control: a real revision of the same row is new.
    revised = [*rows[:2], ("2026-09-01", "159044", B, B), rows[3]]
    figure = first_print(_synthetic(revised), _series("change"))
    assert isinstance(figure, Figure) and figure.period == date(2026, 9, 1)
    assert figure.actual == Decimal("60.0000")  # 159128 - 159068
    assert figure.prior == Decimal("186.0000")  # 159068 - 158882


def test_an_observation_removed_on_d_is_not_in_the_vintage() -> None:
    """The D vintage is the rows **ending on D**, not every row in the window.

    2026-08-01 was valid on D-1 only: FRED removed it on D. In the D vintage
    the observation before 2026-09-01 is 2026-07-01, two months back, so
    there is no m/m figure. Reading every row as the vintage would compute
    120 - 110 = +10 and 110 - 100 = +10 from a value no longer in it.
    """
    outcome = first_print(
        _synthetic(
            [
                ("2026-07-01", "100", B, D),
                ("2026-08-01", "110", B, B),
                ("2026-09-01", "120", D, D),
            ]
        ),
        _series("change"),
    )
    assert isinstance(outcome, NoValue)
    assert outcome.reason.startswith("x(t-1) should be 2026-08-01")
    assert outcome.reason.endswith("in the 2026-11-06 vintage is 2026-07-01")


def test_the_latest_new_or_revised_observation_is_the_release() -> None:
    # Two rows start on D; the later date is the release, the earlier one a
    # revision to its prior.
    figure = first_print(
        _synthetic(
            [
                ("2026-07-01", "100", B, D),
                ("2026-08-01", "110", B, B),
                ("2026-08-01", "111", D, D),
                ("2026-09-01", "120", D, D),
            ]
        ),
        _series("change"),
    )
    assert figure == Figure(
        "TEST", date(2026, 9, 1), Decimal("9.0000"), Decimal("11.0000"), PAYEMS_DAY
    )


@pytest.mark.parametrize(
    ("transform", "rows", "which"),
    [
        # level needs x(t) and x(t-1).
        ("level", [("2026-09-01", "1", D, D)], "x(t-1)"),
        # change and pct_change need x(t-2) for the prior as well.
        ("change", [("2026-08-01", "1", B, D), ("2026-09-01", "2", D, D)], "x(t-2)"),
        ("pct_change", [("2026-08-01", "1", B, D), ("2026-09-01", "2", D, D)], "x(t-2)"),
    ],
)
def test_an_observation_missing_from_the_response_is_no_value(
    transform: EconTransform, rows: list[tuple[str, str, str, str]], which: str
) -> None:
    # Never an actual beside an invented or zero prior.
    outcome = first_print(_synthetic(rows), _series(transform))
    assert isinstance(outcome, NoValue)
    assert which in outcome.reason


def test_level_needs_only_two_observations() -> None:
    figure = first_print(
        _synthetic([("2026-08-01", "4", B, D), ("2026-09-01", "5", D, D)]), _series("level")
    )
    assert figure == Figure(
        "TEST", date(2026, 9, 1), Decimal("5.0000"), Decimal("4.0000"), PAYEMS_DAY
    )


def test_a_gap_in_the_vintage_is_no_value_not_a_two_period_change() -> None:
    # July is absent: the observation before September is not one month
    # earlier, so an "m/m" figure computed against it would be a two-month one.
    outcome = first_print(
        _synthetic(
            [("2026-06-01", "1", B, D), ("2026-08-01", "2", B, D), ("2026-09-01", "3", D, D)]
        ),
        _series("change"),
    )
    assert isinstance(outcome, NoValue)
    assert "2026-07-01" in outcome.reason


def test_a_monthly_series_seeded_as_quarterly_is_no_value() -> None:
    # A seed row marking PAYEMS quarterly, fetched as the row says: October
    # starts a quarter, but the observation before it is September, not July,
    # so no figure -- never a one-month change beside a "q/q" label.
    seeded_quarterly = dataclasses.replace(PAYEMS, frequency="quarterly")
    payload = body("payems_window")
    outcome = first_print(
        window("PAYEMS", payload, PAYEMS_DAY, seeded_quarterly.frequency), seeded_quarterly
    )
    assert isinstance(outcome, NoValue)
    assert "quarterly" in outcome.reason


def test_a_window_fetched_at_another_frequency_than_the_seed_row_is_a_caller_bug() -> None:
    # The frequency is the seed row's: a window fetched with another lookback
    # was fetched for a different question.
    payload = body("payems_window")
    with pytest.raises(ValueError, match="quarterly.*monthly"):
        first_print(window("PAYEMS", payload, PAYEMS_DAY, "quarterly"), PAYEMS)


def test_a_quarterly_series_reads_quarters() -> None:
    qb, qd = "2026-10-28", "2026-10-29"
    rows = [
        ("2026-01-01", "200", qb, qd),
        ("2026-04-01", "202", qb, qd),
        ("2026-07-01", "203.01", qd, qd),
    ]
    payload = {
        "realtime_start": qb,
        "realtime_end": qd,
        **ECHO,
        "count": len(rows),
        "observations": [
            {"date": d, "value": v, "realtime_start": s, "realtime_end": e} for d, v, s, e in rows
        ],
    }
    figure = first_print(
        window("TEST", payload, date(2026, 10, 29), "quarterly"),
        _series("pct_change", frequency="quarterly"),
    )
    # actual = (203.01 / 202 - 1) x 100 = 1.01 / 202 x 100 = 101 / 202 = 0.5
    # prior  = (202 / 200 - 1) x 100 = 2 / 200 x 100 = 1
    assert figure == Figure(
        "TEST", date(2026, 7, 1), Decimal("0.5000"), Decimal("1.0000"), date(2026, 10, 29)
    )


@pytest.mark.parametrize(
    ("rows", "which"),
    [
        ([("2026-07-01", "5", B, D), ("2026-08-01", "0", B, D), ("2026-09-01", "1", D, D)], "x(t-1)"),
        ([("2026-07-01", "0", B, D), ("2026-08-01", "5", B, D), ("2026-09-01", "1", D, D)], "x(t-2)"),
    ],
)
def test_pct_change_over_zero_is_no_value(
    rows: list[tuple[str, str, str, str]], which: str
) -> None:
    outcome = first_print(_synthetic(rows), _series("pct_change"))
    assert isinstance(outcome, NoValue)
    assert which in outcome.reason and "zero" in outcome.reason


def test_a_change_over_zero_is_a_number() -> None:
    # Only pct_change divides; a change from zero is ordinary.
    figure = first_print(
        _synthetic(
            [("2026-07-01", "0", B, D), ("2026-08-01", "0", B, D), ("2026-09-01", "3", D, D)]
        ),
        _series("change"),
    )
    assert isinstance(figure, Figure)
    assert (str(figure.actual), str(figure.prior)) == ("3.0000", "0.0000")


async def _no_sleep(seconds: float) -> None:  # pragma: no cover
    raise AssertionError("slept")


async def _fetch(
    series: EconReleaseSeries, release_day: date, respond: Any
) -> FredVintageWindow | FredRefusal:
    """One ``vintage_window`` through a fake FRED, at the **seed row's** frequency."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    async with FredProvider(
        credentials=FredCredentials(api_key="notarealfredkeynotarealfredkey00"),
        client=client,
        limiter=HostRateLimiter(clock=lambda: 0.0, sleep=_no_sleep),
    ) as provider:
        return await provider.vintage_window(
            series.series_id, release_day, frequency=series.frequency
        )


@pytest.mark.asyncio
async def test_exhoslusm495s_recorded_400_through_the_provider_is_refused() -> None:
    text = (FIXTURE_DIR / "p3_vintage_exhoslusm495s_window.json").read_text(encoding="utf-8")
    replay = json.dumps(json.loads(text)["body"])
    assert json.loads(text)["status_code"] == 400
    series = EconReleaseSeries(
        291, "EXHOSLUSM495S", "level", Decimal("0.000001"), "M SAAR", 2, "monthly"
    )

    answer = await _fetch(
        series, date(2026, 10, 13), lambda request: httpx.Response(400, content=replay.encode())
    )
    outcome = first_print(answer, series)

    assert isinstance(outcome, Refused)
    assert outcome.series_id == "EXHOSLUSM495S"
    assert outcome.vintage_date == date(2026, 10, 13)
    assert outcome.vendor_message.startswith(
        "Bad Request. The series does not exist in ALFRED but may exist in FRED."
    )


def _fred_honouring_observation_start(history: list[tuple[str, str, str, str]]) -> Any:
    """A fake FRED holding ``history`` that, like FRED, serves only the rows
    dated on or after the request's ``observation_start``."""

    def respond(request: httpx.Request) -> httpx.Response:
        params = request.url.params
        kept = [
            {"date": d, "value": v, "realtime_start": s, "realtime_end": e}
            for d, v, s, e in history
            if d >= params["observation_start"]
        ]
        return httpx.Response(
            200,
            json={
                "realtime_start": params["realtime_start"],
                "realtime_end": params["realtime_end"],
                "observation_start": params["observation_start"],
                **ECHO,
                "count": len(kept),
                "observations": kept,
            },
        )

    return respond


@pytest.mark.asyncio
async def test_a_series_lagging_three_months_is_in_the_window() -> None:
    """x(t) three months before the release's month (2026-08 for a November
    release) puts x(t-2) at 2026-06-01: inside the six-month lookback, outside
    the four the spec states as its floor.

    actual = 121 - 110 = 11 -> 11.0000; prior = 110 - 104 = 6 -> 6.0000.
    March to May are in the window too, and change nothing.
    """
    history = [
        ("2026-03-01", "90", B, D),
        ("2026-04-01", "95", B, D),
        ("2026-05-01", "100", B, D),
        ("2026-06-01", "104", B, D),
        ("2026-07-01", "110", B, D),
        ("2026-08-01", "121", D, D),
    ]
    series = _series("change")
    answer = await _fetch(series, PAYEMS_DAY, _fred_honouring_observation_start(history))

    # Under the four-month floor this is NoValue naming x(t-2), 2026-06-01.
    assert first_print(answer, series) == Figure(
        "TEST", date(2026, 8, 1), Decimal("11.0000"), Decimal("6.0000"), PAYEMS_DAY
    )
    assert isinstance(answer, FredVintageWindow)
    assert min(row.date for row in answer.rows) == date(2026, 5, 1)


@pytest.mark.asyncio
async def test_the_wider_window_keeps_one_period_strictness() -> None:
    # July is missing. The window now reaches back far enough to hold June,
    # May and more, and the observation before August is still not July: no
    # figure, rather than a two-month change against June.
    history = [
        ("2026-05-01", "100", B, D),
        ("2026-06-01", "104", B, D),
        ("2026-08-01", "121", D, D),
    ]
    series = _series("change")
    answer = await _fetch(series, PAYEMS_DAY, _fred_honouring_observation_start(history))

    outcome = first_print(answer, series)

    assert isinstance(outcome, NoValue)
    assert outcome.reason.startswith("x(t-1) should be 2026-07-01")


# --------------------------------------------------------------------------
# Arithmetic: exact, quantized half-even, never float
# --------------------------------------------------------------------------


def test_the_stored_precision_is_the_display_ceiling() -> None:
    # One fact, two names: the seed may ask for at most as many display
    # places as the figure is stored to, and no fewer are stored.
    assert STORED_EXPONENT.as_tuple().exponent == -MAX_DISPLAY_DECIMALS
    assert str(STORED_EXPONENT) == "0.0001"


@pytest.mark.parametrize(
    ("value", "stored"),
    [
        # Exactly half at the fifth place: half-even goes to the even digit.
        ("1.00005", "1.0000"),
        ("1.00015", "1.0002"),
        ("1.00025", "1.0002"),
        ("-1.00015", "-1.0002"),
        # Not a half: ordinary rounding either way.
        ("1.000051", "1.0001"),
        ("7", "7.0000"),
    ],
)
def test_the_stored_value_is_quantized_half_even(value: str, stored: str) -> None:
    figure = first_print(
        _synthetic([("2026-08-01", "1", B, D), ("2026-09-01", value, D, D)]), _series("level")
    )
    assert isinstance(figure, Figure)
    assert str(figure.actual) == stored
    assert figure.actual.as_tuple().exponent == STORED_EXPONENT.as_tuple().exponent


def test_a_negative_zero_is_stored_as_zero() -> None:
    # (9999999 / 10000000 - 1) x 100 = -0.00001, which quantizes to -0.0000.
    figure = first_print(
        _synthetic(
            [
                ("2026-07-01", "1", B, D),
                ("2026-08-01", "10000000", B, D),
                ("2026-09-01", "9999999", D, D),
            ]
        ),
        _series("pct_change"),
    )
    assert isinstance(figure, Figure)
    assert str(figure.actual) == "0.0000"


def test_scale_is_applied_before_quantizing() -> None:
    # 4060000.0 x 0.000001 = 4.06 -> 4.0600 ; 1234.56785 x 0.001 = 1.23456785 -> 1.2346
    figure = first_print(
        _synthetic([("2026-08-01", "1234.56785", B, D), ("2026-09-01", "4060000.0", D, D)]),
        _series("level", "0.000001"),
    )
    assert isinstance(figure, Figure) and str(figure.actual) == "4.0600"
    figure = first_print(
        _synthetic([("2026-08-01", "1", B, D), ("2026-09-01", "1234.56785", D, D)]),
        _series("level", "0.001"),
    )
    assert isinstance(figure, Figure) and str(figure.actual) == "1.2346"


def test_every_figure_is_an_exact_decimal() -> None:
    for answer, series in (
        (recorded("cpiaucsl", "CPIAUCSL", CPI_DAY), CPI),
        (recorded("payems", "PAYEMS", PAYEMS_DAY), PAYEMS),
        (recorded("bopgstb", "BOPGSTB", BOP_DAY), BOPGSTB),
    ):
        figure = first_print(answer, series)
        assert isinstance(figure, Figure)
        assert type(figure.actual) is Decimal and type(figure.prior) is Decimal


def test_a_window_for_another_series_is_a_caller_bug() -> None:
    with pytest.raises(ValueError, match="CPIAUCSL"):
        first_print(recorded("payems", "PAYEMS", PAYEMS_DAY), CPI)
    with pytest.raises(ValueError, match="PAYEMS"):
        first_print(FredRefusal("PAYEMS", PAYEMS_DAY, "no"), CPI)


# --------------------------------------------------------------------------
# Labelling
# --------------------------------------------------------------------------


def test_the_label_names_the_series() -> None:
    assert COMPUTED_FROM_FRED == "computed from FRED's levels"
    assert computed_label("CPIAUCSL") == "computed from FRED's levels: CPIAUCSL"
