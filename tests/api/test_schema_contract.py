"""The API contract, checked against ``web/src/lib/types.ts`` field by field.

The design spec's Testing section asks for *"API -- schema round-trip against
the TS types"*, and this is it. The failure it exists to catch is quiet: a
renamed or dropped field typechecks on both sides -- Python is happy, TypeScript
is happy, the browser receives an object with a key the component never reads
-- and the symptom is one empty column on one page.

So the TypeScript is parsed rather than transcribed. A transcription is a
third copy to keep in step, and the first thing anyone would forget to update.
"""

import re
from pathlib import Path
from typing import Mapping

import pytest

from corollary.api import schemas
from corollary.api.schemas import ApiModel

TYPES_TS = (
    Path(__file__).resolve().parents[2] / "web" / "src" / "lib" / "types.ts"
)

#: Every interface this API serves, and the model that mirrors it.
MIRRORED: Mapping[str, type[ApiModel]] = {
    "AccountSnapshot": schemas.AccountSnapshot,
    "ActivityItem": schemas.ActivityItem,
    "ActivityStats": schemas.ActivityStats,
    "ApiKeyPresence": schemas.ApiKeyPresence,
    "AttachedExit": schemas.AttachedExit,
    "AuditLogEntry": schemas.AuditLogEntry,
    "ChainSpec": schemas.ChainSpec,
    "DataFeed": schemas.DataFeed,
    "DataSourceStatus": schemas.DataSourceStatus,
    "IntradayPoint": schemas.IntradayPoint,
    "ManagedExit": schemas.ManagedExit,
    "NotificationRoute": schemas.NotificationRoute,
    "OptionContract": schemas.OptionContract,
    "Position": schemas.Position,
    "PositionLeg": schemas.PositionLeg,
    "PricePoint": schemas.PricePoint,
    "RiskLimit": schemas.RiskLimit,
    "StockQuote": schemas.StockQuote,
    "Trend": schemas.Trend,
    "UnderlyingQuote": schemas.UnderlyingQuote,
    "WorkingOrder": schemas.WorkingOrder,
}

#: Fields the server sends that ``types.ts`` does not declare yet, with the
#: reason. Listed rather than tolerated: an undeclared field is normally a
#: typo, and the only way to tell the two apart is to write down which ones
#: are deliberate.
DELIBERATE_ADDITIONS: Mapping[str, frozenset[str]] = {
    # Decision 10: vendor analytics pass through where Alpaca's own solve
    # succeeds and are derived where it does not, and a chain must record
    # which of the two a number came from.
    "OptionContract": frozenset({"ivSource"}),
}

#: Named TS unions this API reproduces. Values, not just names -- a dropped
#: member is a value the server can send that the client will not narrow.
MIRRORED_UNIONS: Mapping[str, object] = {
    "AccountMode": schemas.AccountMode,
    "ActivityStatus": schemas.ActivityStatus,
    "ActivityAction": schemas.ActivityAction,
    "TimeInForce": schemas.TimeInForce,
    "ExitHolder": schemas.ExitHolder,
    "OrderSide": schemas.OrderSide,
    "RiskLimitKey": schemas.RiskLimitKey,
    "AuditCategory": schemas.AuditCategory,
    "NotificationEvent": schemas.NotificationEvent,
    "FeedKey": schemas.FeedKey,
    "SessionState": schemas.SessionState,
}

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_LINE_COMMENT = re.compile(r"//[^\n]*")
_FIELD = re.compile(r"^  (\w+)\??:", re.MULTILINE)


def _source() -> str:
    text = TYPES_TS.read_text(encoding="utf-8")
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", text))


def ts_interface_fields(name: str) -> frozenset[str]:
    """The top-level field names of one ``export interface``.

    Two-space indentation is the discriminator, which is what keeps the
    members of an inline object literal (``backtest: { winRate: number }``,
    all on one line) out of the result.
    """
    source = _source()
    opener = f"export interface {name} {{"
    start = source.index(opener) + len(opener)
    end = source.index("\n}", start)
    body = source[start:end]
    return frozenset(
        match.group(1) for match in _FIELD.finditer(body) if match.group(1)
    )


def ts_union_members(name: str) -> frozenset[str]:
    """The string literals of an ``export type X = 'a' | 'b'``."""
    source = _source()
    opener = f"export type {name} ="
    start = source.index(opener) + len(opener)
    end = source.index("\n\n", start)
    return frozenset(re.findall(r"'([^']*)'", source[start:end]))


def wire_fields(model: type[ApiModel]) -> frozenset[str]:
    """What this model actually puts on the wire, aliases resolved."""
    return frozenset(
        info.alias or name for name, info in model.model_fields.items()
    )


def python_union_members(target: object) -> frozenset[str]:
    import enum
    import typing

    if isinstance(target, type) and issubclass(target, enum.Enum):
        return frozenset(str(member.value) for member in target)
    return frozenset(str(arg) for arg in typing.get_args(target))


def test_the_types_file_is_where_it_is_expected() -> None:
    assert TYPES_TS.exists(), TYPES_TS


@pytest.mark.parametrize("name", sorted(MIRRORED))
def test_every_ts_field_is_served(name: str) -> None:
    missing = ts_interface_fields(name) - wire_fields(MIRRORED[name])

    assert not missing, f"{name} is missing {sorted(missing)}"


@pytest.mark.parametrize("name", sorted(MIRRORED))
def test_the_server_sends_nothing_undeclared(name: str) -> None:
    """Both directions, because a deliberate addition is a temporary state.

    The failure that reads oddly otherwise is the *second* one: once
    ``types.ts`` declares a field that was listed here, ``extra`` is empty
    and the old message said the model sent nothing types.ts did not
    declare. Naming which side is stale is what makes that a one-line fix
    rather than a puzzle.
    """
    extra = wire_fields(MIRRORED[name]) - ts_interface_fields(name)
    deliberate = DELIBERATE_ADDITIONS.get(name, frozenset())

    assert extra == deliberate, (
        f"{name} sends {sorted(extra - deliberate)}, which types.ts does not "
        f"declare; and DELIBERATE_ADDITIONS still lists "
        f"{sorted(deliberate - extra)}, which types.ts now declares"
    )


@pytest.mark.parametrize("name", sorted(MIRRORED_UNIONS))
def test_unions_have_the_same_members(name: str) -> None:
    assert python_union_members(MIRRORED_UNIONS[name]) == ts_union_members(name)


def test_the_parser_finds_something(
) -> None:
    """A parser that silently matched nothing would make every test above pass."""
    assert ts_interface_fields("Trend") == {"changePct", "comparedTo"}
    assert ts_union_members("AccountMode") == {"paper", "cash"}


def test_volume24h_keeps_its_lowercase_h() -> None:
    """The one alias ``to_camel`` cannot generate, pinned on its own.

    ``to_camel`` uppercases the letter after a digit, so ``volume_24h``,
    ``volume24h`` and ``volume_24_h`` all produce ``volume24H``. Remove the
    explicit alias and the Account page loses its volume card with nothing
    raising.
    """
    from pydantic.alias_generators import to_camel

    assert to_camel("volume_24h") == "volume24H"
    assert "volume24h" in wire_fields(schemas.AccountSnapshot)
