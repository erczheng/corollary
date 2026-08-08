"""RiskManager — the only code path to submit_order.

There is exactly one way an order reaches the broker: through
RiskManager.approve(). Never call the broker directly from a strategy, the
scanner, the LLM layer, or an API endpoint. See CLAUDE.md rule 1 and PRD.md
§4.

Not implemented yet — Phase 6. Do not add logic here ahead of that phase.
"""
