"""SQLAlchemy models, session management and migrations.

SQLite in WAL mode, one writer. Money columns are exact decimals — see
``corollary.db.types.Money`` for why a plain ``Numeric`` is not enough on
SQLite, and why the price of that is a column SQL is not allowed to compare,
order or aggregate. Read the rows and compare as ``Decimal`` in Python;
``seed.risk_limits`` is that read. Timestamps are stored UTC via
``UtcDateTime``.

Phase 2 step 2 lands five of the design spec's ten tables: the three config
tables (``risk_limit``, ``data_feed``, ``notification_route``), the single
``audit_log`` spanning all three, and the ``engine_state`` singleton. The
other five belong to later steps.

Run ``uv run alembic upgrade head`` to build the schema and seed the
defaults.
"""
