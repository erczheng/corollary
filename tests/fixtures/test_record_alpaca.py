"""The recorder's serialiser and scrubber. The recorder never runs in the suite.

``dumps_exact`` is the half of the fixture pipeline that decides whether the
files on disk carry Alpaca's digits or Python's ``repr`` of a double. It has a
test because the answer is invisible in the output: a fixture written the
lossy way looks identical for every value that happens to round-trip, which is
almost all of them.

``scrub`` is the other half, and it has the same property for a different
reason. It used to collapse every ``id``, ``asset_id``, ``underlying_asset_id``
and ``client_order_id`` onto one placeholder, on the stated grounds that
"nothing in this codebase reads them". Step 4 made that false: the multi-leg
join is **two-hop** -- a fill carries its *leg's* order id and the parent id
appears nowhere on it -- so the leg-id to parent-id map built from ``legs[]``
is the only route to the parent. Collapse the ids and that map becomes a map
from one key to itself, the composite activity id vanishes, and
``fill(activity_id UNIQUE)`` has nothing to be unique on. The fixtures would
replay green and prove nothing.

The tests below are what stops someone re-tightening ``_PSEUDONYM_KEYS`` later
and silently hollowing the fixtures out again.
"""

import json
import os
import re
import sys
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
# And the repo root, so the write-verb list can be *imported* from the guard
# that owns it rather than copied alongside it. ``tests`` is a package and
# this file's own directory is not, which is why both entries are needed.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from record_alpaca import (  # noqa: E402
    REDACTED,
    REDACTED_SUBSTRING,
    SECTIONS,
    dumps_exact,
    parse_sections,
    scrub,
)
from tests.test_hard_rules import WRITE_VERBS, recorders  # noqa: E402

FIXTURES_ROOT = Path(__file__).resolve().parent
FIXTURE_DIR = FIXTURES_ROOT / "alpaca"
REPO_ROOT = Path(__file__).resolve().parents[2]

#: Every vendor's recorded bodies, relative to :data:`FIXTURES_ROOT`.
#:
#: One level of directory rather than ``alpaca/`` by name. The sweep below
#: was written for the Alpaca fixtures because those were the only ones; the
#: day ``finnhub/`` appeared beside them, a secrets guard scoped to one
#: vendor's directory was the same miss the sweep itself was widened to fix.
#: Rule 6 is not per vendor.
FIXTURE_JSON = "*/*.json"

#: Twenty-three significant figures. A double holds about seventeen, so this
#: is a value that cannot survive ``json.loads`` without ``parse_float``.
BEYOND_A_DOUBLE = "205.67753612345678901234"


def test_a_value_no_double_can_hold_survives_the_round_trip() -> None:
    payload = json.loads(
        '{"bars": {"NVDA": [{"vw": ' + BEYOND_A_DOUBLE + "}]}}",
        parse_float=Decimal,
    )
    text = dumps_exact(payload)
    assert BEYOND_A_DOUBLE in text
    assert json.loads(text, parse_float=Decimal) == payload


def test_the_lossy_path_is_what_this_replaces() -> None:
    """Proof the test above is not vacuous.

    ``json.dumps(json.loads(text))`` -- what the recorder used to do -- loses
    the tail of the value silently and writes something that still looks like
    a price.
    """
    lossy = json.dumps(json.loads('{"vw": ' + BEYOND_A_DOUBLE + "}"))
    assert BEYOND_A_DOUBLE not in lossy


@pytest.mark.parametrize(
    "literal",
    [
        "-1.5e-9",  # Decimal renders this as -1.5E-9, which is valid JSON
        "0",
        "0.0",
        "1e2",
        "-0.000001",
        "123456789012345678901234567890",
    ],
)
def test_awkward_numbers_come_back_as_numbers_not_strings(literal: str) -> None:
    """The sentinel must be unwrapped, not left quoted.

    ``json.dumps`` writes a control character as a ``\\u0001`` escape rather
    than as itself, so a pattern matching the raw byte silently matches
    nothing and every number in the fixture becomes a string. Nothing would
    error; the fixtures would just stop being JSON numbers.
    """
    payload = json.loads('{"v": ' + literal + "}", parse_float=Decimal)
    reparsed = json.loads(dumps_exact(payload), parse_float=Decimal)
    assert not isinstance(reparsed["v"], str)
    assert reparsed == payload


