"""The risk-free rate a derived greek is computed at, and its provenance.

Phase 3 decision 19: *"``AlpacaProvider`` takes the latest ``DGS3MO``
observation, falling back to the placeholder only when FRED is unreachable,
and the chain's derived greeks record which one they used -- the same
measured versus derived discipline decision 10 of the Phase 2 spec set for
IV."*

This module is vendor-neutral on purpose. It knows that ``DGS3MO`` is quoted
in percent, because that is a fact about the series rather than about the
HTTP client that fetched it; it does not import the FRED client, so the
pricing layer never reaches a vendor file.

Semantics, stated once
----------------------

* **The rate in use is the latest non-missing stored ``DGS3MO``
  observation.** ``corollary.data.macro.risk_free`` reads it from the
  ``fred_observation`` table and hands it to :meth:`RiskFreeRateSource.adopt`.
* **The fallback is reachable only before any observation was ever
  obtained.** :class:`RiskFreeRateSource` starts at
  :data:`FALLBACK_RISK_FREE_RATE` and refuses to be set back to it. A FRED
  outage after the first success leaves the last observation in use, with its
  date showing how old it is.
* **No staleness cutoff.** None is invented here: the observation date travels
  with every rate, so staleness is visible wherever the rate is shown, and a
  week-old bill yield is still a far better input than a constant.

Compounding convention (owner decision Q16)
-------------------------------------------

**Every** :attr:`RiskFreeRate.rate` **is a continuously compounded annual
rate** -- the ``r`` in Black-Scholes' ``exp(-r*T)`` -- and that includes the
fallback. ``DGS3MO`` is not one. FRED titles it *"Market Yield on U.S.
Treasury Securities at 3-Month Constant Maturity, Quoted on an Investment
Basis"*, and the Treasury's yield-curve methodology says *"The inputs for the
bills are bid discount rates corresponding to their bond equivalent yields"*,
the 13-week bill among them. For a bill that short the bond-equivalent yield
is a simple annualised rate on a 365-day year, so a quoted ``y`` grows 1 to
``1 + y*t`` over the bill's term ``t``, and the continuous rate earning the
same is::

    r = ln(1 + y*t) / t,    t = DGS3MO_TERM_YEARS = 91/365

Neither FRED's series notes nor the methodology states a day count, so ``t``
is the owner's 91/365 -- a 13-week bill on a 365-day year. At ``y = 4.00%``,
``r = 3.98019%``: 1.98bp lower. The gap grows roughly as ``y**2 * t / 2``.

``fred_observation`` keeps FRED's quoted percent, untouched: the raw input is
stored, and the conversion happens once, in :func:`rate_from_dgs3mo`, where
the rate source produces the pricing rate. The arithmetic is ``Decimal``
throughout -- ``ln`` is :meth:`decimal.Decimal.ln` under ``_RATE_CONTEXT``,
an explicit precision, so neither a float nor the caller's ambient context
reaches the result.
"""

from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_EVEN, Context, Decimal
from enum import StrEnum

__all__ = [
    "DGS3MO_SERIES",
    "DGS3MO_TERM_YEARS",
    "FALLBACK_DGS3MO_PERCENT",
    "FALLBACK_RISK_FREE_RATE",
    "RATE_QUANTUM",
    "RateProvenance",
    "RiskFreeRate",
    "RiskFreeRateSource",
    "continuous_rate_from_bill_yield",
    "rate_from_dgs3mo",
]

#: FRED's series id for the 3-month Treasury bill, constant maturity.
DGS3MO_SERIES = "DGS3MO"

_PERCENT = Decimal(100)

#: The context every rate conversion runs under. 40 significant digits is far
#: past anything a greek resolves; stated here so ``Decimal.ln`` never runs at
#: whatever precision the caller's thread happened to have.
_RATE_CONTEXT = Context(prec=40, rounding=ROUND_HALF_EVEN)

#: The term of the bill ``DGS3MO`` quotes, in years: **91/365**. The 3-month
#: constant-maturity point is built from the 13-week (91-day) bill, and a
#: bond-equivalent yield annualises on a 365-day year. Neither FRED's series
#: notes nor the Treasury's yield-curve methodology states another day count,
#: so this is the owner's choice (decision Q16), not a measured one. A
#: ``Decimal`` quotient under ``_RATE_CONTEXT``.
DGS3MO_TERM_YEARS = _RATE_CONTEXT.divide(Decimal(91), Decimal(365))

#: Every converted rate is rounded to this. ``1E-10`` is a millionth of a
#: basis point, far below any greek's resolution, and a fixed quantum keeps
#: the number deterministic and legible in logs and on the API.
RATE_QUANTUM = Decimal("1E-10")


class RateProvenance(StrEnum):
    """Where a risk-free rate came from. The value is what the API serves."""

    #: A stored FRED ``DGS3MO`` observation, converted from its quoted
    #: percent to a continuous rate.
    FRED_DGS3MO = "fred_dgs3mo"
    #: :data:`FALLBACK_RISK_FREE_RATE`: no observation has ever been obtained.
    DEFAULT = "default"


