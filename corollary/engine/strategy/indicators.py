"""THE WHITELIST.

Every function a strategy YAML may call is defined here, and nowhere else.
Adding a primitive is a deliberate human code change with a test — the LLM
layer may propose rules but may never extend this whitelist. Unknown
function names must reject the whole strategy; never partially evaluate.

Not implemented yet — Phase 4.
"""