def test_a_string_that_looks_like_a_number_stays_a_string() -> None:
    """The trading API sends money as strings; those must not be unquoted."""
    payload = {"avg_entry_price": "8.21", "qty": "2"}
    assert json.loads(dumps_exact(payload)) == payload


def test_an_unserialisable_value_raises_rather_than_being_stringified() -> None:
    with pytest.raises(TypeError):
        dumps_exact({"when": object()})


# --------------------------------------------------------------------------
# The scrubber
# --------------------------------------------------------------------------

#: One ``mleg`` parent with two legs, plus the two fills those legs produced.
#: Trimmed to the fields the relation depends on. The ids here are invented but
#: shaped like Alpaca's: UUIDs for orders, ``<stamp>::<uuid>`` for activities.
NESTED_ORDER = {
    "id": "83f37e9f-6b1f-49ed-8fc6-3e6af716323f",
    "order_class": "mleg",
    "client_order_id": "646b1fe6-b212-4f54-94c6-429e7bcdee04",
    "legs": [
        {
            "id": "df4ff24a-c58a-4e37-8b9f-ef32b83a11f2",
            "symbol": "AAPL241213C00250000",
            "ratio_qty": "3",
        },
        {
            "id": "ecd91110-c34d-4e9d-a7bf-a9c27c40f8b5",
            "symbol": "AAPL241213C00260000",
            "ratio_qty": "1",
        },
    ],
}

FILLS = [
    {
        "activity_type": "FILL",
        "id": "20260910131125598::68cda3e9-0e3f-4a15-9d0e-5b1f8f62c0aa",
        "order_id": "df4ff24a-c58a-4e37-8b9f-ef32b83a11f2",
        "qty": "3",
    },
    {
        "activity_type": "FILL",
        "id": "20260910131125599::7b1c0d22-2f47-4f0a-8c3b-9de0a1b2c3d4",
        "order_id": "ecd91110-c34d-4e9d-a7bf-a9c27c40f8b5",
        "qty": "1",
    },
]

#: ``<digits>::<uuid>``. The activity id is composite, not a UUID. Its
#: 17-digit **stamp** sorts chronologically as a string, which is why the
#: pseudonymiser keeps that half intact; the whole id does not sort, because
#: stamps repeat inside a millisecond and the UUID half then breaks the tie
#: arbitrarily. What the id is used for is Alpaca's own ``page_token`` -- the
#: resume cursor for ingestion -- and the unique key on ``fill``.
COMPOSITE_ID = re.compile(r"^\d{10,}::[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}$")


def test_the_leg_to_parent_relation_survives_scrubbing() -> None:
    """The two-hop join has to still be walkable in the fixtures.

    A fill's ``order_id`` is a *leg* id. Reaching the parent means matching it
    against an id inside ``legs[]``, so scrubbing has to be injective across
    the whole recording rather than merely reversible-looking within one
    object.
    """
    order = scrub(NESTED_ORDER)
    fills = scrub(FILLS)

    leg_to_parent = {leg["id"]: order["id"] for leg in order["legs"]}
    assert len(leg_to_parent) == 2, "the two legs collapsed onto one id"
    assert order["id"] not in leg_to_parent, "a leg id equals the parent's"

    parents = [leg_to_parent.get(fill["order_id"]) for fill in fills]
    assert parents == [order["id"], order["id"]]


def test_a_scrubbed_activity_id_still_matches_the_composite_format() -> None:
    for fill in scrub(FILLS):
        assert COMPOSITE_ID.match(fill["id"]), fill["id"]


def test_the_timestamp_half_of_an_activity_id_is_kept_so_it_still_sorts() -> None:
    """Chronological string sort is the property ingestion resumes on."""
    scrubbed = [fill["id"] for fill in scrub(FILLS)]
    assert [entry.split("::")[0] for entry in scrubbed] == [
        "20260910131125598",
        "20260910131125599",
    ]
    assert scrubbed == sorted(scrubbed)


