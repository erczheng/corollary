"""Record OpenFIGI's answers for the 29 ISIN-only SPDR holdings into ``tests/fixtures/openfigi/``.

Run by hand, never by the test suite. It needs **nothing** from the
environment -- it is keyless by construction -- so it runs plainly::

    uv run python tests/fixtures/record_openfigi.py

Phase 3 spec Q17 resolves the N-PORT lines that carry an ISIN and no CUSIP
through OpenFIGI; Q21 is the owner's narrow rule-1 exemption that lets
``corollary/data/providers/openfigi.py`` make its one mapping request. This
file is **not** exempt and needs no exemption: it makes no request of its
own. Every byte that goes on the wire goes through
:meth:`OpenFigiProvider.map_isin_batches`, the provider's one exempt call
site, and this file imports no HTTP client at all.

What it enforces, in order, before anything is written:

* **The ISINs come from the recorded filings, and must be exactly Q17's.**
  They are derived from ``tests/fixtures/sec/nport_XL*_primary_doc.xml`` --
  every ``EC`` line with no usable CUSIP and a well-formed ISIN -- and
  compared against :data:`EXPECTED_ISIN_ONLY`, the table in spec Q17. Any
  difference (a missing line, an extra one, a different fund, a different
  order) refuses the run before a request is made.
* **Keyless, explicitly.** The provider is built with
  ``OpenFigiCredentials(api_key=None)`` -- never from the environment -- so
  no key can reach a request header or a fixture, and the run is exactly
  three requests of 10, 10 and 9 jobs under the keyless limit. A different
  batching refuses the run.
* **No secret reaches disk.** Every file's text is checked for the key
  header's name and other key-shaped words, and for the value of every
  environment variable whose name contains ``KEY``, ``SECRET``, ``TOKEN`` or
  ``AGENT`` (eight characters or more). A hit aborts with nothing written.
* **All or nothing, and no overwrite.** All three files are rendered and
  checked before the first is written; an existing fixture refuses the run.
* **No number is re-serialised.** A mapping reply carries only text, so a
  JSON number is a shape change; abort rather than round it. The decoded
  reply is walked before rendering and every leaf must be a string, a
  boolean or null -- a ``Decimal`` (a JSON fraction), an ``int`` (a JSON
  integer) and a ``float`` (``NaN`` / ``Infinity``, which Python's decoder
  accepts) are all refused, and the render itself runs with
  ``allow_nan=False`` as a backstop.

The issuer names in each file are the N-PORT names, carried for the record
only. **They are never sent** -- the provider takes identifiers alone.
"""

import asyncio
import json
import os
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Final

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from corollary.data.providers.openfigi import (  # noqa: E402
    KEYLESS_JOBS_PER_REQUEST,
    OPENFIGI_KEY_HEADER,
    MappingBatch,
    OpenFigiCredentials,
    OpenFigiProvider,
)
from corollary.data.providers.sec import parse_nport_document  # noqa: E402

FIXTURES: Final = Path(__file__).resolve().parent
SEC_FIXTURES: Final = FIXTURES / "sec"
OUTPUT_DIR: Final = FIXTURES / "openfigi"
NPORT_GLOB: Final = "nport_XL*_primary_doc.xml"
FIXTURE_GLOB: Final = "mapping_batch_*.json"
RECORDER: Final = "tests/fixtures/record_openfigi.py"

#: Spec Q17's table of ISIN-only lines (2026-06-30 filings), in the order the
#: filings list them, funds alphabetically. The recorder refuses to run unless
#: the filings yield exactly this.
EXPECTED_ISIN_ONLY: Final[tuple[tuple[str, str], ...]] = (
    ("XLB", "JE00BV7DQ550"),
    ("XLB", "IE0001827041"),
    ("XLB", "IE000S9YS762"),
    ("XLB", "IE00028FXN24"),
    ("XLB", "NL0009434992"),
    ("XLF", "IE00BLP1HW54"),
    ("XLF", "BMG0450A1053"),
    ("XLF", "BMG3223R1088"),
    ("XLF", "BMG491BT1088"),
    ("XLF", "IE00BDB6Q211"),
    ("XLF", "CH0044328745"),
    ("XLI", "IE00BFRT3W74"),
    ("XLI", "IE00B8KQN827"),
    ("XLI", "IE00BY7QL619"),
    ("XLI", "IE00BLS09M33"),
    ("XLI", "IE00BK9ZQ967"),
    ("XLK", "IE00B4BNMY34"),
    ("XLK", "IE00BKVD2N49"),
    ("XLK", "IE000IVNQZ81"),
    ("XLK", "NL0009538784"),
    ("XLK", "SG9999000020"),
    ("XLP", "CH1300646267"),
    ("XLV", "IE00BTN1Y115"),
    ("XLV", "IE00BFY8C754"),
    ("XLY", "BMG2004J1036"),
    ("XLY", "JE00BTDN8H13"),
    ("XLY", "BMG667211046"),
    ("XLY", "CH0114405324"),
    ("XLY", "LR0008862868"),
)

