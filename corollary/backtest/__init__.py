"""Backtest worker — a separate process, SimBroker only.

Its environment must be structurally scrubbed of live Alpaca credentials.
On Windows this means launching with an explicit environment, not an
inherited one. Not implemented yet — Phase 5.
"""