def test_an_account_number_is_still_hard_redacted() -> None:
    """The one genuinely sensitive field on any of these responses."""
    scrubbed = scrub({"id": "abc", "account_number": "PA3ABCDEFGHI", "cash": "1000"})
    assert scrubbed["account_number"] == REDACTED
    assert scrubbed["cash"] == "1000"
    assert scrubbed["id"] != REDACTED


def test_a_client_order_id_is_still_hard_redacted() -> None:
    assert scrub(NESTED_ORDER)["client_order_id"] == REDACTED


def test_pseudonyms_are_stable_and_injective() -> None:
    """Same input, same output; distinct inputs, distinct outputs.

    Stability is what lets a re-recording produce a readable diff. Injectivity
    is what keeps the leg-to-parent relation a relation.
    """
    ids = [
        "df4ff24a-c58a-4e37-8b9f-ef32b83a11f2",
        "ecd91110-c34d-4e9d-a7bf-a9c27c40f8b5",
        "83f37e9f-6b1f-49ed-8fc6-3e6af716323f",
    ]
    once = [scrub({"id": value})["id"] for value in ids]
    twice = [scrub({"id": value})["id"] for value in ids]
    assert once == twice
    assert len(set(once)) == len(ids)


def test_a_pseudonym_is_not_the_original_value() -> None:
    """Otherwise this is not a scrubber at all."""
    original = "df4ff24a-c58a-4e37-8b9f-ef32b83a11f2"
    assert scrub({"id": original})["id"] != original


def test_a_group_id_survives_because_it_is_a_non_trade_row_only_linkage() -> None:
    """Non-trade activities carry no ``order_id`` at all.

    ``group_id`` -- "ID used to link activities who share a sibling
    relationship" -- is the whole of it, so collapsing it would erase the pair
    structure of every option event.
    """
    rows = scrub(
        [
            {"activity_type": "OPEXC", "group_id": "g-one", "net_amount": "0"},
            {"activity_type": "OPTRD", "group_id": "g-one", "net_amount": "-30000"},
            {"activity_type": "OPEXP", "group_id": "g-two", "net_amount": "0"},
        ]
    )
    assert rows[0]["group_id"] == rows[1]["group_id"]
    assert rows[0]["group_id"] != rows[2]["group_id"]


# --------------------------------------------------------------------------
# Identifiers inside free text
# --------------------------------------------------------------------------

#: Invented, and shaped like Alpaca's paper account numbers (``PA`` then ten
#: alphanumerics). The real one is never in this repository, which is the
#: whole point of the tests below.
#:
#: That sentence was briefly false, and in the same change set that wrote it:
#: the real number went into a module comment, a docstring and the design spec
#: -- three pieces of prose *explaining* the redaction, none of them reachable
#: by a sweep that globbed ``tests/fixtures/alpaca/*.json``. It is this value
#: that stands in all three places now, and the sweep below reads every ``.py``
#: and ``.md`` in the tree so that the next one is caught where it happens
#: rather than in a review.
FAKE_ACCOUNT_NUMBER = "PA0EXAMPLE00"

#: The row that found the hole. A live ``FEE`` activity's ``description``
#: carries the account number in prose, one object away from the
#: ``account_number`` field the scrubber was dutifully blanking.
FEE_ROW = {
    "activity_type": "FEE",
    "activity_sub_type": "CAT",
    "description": (
        f"CAT fee for proceed of 15 trades on 2026-09-10 by {FAKE_ACCOUNT_NUMBER}"
    ),
    "net_amount": "-0.01",
}


def test_an_account_number_in_free_text_is_blanked_too() -> None:
    """Field-level redaction alone is not enough, and was not.

    The first trading recording wrote the account number into
    ``activities_non_trade.json`` eight times, in the ``description`` of every
    ``FEE`` row, while ``account_number`` itself was correctly redacted. A
    field-name rule cannot see inside prose; this pass can.
    """
    scrubbed = scrub(FEE_ROW, (FAKE_ACCOUNT_NUMBER,))
    assert FAKE_ACCOUNT_NUMBER not in scrubbed["description"]
    assert REDACTED_SUBSTRING in scrubbed["description"]
    # The rest of the sentence survives -- a fixture still has to say what
    # happened.
    assert "CAT fee for proceed of 15 trades" in scrubbed["description"]