#: 29 ISINs at the keyless cap of 10 jobs a request.
EXPECTED_BATCH_SIZES: Final = (10, 10, 9)

#: Environment variable names whose values are treated as secrets by the scrub.
SECRET_NAME_MARKERS: Final = ("KEY", "SECRET", "TOKEN", "AGENT")
MIN_SECRET_LENGTH: Final = 8

#: Words that would only be in a fixture if a key, or the header carrying
#: one, had leaked into it. Matched case-insensitively.
KEY_LIKE_WORDS: Final = (OPENFIGI_KEY_HEADER, "apikey", "api_key", "api-key")

NOTE: Final = (
    "Recorded by tests/fixtures/record_openfigi.py, keyless, through "
    "OpenFigiProvider.map_isin_batches (the provider's one exempt mapping "
    "request, spec Q21). 'jobs' is the exact request body; 'response' is "
    "OpenFIGI's decoded reply, unedited. 'context' carries each ISIN's fund "
    "and N-PORT issuer name for the record only -- the name was not sent."
)


@dataclass(frozen=True, slots=True)
class IsinOnlyHolding:
    """One N-PORT equity line identified by ISIN alone."""

    fund: str
    isin: str
    issuer: str


def isin_only_holdings(sec_dir: Path = SEC_FIXTURES) -> list[IsinOnlyHolding]:
    """Every ``EC`` line with no usable CUSIP and a well-formed ISIN, by fund."""
    paths = sorted(sec_dir.glob(NPORT_GLOB))
    if not paths:
        raise SystemExit(f"ABORTED: no N-PORT fixtures match {NPORT_GLOB} in {sec_dir}")
    found: list[IsinOnlyHolding] = []
    for path in paths:
        fund = path.name.split("_")[1]
        document = parse_nport_document(path.read_bytes())
        for holding in document.equity:
            if holding.cusip is None and holding.isin is not None:
                found.append(IsinOnlyHolding(fund=fund, isin=holding.isin, issuer=holding.name))
    return found


def check_against_q17(holdings: Sequence[IsinOnlyHolding]) -> None:
    """Refuse unless ``holdings`` is exactly spec Q17's 29 lines, in order."""
    got = tuple((h.fund, h.isin) for h in holdings)
    if got == EXPECTED_ISIN_ONLY:
        return
    missing = sorted(set(EXPECTED_ISIN_ONLY) - set(got))
    extra = sorted(set(got) - set(EXPECTED_ISIN_ONLY))
    raise SystemExit(
        f"ABORTED: the filings yield {len(got)} ISIN-only lines, not spec Q17's "
        f"{len(EXPECTED_ISIN_ONLY)}; missing {missing}, extra {extra}"
        + ("" if missing or extra else ", same set in a different order")
        + ". No request was made."
    )


def build_provider() -> OpenFigiProvider:
    """The provider, keyless **explicitly** -- never read from the environment."""
    provider = OpenFigiProvider(credentials=OpenFigiCredentials(api_key=None))
    if provider.jobs_per_request != KEYLESS_JOBS_PER_REQUEST:
        raise SystemExit("ABORTED: the provider is not keyless. No request was made.")
    return provider


def environment_secrets(env: Mapping[str, str]) -> tuple[str, ...]:
    """Values of variables named like a secret, long enough to search for."""
    return tuple(
        value
        for name, value in env.items()
        if any(marker in name.upper() for marker in SECRET_NAME_MARKERS)
        and len(value) >= MIN_SECRET_LENGTH
    )


def _refuse_number(value: object) -> SystemExit:
    return SystemExit(
        f"ABORTED: an OpenFIGI reply carries a {type(value).__name__}, which a "
        "mapping reply should not; nothing was written"
    )


def _refuse_non_json(value: object) -> Any:
    """``json.dumps`` default hook: a mapping reply carries text, never a number."""
    raise _refuse_number(value)


