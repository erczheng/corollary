"""The explicit, configurable spread model for backtest fills.

Alpaca has no historical options quotes endpoint. Every backtest fill price
is an estimate — fill at mid ± half the modeled spread, estimated from
contract moneyness, DTE, and underlying liquidity — never a measurement.
See PRD.md §5.3. Not implemented yet — Phase 5.
"""
