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

``DGS3MO`` is the 3-month constant-maturity Treasury yield, a bond-equivalent
annual rate, and it is used as Black-Scholes' ``r`` unconverted -- the
continuous-compounding difference at 4% is about 8bp, well inside the
sensitivity bound :mod:`corollary.pricing.blackscholes` states for the tenors
this app trades.
"""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import StrEnum

__all__ = [
    "DGS3MO_SERIES",
    "FALLBACK_RISK_FREE_RATE",
    "RateProvenance",
    "RiskFreeRate",
    "RiskFreeRateSource",
    "rate_from_dgs3mo",
]

#: FRED's series id for the 3-month Treasury bill, constant maturity.
DGS3MO_SERIES = "DGS3MO"

_PERCENT = Decimal(100)


class RateProvenance(StrEnum):
    """Where a risk-free rate came from. The value is what the API serves."""

    #: A stored FRED ``DGS3MO`` observation, converted from percent.
    FRED_DGS3MO = "fred_dgs3mo"
    #: :data:`FALLBACK_RISK_FREE_RATE`: no observation has ever been obtained.
    DEFAULT = "default"


@dataclass(frozen=True, slots=True)
class RiskFreeRate:
    """An annual risk-free rate as a fraction (``0.0416`` is 4.16%), and its source.

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


#: The fallback 3-month bill rate, used only while no FRED ``DGS3MO``
#: observation has ever been obtained. The **only** definition of this number:
#: the pricing functions take ``rate`` as a required argument precisely so that
#: nothing can solve at it without also carrying the ``default`` label.
FALLBACK_RISK_FREE_RATE = RiskFreeRate(
    rate=Decimal("0.0425"),
    provenance=RateProvenance.DEFAULT,
    observation_date=None,
)


def rate_from_dgs3mo(percent: Decimal, observation_date: date) -> RiskFreeRate:
    """One ``DGS3MO`` observation as a rate. ``4.16`` (percent) becomes ``0.0416``."""
    if not isinstance(percent, Decimal):
        raise TypeError(f"DGS3MO arrives as a Decimal, got {type(percent).__name__}")
    if not percent.is_finite():
        raise ValueError(f"a DGS3MO observation must be finite, got {percent}")
    return RiskFreeRate(
        rate=percent / _PERCENT,
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
