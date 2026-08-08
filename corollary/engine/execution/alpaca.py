"""AlpacaBroker — live/paper order placement via Alpaca.

This is one of exactly two files in the codebase allowed to import the
`alpaca` package (the other is data/providers/alpaca.py). See CLAUDE.md,
"Working with market data."

Not implemented yet — Phase 6. This file must never be imported by the
backtest worker; its process environment carries no live credentials.
"""
