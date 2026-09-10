"""Option pricing mathematics. Pure, vendor-neutral, no I/O.

Deliberately *not* inside ``data/providers/``. Black-Scholes is not an Alpaca
fact — the next provider will want exactly this module, and a provider that
happens to serve greeks (OPRA, once the plan is bought) still needs the same
arithmetic to fill the gaps.
"""