def test_the_substring_pass_is_case_insensitive() -> None:
    """Redacting more broadly than ``save`` scans is deliberate.

    ``save``'s abort scan is case-sensitive, so a case-insensitive redaction
    means the scan can only ever fire on something genuinely missed rather
    than on a spelling difference.
    """
    scrubbed = scrub({"note": f"by {FAKE_ACCOUNT_NUMBER.lower()}"}, (FAKE_ACCOUNT_NUMBER,))
    assert FAKE_ACCOUNT_NUMBER.lower() not in scrubbed["note"]


def test_no_identifiers_means_free_text_is_untouched() -> None:
    """The market half passes none, and must be byte-for-byte unchanged."""
    assert scrub(FEE_ROW) == FEE_ROW


def test_an_identifier_is_blanked_at_any_depth() -> None:
    nested = {"activities": [{"inner": {"description": FAKE_ACCOUNT_NUMBER}}]}
    scrubbed = scrub(nested, (FAKE_ACCOUNT_NUMBER,))
    assert (
        scrubbed["activities"][0]["inner"]["description"] == REDACTED_SUBSTRING
    )


#: Key-shaped literals this repository commits **on purpose**, every one of
#: them invented. :func:`offenders` subtracts them, so adding a value here is a
#: deliberate statement that it is fabricated -- and the real identifiers,
#: being absent from it, still fire. It excuses *values*, never files: a file
#: cannot be waved through, only a value someone has vouched for.
PLACEHOLDER_IDENTIFIERS = frozenset(
    {
        # This file, and the prose in three other files that documents the leak.
        FAKE_ACCOUNT_NUMBER,
        # ``test_an_account_number_is_still_hard_redacted`` above, and
        # deliberately a *different* invented number: that test passes no
        # identifiers at all, so it proves the field rule works without the
        # substring pass, and one shared constant would blur which mechanism
        # was under test.
        "PA3ABCDEFGHI",
        # tests/engine/execution/conftest.py, tests/data/providers/conftest.py,
        # and the tests that assert on the headers they produce.
        "PKTESTTESTTESTTEST",
        # tests/engine/execution/test_alpaca_transport.py: ``from_env`` and the
        # repr-masking test.
        "PKFAKEFAKEFAKEFAKE",
        "PKVISIBLEVISIBLE",
        # ditto: the error body that echoes a full-width secret back at the
        # broker, which is the shape rule 6 cares about most.
        "NOTAREALSECRETnotarealsecret000000000000",
        # tests/data/providers/test_alpaca_provider.py: the same echoed body
        # aimed at the *provider*, whose ``_get`` had no redaction at all until
        # the helper moved to ``corollary.wire``. A separate invented value
        # from the broker's, so a test that passed only because the broker's
        # constant happened to be redacted would still fail here.
        "PROVIDERnotarealsecretPROVIDERnotareal00",
        "PA9PROVIDER0",
        # tests/test_wire.py: the redaction's own direct coverage, at the
        # module that owns it. Distinct again, and for the same reason.
        "WIREnotarealsecretWIREnotarealsecret0000",
        "PKWIREWIREWIREWIRE",
        "PA7WIREFAKE0",
        # tests/api/test_account_routes.py: the transfers route drops a cash
        # activity it cannot render and logs the row's ``description``, which
        # is where this vendor puts an account number in prose. Distinct from
        # every value above for the same reason they are distinct from each
        # other -- a test that passed only because somebody else's constant
        # happened to be redacted would prove nothing about this route.
        "PAEXAMPLE000",
    }
)