@dataclass(frozen=True, slots=True)
class RiskFreeRate:
    """A **continuously compounded** annual risk-free rate as a fraction, and its source.

    ``0.0413857528`` is 4.13857528% continuous -- what a quoted ``DGS3MO`` of
    4.16% becomes (see the module docstring). Never a quoted yield: build one
    from FRED's percent with :func:`rate_from_dgs3mo`, which converts.

    ``observation_date`` is the FRED observation's date -- the session whose
    close it records, not the day it was fetched -- and is ``None`` exactly
    when ``provenance`` is :attr:`RateProvenance.DEFAULT`.
    """

    rate: Decimal
    provenance: RateProvenance
    observation_date: date | None

    def __post_init__(self) -> None:
        if not isinstance(self.rate, Decimal):
            raise TypeError(
                f"a risk-free rate is a Decimal, got {type(self.rate).__name__}"
            )
        if not self.rate.is_finite():
            raise ValueError(f"a risk-free rate must be finite, got {self.rate}")
        if self.provenance is RateProvenance.DEFAULT:
            if self.observation_date is not None:
                raise ValueError(
                    "the default rate was observed on no date; an observation "
                    "date beside it would claim a measurement that never happened"
                )
        elif self.observation_date is None:
            raise ValueError(
                f"a {self.provenance.value} rate must carry its observation date, "
                "so how old it is stays visible"
            )


def continuous_rate_from_bill_yield(bill_yield: Decimal) -> Decimal:
    """A 3-month bill's bond-equivalent yield, as a continuously compounded rate.

    ``bill_yield`` is a fraction (``0.04`` is 4%). Returns
    ``ln(1 + y*t) / t`` with ``t`` = :data:`DGS3MO_TERM_YEARS`, worked in
    ``Decimal`` under ``_RATE_CONTEXT`` and rounded to :data:`RATE_QUANTUM`.
    Zero maps to zero; every positive yield comes out lower, by 1.98bp at 4%.
    """
    if not isinstance(bill_yield, Decimal):
        raise TypeError(f"a bill yield is a Decimal, got {type(bill_yield).__name__}")
    if not bill_yield.is_finite():
        raise ValueError(f"a bill yield must be finite, got {bill_yield}")
    ctx = _RATE_CONTEXT
    growth = ctx.add(Decimal(1), ctx.multiply(bill_yield, DGS3MO_TERM_YEARS))
    if growth <= 0:
        raise ValueError(
            f"a bill yield of {bill_yield} makes 1 + y*t = {growth}, which has no "
            "logarithm; no continuous rate corresponds to it"
        )
    continuous = ctx.divide(growth.ln(ctx), DGS3MO_TERM_YEARS)
    return continuous.quantize(RATE_QUANTUM, context=ctx)


#: The fallback's quoted yield, in percent: the old ``0.0425`` placeholder.
#: It stood in for **the same quantity FRED publishes** -- the 3-month bill's
#: quoted yield; ``blackscholes.py`` named ``DGS3MO`` as its replacement -- so
#: it is read as a quoted yield and converted exactly as an observation is.
#: That keeps a FRED observation of 4.25% and the fallback pricing
#: identically: one convention matters more than the 2bp the conversion moves.
FALLBACK_DGS3MO_PERCENT = Decimal("4.25")

#: The fallback 3-month bill rate, used only while no FRED ``DGS3MO``
#: observation has ever been obtained. The **only** definition of this number:
#: the pricing functions take ``rate`` as a required argument precisely so that
#: nothing can solve at it without also carrying the ``default`` label.
#: Continuous, like every :class:`RiskFreeRate`: ``0.0422764153``.
FALLBACK_RISK_FREE_RATE = RiskFreeRate(
    rate=continuous_rate_from_bill_yield(
        _RATE_CONTEXT.divide(FALLBACK_DGS3MO_PERCENT, _PERCENT)
    ),
    provenance=RateProvenance.DEFAULT,
    observation_date=None,
)


def rate_from_dgs3mo(percent: Decimal, observation_date: date) -> RiskFreeRate:
    """One ``DGS3MO`` observation as the continuous rate pricing uses.

    ``percent`` is FRED's quoted value as stored (``4.16``). It becomes a
    fraction and is converted by :func:`continuous_rate_from_bill_yield`, so
    4.16% becomes ``0.0413857528``. This is the one place a quoted yield
    becomes a pricing rate.
    """
    if not isinstance(percent, Decimal):
        raise TypeError(f"DGS3MO arrives as a Decimal, got {type(percent).__name__}")
    if not percent.is_finite():
        raise ValueError(f"a DGS3MO observation must be finite, got {percent}")
    return RiskFreeRate(
        rate=continuous_rate_from_bill_yield(_RATE_CONTEXT.divide(percent, _PERCENT)),
        provenance=RateProvenance.FRED_DGS3MO,
        observation_date=observation_date,
    )


class RiskFreeRateSource:
    """The rate the pricing boundary reads, updated as observations are stored.

    One instance per process, shared by the market-data provider (which reads
    it per chain) and the FRED refresh job (which adopts what it stored). It
    is a cache of the table's answer, not a second authority: the job adopts
    the latest non-missing stored row, whatever it was before.

    It starts at :data:`FALLBACK_RISK_FREE_RATE` and **never returns to it**.
    """

    def __init__(self, initial: RiskFreeRate = FALLBACK_RISK_FREE_RATE) -> None:
        self._current = initial

    def current(self) -> RiskFreeRate:
        return self._current

    def adopt(self, rate: RiskFreeRate) -> None:
        """Use ``rate`` from now on. Refuses the default -- see the class docstring."""
        if rate.provenance is RateProvenance.DEFAULT:
            raise ValueError(
                "the default rate is what never having reached FRED looks like; "
                "it is not a value to adopt over an observation"
            )
        self._current = rate
