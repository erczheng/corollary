SYNTHETIC TEST FIXTURES -- not State Street data. Every weight here is invented.

These eleven files imitate the layout of State Street's daily Select Sector SPDR
holdings sheets (header lines, a `Name, Ticker, ... Weight ...` table, a cash row,
footer disclaimers) so `tests/scripts/test_build_spdr_seed.py` can drive
`scripts/build_spdr_seed.py` end to end. The layout itself was written from memory
and is unverified against a real download. Never copy these into
`corollary/data/seeds/`.

Each fund's six equity weights sum to exactly 100 (the cash and futures rows are
not equities and are excluded), because the builder refuses a fund whose equity
weights sum outside 90-110. The six names per fund are nowhere near a real
fund's holdings; the weights are scaled up to make the sum check pass, nothing
more.