def check_text_only(value: object) -> None:
    """Refuse unless every leaf of a decoded reply is a string, a boolean or null.

    Walked before rendering, because the ``default`` hook alone sees only
    types ``json`` cannot already serialise: an ``int`` and a ``float`` --
    including ``NaN`` and ``Infinity``, which the decoder accepts -- would
    otherwise be written back out unremarked. ``bool`` is tested before
    anything numeric because it is a subclass of ``int``.
    """
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise _refuse_number(key)
            check_text_only(item)
        return
    if isinstance(value, list):
        for item in value:
            check_text_only(item)
        return
    if value is None or isinstance(value, (bool, str)):
        return
    raise _refuse_number(value)


def render(
    index: int,
    total: int,
    batch: MappingBatch,
    holdings: Mapping[str, IsinOnlyHolding],
    recorded_at: str,
) -> str:
    """One batch's fixture text. Refuses any reply leaf that is not text."""
    check_text_only(batch.payload)
    envelope: dict[str, Any] = {
        "recorded_at": recorded_at,
        "recorder": RECORDER,
        "note": NOTE,
        "batch": index,
        "batches": total,
        "request": {"method": "POST", "keyed": False, "jobs": list(batch.jobs)},
        "context": [
            {"isin": isin, "fund": holdings[isin].fund, "issuer": holdings[isin].issuer}
            for isin in batch.isins
        ],
        "response": batch.payload,
    }
    return json.dumps(
        envelope, indent=2, ensure_ascii=False, allow_nan=False, default=_refuse_non_json
    ) + "\n"


def scrub_check(name: str, text: str, secrets: Sequence[str]) -> None:
    """Abort if ``text`` carries a key-shaped word or any secret value.

    The message names the file and the *kind* of hit, never the value.
    """
    lowered = text.lower()
    for word in KEY_LIKE_WORDS:
        if word.lower() in lowered:
            raise SystemExit(f"ABORTED: {name} contains a key-like word. Nothing was written.")
    for secret in secrets:
        if secret and secret.lower() in lowered:
            raise SystemExit(
                f"ABORTED: {name} contains an environment secret's value. Nothing was written."
            )


async def record(
    provider: OpenFigiProvider,
    holdings: Sequence[IsinOnlyHolding],
    output_dir: Path,
    *,
    secrets: Sequence[str],
    now: datetime | None = None,
) -> list[Path]:
    """Ask OpenFIGI about ``holdings`` through ``provider`` and write one file per batch.

    Closes ``provider`` whatever happens, including a refusal before any request.
    """
    try:
        check_against_q17(holdings)
        existing = sorted(output_dir.glob(FIXTURE_GLOB)) if output_dir.exists() else []
        if existing:
            raise SystemExit(
                f"ABORTED: refusing to overwrite {[p.name for p in existing]} in "
                f"{output_dir}. No request was made."
            )
        if provider.jobs_per_request != KEYLESS_JOBS_PER_REQUEST:
            raise SystemExit("ABORTED: the provider is not keyless. No request was made.")
        batches = await provider.map_isin_batches([h.isin for h in holdings])
    finally:
        await provider.aclose()

    sizes = tuple(len(b.jobs) for b in batches)
    if sizes != EXPECTED_BATCH_SIZES:
        raise SystemExit(f"ABORTED: batches of {sizes}, not {EXPECTED_BATCH_SIZES}")
    by_isin = {h.isin: h for h in holdings}
    stamp = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    recorded_at = stamp.isoformat(timespec="seconds")

    rendered: list[tuple[Path, str]] = []
    for index, batch in enumerate(batches, start=1):
        out = output_dir / f"mapping_batch_{index}.json"
        text = render(index, len(batches), batch, by_isin, recorded_at)
        scrub_check(out.name, text, secrets)
        rendered.append((out, text))

    output_dir.mkdir(parents=True, exist_ok=True)
    for out, text in rendered:
        out.write_text(text, encoding="utf-8", newline="\n")
        print(f"  wrote {out.name} ({len(text)} bytes)")
    for batch in batches:
        for isin, result in batch.results.items():
            outcome = "matched" if result.matched else ("warning" if result.warning else "error")
            print(f"  {by_isin[isin].fund} {isin} {outcome} records={len(result.records)}")
    return [out for out, _ in rendered]


def main() -> None:
    holdings = isin_only_holdings()
    check_against_q17(holdings)
    provider = build_provider()
    secrets = environment_secrets(os.environ)
    print(
        f"recording {len(holdings)} ISINs keyless in batches of "
        f"{list(EXPECTED_BATCH_SIZES)}; scrubbing against {len(secrets)} environment value(s)"
    )
    asyncio.run(record(provider, holdings, OUTPUT_DIR, secrets=secrets))


if __name__ == "__main__":
    main()
