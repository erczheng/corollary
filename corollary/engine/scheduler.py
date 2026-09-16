"""Pre-market build, 15-minute refresh, and end-of-day roll.

See PRD.md §6.5 for the schedule. Not implemented yet — Phase 4.

**Two things belong here and are deliberately not elsewhere.**

*Re-planning the stream subscription when the book changes.*
``engine/sockets.py`` plans once at the session open and says so: nothing in
Phase 2 places an order, so the book cannot change under it, and re-asking
the broker on a five-second socket tick would spend the 200/min budget on a
question whose answer cannot have moved. The refresh that *would* notice a
change is this module's, beside the position poll — and the re-subscribe it
implies (unsubscribe, replace the plan, reconcile the acknowledgement
against ``reconcile_acknowledgement``) is one decision, not two.

*The opening snapshot.* ``EngineRuntime.record_opening_snapshot`` has no
caller yet. It is readiness rather than permission — it does not clear the
cold-start halt — and the pre-market build is what will take it.
"""