#: Not descended into. ``.git`` and ``__pycache__`` hold derived copies of
#: files that are swept anyway, ``.venv`` and ``node_modules`` are other
#: people's source, and the caches are output. ``.claude/worktrees`` is another
#: agent's private tree of this same repository -- not this commit's to answer
#: for, and sweeping it would report every finding twice. Pruning them is what
#: holds the walk to about a hundred files.
SWEEP_SKIP_DIRS = frozenset(
    {".git", ".venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache"}
)

WORKTREES = REPO_ROOT / ".claude" / "worktrees"

#: Swept everywhere under the root, because prose is where the leak actually
#: was. ``.json`` is swept only under :data:`FIXTURES_ROOT` -- the recorders'
#: own output, and the reason any of this exists.
SWEEP_SUFFIXES = (".py", ".md")


def swept_paths(root: Path) -> list[Path]:
    """Every file the sweep reads, under ``root``."""
    found: list[Path] = []
    for directory, subdirectories, filenames in os.walk(root):
        here = Path(directory)
        subdirectories[:] = [
            name
            for name in sorted(subdirectories)
            if name not in SWEEP_SKIP_DIRS and here / name != WORKTREES
        ]
        found.extend(
            here / name for name in sorted(filenames) if name.endswith(SWEEP_SUFFIXES)
        )
    if FIXTURES_ROOT.is_relative_to(root):
        found.extend(sorted(FIXTURES_ROOT.glob(FIXTURE_JSON)))
    return found


def offenders(compiled: "re.Pattern[str]", root: Path) -> dict[str, list[str]]:
    """Matches of ``compiled`` under ``root``, minus the registered placeholders.

    ``errors="ignore"`` because an identifier is ASCII: a byte that fails to
    decode cannot be part of one, and one oddly-encoded file must not turn this
    guard into an error instead of an answer.
    """
    found: dict[str, list[str]] = {}
    for path in swept_paths(root):
        text = path.read_text(encoding="utf-8", errors="ignore")
        hits = sorted(set(compiled.findall(text)) - PLACEHOLDER_IDENTIFIERS)
        if hits:
            found[path.relative_to(root).as_posix()] = hits
    return found


#: The shapes a credential takes here. **Alpaca's three, and a stated gap.**
#:
#: The sweep's *scope* is every vendor -- ``.py``, ``.md`` and every
#: recorded body under ``fixtures/*/`` -- but its *vocabulary* is
#: single-vendor, and deliberately stays that way. A Finnhub token is about
#: twenty lowercase alphanumerics with no prefix to anchor on, so matching it
#: means matching bare ``[0-9a-z]{20}``: abbreviated hashes, base32 ids,
#: request ids, and half the invented constants in this repository. A secret
#: scanner that cries wolf is one that gets waved through, and the pattern
#: that fires on ordinary test data would take the three below down with it.
#:
#: What carries that vendor instead is the **record-time** scrub in
#: ``record_finnhub.py`` (``save``'s ``secrets`` check), which knows the
#: literal token because it just used it, and aborts the run with nothing on
#: disk rather than guessing at a shape. That is the load-bearing guard for
#: Finnhub; this sweep is defence in depth behind it and does not reach it.
#: Written down because a known gap behaves differently from an unnoticed
#: one: the next person to add a vendor needs to know the scan will not cover
#: their token, so the record-time abort is not optional there.
IDENTIFIER_SHAPES = [
    (r"(?<![0-9A-Z])PA[0-9A-Z]{10}(?![0-9A-Z])", "an account number"),
    (r"(?<![0-9A-Z])PK[0-9A-Z]{14,}(?![0-9A-Z])", "an API key id"),
    (r"(?<![0-9A-Za-z])[0-9A-Za-z]{40}(?![0-9A-Za-z])", "an API secret key"),
]

#: One sample per shape, for the plant test below, assembled from halves.
#: Written as plain literals they would match the sweep in *this* file and so
#: would have to be excused in :data:`PLACEHOLDER_IDENTIFIERS` -- and a planted
#: value the sweep is told to ignore plants nothing.
PLANTED = {
    "an account number": "PA" + "PLANTED123",
    "an API key id": "PK" + "PLANTEDPLANTED",
    "an API secret key": ("PLANTEDSECRET" * 4)[:40],
}


