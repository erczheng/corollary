"""The engine's record that a human confirmed the Cash account -- in memory only.

**Rule 5.** *"Paper is the default. Every cold start comes up in Paper.
Switching to Cash requires an explicit confirmation naming the account and
balance. Never default to live, never persist Cash across a restart without
re-confirmation."*

Until this module the confirmation lived only in the browser, so the engine
had no record of it and no engine job could ever read Cash. A
:class:`CashSession` is that record, and three properties are the rule:

* **It is never persisted.** Not to the database, not to a file. The holder
  is a plain attribute on a plain object built by ``create_app``, so a process
  restart starts with no session and Cash needs confirming again. Adding a
  column for it, or "restoring" it in the lifespan, would be exactly the
  change rule 5 forbids. The audit log records that a session was confirmed
  or ended; it is a record, never a source the session is read back from.
* **It is created only by a confirmation the server checked.**
  ``POST /api/account/cash-session`` reads the live account through the
  read-only Cash ``BrokerAccount`` and refuses unless the masked number the
  dialog echoed matches the one the server just read (rule 4: never trust a
  client value). This module does not check that -- it cannot reach a broker,
  by design -- so :meth:`CashSessionHolder.confirm` is called from that route
  and nowhere else.
* **It holds no full account number.** Only the masked tail (``••••1234``),
  which is all the dialog ever showed and all the echo can be compared
  against; the full number is dropped inside the vendor decode
  (``wire.mask_account_number``). Rule 6.

**What it does not do.** It places nothing and authorises nothing: there is no
``BrokerExecution`` and rule 1's one order path is untouched. It does not
touch the halt state (rule 9) -- confirming Cash never resumes a halted
engine, and ending the session never halts one. It holds no broker, no
registry and no callable, and imports nothing from ``corollary``, so a context
job may be handed a :class:`CashSessionReader` without the jobs' import scan
(``tests/engine/test_scheduler.py``) reaching the API, the runtime or the
broker. The Cash broker itself stays in the lifespan, behind
``ServiceRegistry``; see ``corollary.api.deps.confirmed_cash_broker``.

Single event loop, one assignment per change, so a reader sees the old session
or the new one, never half of each.

**Two writers, serialised, and an end always wins.** The confirm route awaits
the live account read and its audit write, so without care an end could land
in between and be undone when the confirm finished -- Cash back on after the
human turned it off (audit M1). Two guards, either sufficient on its own for
the routes:

* :attr:`CashSessionHolder.writer_lock` -- one ``asyncio.Lock`` both routes
  hold for their whole bodies, so a confirm and an end never interleave and
  their notices go out in the order the changes happened.
* :attr:`CashSessionHolder.generation` -- bumped by every :meth:`end`, never
  by :meth:`confirm`. A confirmation captures it before reading the broker and
  passes it back; if an end happened meanwhile, by any path, the confirm is
  refused with :class:`CashSessionEndedWhileConfirming` and no session is
  created.
"""

import asyncio
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final

__all__ = [
    "MASKED_ACCOUNT_NUMBER",
    "CashSession",
    "CashSessionEndedWhileConfirming",
    "CashSessionHolder",
    "CashSessionReader",
]

#: The only shape of account identifier this module will hold: four bullets
#: and the last four characters. Kept in step with ``wire.mask_account_number``
#: by ``tests/engine/test_cash_session.py``.
MASKED_ACCOUNT_NUMBER: Final = re.compile(r"^••••[A-Za-z0-9]{4}$")


@dataclass(frozen=True, slots=True)
class CashSession:
    """One confirmed Cash session.

    ``confirmed_at`` is aware UTC. ``account_number_masked`` is the masked
    tail the confirmation was checked against. ``balance`` is the account's
    equity as the server read it for that confirmation -- what the human
    confirmed against, kept rather than re-read. ``correlation_id`` is the
    confirming request's, so the session traces back to its audit row, its
    notification and its log line.
    """

    confirmed_at: datetime
    account_number_masked: str
    balance: Decimal
    correlation_id: str


class CashSessionEndedWhileConfirming(Exception):
    """An end happened after this confirmation read its generation.

    Raised by :meth:`CashSessionHolder.confirm` instead of creating a session:
    the human's end is the later decision and it stands (rule 5, fail-off).
    """


class CashSessionHolder:
    """The process's one Cash session, or none. Built empty by ``create_app``.

    Two writers, both API routes (``POST`` and ``DELETE
    /api/account/cash-session``); readers are the ``GET`` route and, from
    unit 2, the lifespan's Cash positions reader through
    :class:`CashSessionReader`.
    """

    def __init__(self) -> None:
        self._session: CashSession | None = None
        self._generation = 0
        #: Held by both writing routes for their whole bodies. Created unbound:
        #: an ``asyncio.Lock`` binds to a loop only when first contended.
        self.writer_lock = asyncio.Lock()

    @property
    def generation(self) -> int:
        """How many times :meth:`end` has been called. A confirm passes back
        the value it read before its broker call; see the module docstring."""
        return self._generation

    def current(self) -> CashSession | None:
        return self._session

    def active(self) -> bool:
        return self._session is not None

    def confirm(
        self,
        *,
        account_number_masked: str,
        balance: Decimal,
        at: datetime,
        correlation_id: str,
        generation: int,
    ) -> CashSession | None:
        """Record a server-checked confirmation. Returns the session it replaced.

        ``generation`` is :attr:`generation` as read before the live account
        was read. If :meth:`end` has run since, this raises
        :class:`CashSessionEndedWhileConfirming` and changes nothing.
        """
        if at.tzinfo is None or at.utcoffset() != timedelta(0):
            raise ValueError(f"confirmed_at must be aware UTC, got {at!r}")
        if not MASKED_ACCOUNT_NUMBER.match(account_number_masked):
            # Never echo the value: if it is not a mask it may be the number.
            raise ValueError("a Cash session holds a masked account number only")
        if not isinstance(balance, Decimal) or not balance.is_finite():
            raise ValueError("a Cash session's balance is a finite Decimal")
        if not correlation_id:
            raise ValueError("a Cash session needs its confirming correlation id")
        if generation != self._generation:
            raise CashSessionEndedWhileConfirming(
                f"the Cash session was ended while this confirmation was in "
                f"flight (read generation {generation}, now {self._generation})"
            )
        previous = self._session
        self._session = CashSession(
            confirmed_at=at,
            account_number_masked=account_number_masked,
            balance=balance,
            correlation_id=correlation_id,
        )
        return previous

    def end(self) -> CashSession | None:
        """End the session, if any. Returns the session that ended, or ``None``.

        Always bumps :attr:`generation`, even with no session: an end during a
        first confirmation must still refuse that confirmation.
        """
        previous = self._session
        self._session = None
        self._generation += 1
        return previous


@dataclass(frozen=True, slots=True, eq=False)
class CashSessionReader:
    """A read-only view of a :class:`CashSessionHolder`, for code outside the API.

    The shape of :class:`~corollary.engine.scheduler.HeldPositionUnderlyings`:
    it reads the holder and nothing more, so whatever holds one can tell
    whether Cash is confirmed but can neither confirm nor end it.
    """

    holder: CashSessionHolder

    def current(self) -> CashSession | None:
        return self.holder.current()

    def active(self) -> bool:
        return self.holder.active()