@pytest.mark.parametrize("pattern, what", IDENTIFIER_SHAPES)
def test_no_file_in_the_repository_carries_an_account_identifier(
    pattern: str, what: str
) -> None:
    """A shape-based sweep over the whole working tree, not just the fixtures.

    Shape-based because the alternative is worse: a test that knew the real
    account number would have to contain it, and rule 6 says no identifier
    lives in a test or a fixture. So this matches the *form* -- ``PA`` plus ten
    alphanumerics for a paper account number, ``PK`` plus a long tail for a key
    id, forty alphanumerics for a secret -- and every one of those forms is
    absent here, so a hit is a regression rather than a baseline.

    **It sweeps ``.py`` and ``.md`` because that is where the leak was.** The
    version this replaced globbed ``tests/fixtures/alpaca/*.json`` alone, and
    the fixtures were clean -- while the real account number sat in three files
    it structurally could not see: a module comment, a docstring, and the
    design spec, all three of them prose *documenting* the redaction. A guard
    that watches only the surface that was fixed is not watching the one that
    failed.

    The secret-key shape is defence in depth: ``save``'s literal scan already
    aborts a recording that carries one, and this catches the copy that never
    went through ``save``. Forty alphanumerics is a broad shape, and the thing
    it would otherwise match is a full 40-character git SHA -- nothing here
    quotes one, and the remedy if something ever does is to abbreviate it,
    which is the convention anyway.
    """
    found = offenders(re.compile(pattern), REPO_ROOT)
    assert not found, (
        f"{what} appears in {found}. If one of those is invented, register the "
        "value in PLACEHOLDER_IDENTIFIERS and say where it is used. If it is "
        "real, it does not belong in this repository at any strength of "
        "justification -- rule 6 has no exception for documentation."
    )


def test_the_sweep_reads_every_vendors_recorded_bodies() -> None:
    """A second vendor's fixtures are in scope, not just the first one's.

    The ``.json`` half of the sweep globbed ``fixtures/alpaca/*.json``, which
    was every recorded body there was until ``fixtures/finnhub/`` arrived.
    A secret in the new directory would have missed the guard that exists for
    exactly that -- the same shape as the miss the ``.py``/``.md`` half was
    widened to fix, one directory further in.

    Asserts the glob is populated as well as included: a pattern matching
    nothing sweeps nothing and says so by passing.
    """
    recorded = set(FIXTURES_ROOT.glob(FIXTURE_JSON))
    assert recorded, "no recorded fixtures found; the glob has gone stale"
    assert {path.parent.name for path in recorded} >= {"alpaca", "finnhub"}
    assert recorded <= set(swept_paths(REPO_ROOT))


@pytest.mark.parametrize("pattern, what", IDENTIFIER_SHAPES)
def test_the_sweep_can_see_a_python_file_and_a_markdown_file(
    tmp_path: Path, pattern: str, what: str
) -> None:
    """Proof the sweep above is not vacuous, one shape at a time.

    Three patterns matching nothing are indistinguishable from three patterns
    matching nothing *because they are wrong*, and the version this replaced
    could not have seen a ``.py`` or a ``.md`` at all. Each shape is planted in
    both, mid-sentence, the way the real one was -- and in two places the sweep
    must ignore, so that "sees everything" does not quietly mean "reads the
    whole disk".
    """
    value = PLANTED[what]
    assert re.search(pattern, value), "the sample must match the shape it plants"

    (tmp_path / "notes.md").write_text(f"the fee row ended `by {value}`\n", "utf-8")
    (tmp_path / "leak.py").write_text(f"#: recorded as {value}\n", "utf-8")
    (tmp_path / "quiet.txt").write_text(value, "utf-8")
    (tmp_path / "__pycache__").mkdir()
    (tmp_path / "__pycache__" / "cached.py").write_text(value, "utf-8")

    assert offenders(re.compile(pattern), tmp_path) == {
        "leak.py": [value],
        "notes.md": [value],
    }


# --------------------------------------------------------------------------
# The CLI
# --------------------------------------------------------------------------


def test_no_argument_records_everything() -> None:
    assert parse_sections([]) == frozenset(SECTIONS)


@pytest.mark.parametrize("name", SECTIONS)
def test_one_section_records_only_that_half(name: str) -> None:
    assert parse_sections([name]) == frozenset({name})


def test_a_section_name_is_normalised() -> None:
    assert parse_sections([" TRADING "]) == frozenset({"trading"})


def test_an_unknown_section_exits_rather_than_recording_nothing() -> None:
    """Being ignored is the worst available outcome for a typo.

    ``tradng`` would match neither branch, both would be skipped, and the run
    would print ``done`` having written not one file and spent not one
    request -- indistinguishable from success.
    """
    with pytest.raises(SystemExit) as raised:
        parse_sections(["tradng"])
    assert "tradng" in str(raised.value)
    assert "trading" in str(raised.value)


# --------------------------------------------------------------------------
# Rule 1: the recorder is read-only, checked twice
#
# ``test_the_recorder_can_only_issue_gets`` used to be the whole of it. The
# parsed half now lives in ``tests/test_hard_rules.py``, which walks the
# vendor surface -- the two package files *and this recorder*, the only other
# thing in the tree holding live credentials -- and refuses any write verb
# anywhere on it. That guard parses instead of grepping, so it catches shapes
# the substring version missed: a ``request("POST", ...)`` reached through a
# local alias, a module-level ``httpx.post(...)``, a ``.send()`` of a
# prebuilt request. It carries ``@pytest.mark.risk``.
#
# What parsing gives up is **text**, and the substring version below is
# restored rather than left deleted. A ``.post(`` sitting in a comment or a
# docstring is invisible to an AST and one diff away from being code, which
# is the exact argument that keeps ``test_settings_reaches_no_order_path``
# alive in ``tests/api/test_settings_routes.py``. Every recorder is in the
# same position -- none holds prose about these verbs, none has any reason to
# grow some -- so they get the same guard for the same stated reason. If one
# of them ever needs to *discuss* a write verb, delete the text half there
# and say why; do not weaken the parsed one.
#
# **Both halves walk the same scope, and that took two goes.** The parsed
# half was generalised to every ``record_*.py`` when ``record_finnhub.py``
# arrived in the scope of no guard at all; this half was left reading one
# hardcoded path, so a commented-out ``client.post(...)`` -- left as a
# re-recording note in a script that loads ``.env`` and holds live
# credentials -- failed nothing in the new file while the identical line
# failed immediately in the old one. Half a guard on half the files is the
# same miss one directory further in.
#
# This file stays bare of ``@pytest.mark.risk``, being dev tooling that is
# never on the "before any engine change" path.
# --------------------------------------------------------------------------


def test_no_recorder_names_a_write_verb_even_in_prose() -> None:
    """Read-only probes, enforced rather than intended -- the text half.

    Phase 2 places no order at all, and a recorder that could POST would be a
    write path to the broker sitting outside ``RiskManager.approve()``.
    ``tests/test_hard_rules.py`` proves no write verb is *called*; this proves
    none is *written*, commented out or otherwise.

    The verbs **and the files** are imported from that module, not restated
    here. A hand-copied verb list is content-identical right up to the day
    Alpaca invents a seventh verb -- which is the eventuality the parsed
    guard's own docstring anticipates. A hand-named file is worse, and was
    worse: this read ``record_alpaca.py`` alone while the parsed half had
    already moved to every ``record_*.py``, so the second recorder had the
    AST guard and not this one. ``recorders()`` carries its own floor, so an
    empty scope raises there rather than passing here.

    Silent scope decay is the exact failure ``tests/test_hard_rules.py``
    names as its reason for existing, and it would be a poor joke to
    reproduce it in the guard standing beside it. Twice.
    """
    assert WRITE_VERBS, "the verb list arrived empty; a guard over nothing passes"
    offenders = []
    for path in recorders():
        source = path.read_text(encoding="utf-8")
        offenders.extend(
            f"{path.name}: .{verb}("
            for verb in sorted(WRITE_VERBS)
            if f".{verb}(" in source
        )
    assert offenders == [], f"a recorder names a write verb in text: {offenders}"
