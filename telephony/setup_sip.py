#!/usr/bin/env python
"""Create or update the LiveKit inbound SIP trunk and dispatch rule for the receptionist.

Idempotent: run it again after changing a flag and it updates the existing trunk/rule (matched by
name) instead of creating duplicates.

    python telephony/setup_sip.py --number +15551234567            # apply
    python telephony/setup_sip.py --number +15551234567 --dry-run  # show what would change
    python telephony/setup_sip.py --number +15551234567 --print-json  # offline, no credentials

Credentials come from LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET (a `.env` in the current
directory is loaded if present). The rule dispatches the worker named by AGENT_NAME, as the voice
agent registers it. See telephony/README.md for the carrier side.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol, get_args

from google.protobuf import json_format
from livekit import api

from receptionist.agent.settings import SUPPORTED_LANGUAGES, AgentSettings, Mode

DEFAULT_AGENT_NAME: str = AgentSettings.model_fields["agent_name"].default
ROOM_PREFIX = "call-"
DEFAULT_TRUNK_NAME = "lakeside-inbound"
DEFAULT_RULE_NAME = "lakeside-receptionist"
DEFAULT_DISPATCH_METADATA: dict[str, str] = {
    "mode": "realtime",
    "language": "auto",
}
_MODES: tuple[str, ...] = get_args(Mode)

Action = Literal["created", "updated", "unchanged", "would create", "would update"]


@dataclass(frozen=True, slots=True)
class SipSetup:
    """Desired state for the clinic's phone line."""

    numbers: tuple[str, ...]
    trunk_name: str = DEFAULT_TRUNK_NAME
    rule_name: str = DEFAULT_RULE_NAME
    allowed_addresses: tuple[str, ...] = ()
    # Caller IDs allowed to reach the line; empty means anyone. Handy for a private demo.
    allowed_callers: tuple[str, ...] = ()
    krisp_enabled: bool = True
    agent_name: str = DEFAULT_AGENT_NAME
    dispatch_metadata: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_DISPATCH_METADATA)
    )


@dataclass(frozen=True, slots=True)
class Result:
    trunk_id: str
    trunk_action: Action
    rule_id: str
    rule_action: Action


class SipApi(Protocol):
    """The subset of `livekit.api.SipService` this script uses (lets tests pass a fake)."""

    async def list_inbound_trunk(
        self, list: api.ListSIPInboundTrunkRequest
    ) -> api.ListSIPInboundTrunkResponse: ...

    async def create_inbound_trunk(
        self, create: api.CreateSIPInboundTrunkRequest
    ) -> api.SIPInboundTrunkInfo: ...

    async def update_inbound_trunk(
        self, trunk_id: str, trunk: api.SIPInboundTrunkInfo
    ) -> api.SIPInboundTrunkInfo: ...

    async def list_dispatch_rule(
        self, list: api.ListSIPDispatchRuleRequest
    ) -> api.ListSIPDispatchRuleResponse: ...

    async def create_dispatch_rule(
        self, create: api.CreateSIPDispatchRuleRequest
    ) -> api.SIPDispatchRuleInfo: ...

    async def update_dispatch_rule(
        self, rule_id: str, rule: api.SIPDispatchRuleInfo
    ) -> api.SIPDispatchRuleInfo: ...


# -- desired state ----------------------------------------------------------------------------


def build_inbound_trunk(setup: SipSetup) -> api.SIPInboundTrunkInfo:
    return api.SIPInboundTrunkInfo(
        name=setup.trunk_name,
        numbers=list(setup.numbers),
        allowed_addresses=list(setup.allowed_addresses),
        allowed_numbers=list(setup.allowed_callers),
        krisp_enabled=setup.krisp_enabled,
    )


def build_dispatch_rule(setup: SipSetup, trunk_id: str) -> api.SIPDispatchRuleInfo:
    """One room per caller (`call-…`), with the receptionist dispatched into it on creation."""
    return api.SIPDispatchRuleInfo(
        name=setup.rule_name,
        trunk_ids=[trunk_id],
        rule=api.SIPDispatchRule(
            dispatch_rule_individual=api.SIPDispatchRuleIndividual(room_prefix=ROOM_PREFIX)
        ),
        room_config=api.RoomConfiguration(
            agents=[
                api.RoomAgentDispatch(
                    agent_name=setup.agent_name,
                    metadata=json.dumps(setup.dispatch_metadata, separators=(", ", ": ")),
                )
            ]
        ),
    )


def _trunk_differs(existing: api.SIPInboundTrunkInfo, desired: api.SIPInboundTrunkInfo) -> bool:
    return (
        existing.name != desired.name
        or list(existing.numbers) != list(desired.numbers)
        or list(existing.allowed_addresses) != list(desired.allowed_addresses)
        or list(existing.allowed_numbers) != list(desired.allowed_numbers)
        or existing.krisp_enabled != desired.krisp_enabled
    )


def _rule_differs(existing: api.SIPDispatchRuleInfo, desired: api.SIPDispatchRuleInfo) -> bool:
    return (
        existing.name != desired.name
        or list(existing.trunk_ids) != list(desired.trunk_ids)
        or existing.rule != desired.rule
        or existing.room_config != desired.room_config
    )


def _merge_trunk(
    existing: api.SIPInboundTrunkInfo, desired: api.SIPInboundTrunkInfo
) -> api.SIPInboundTrunkInfo:
    """Replace only the fields we manage; keep anything set by hand (auth, headers, media)."""
    merged = api.SIPInboundTrunkInfo()
    merged.CopyFrom(existing)
    for name in ("sip_trunk_id", "created_at", "updated_at"):
        merged.ClearField(name)
    merged.name = desired.name
    merged.krisp_enabled = desired.krisp_enabled
    del merged.numbers[:]
    merged.numbers.extend(desired.numbers)
    del merged.allowed_addresses[:]
    merged.allowed_addresses.extend(desired.allowed_addresses)
    del merged.allowed_numbers[:]
    merged.allowed_numbers.extend(desired.allowed_numbers)
    return merged


def _merge_rule(
    existing: api.SIPDispatchRuleInfo, desired: api.SIPDispatchRuleInfo
) -> api.SIPDispatchRuleInfo:
    merged = api.SIPDispatchRuleInfo()
    merged.CopyFrom(existing)
    for name in ("sip_dispatch_rule_id", "created_at", "updated_at"):
        merged.ClearField(name)
    merged.name = desired.name
    del merged.trunk_ids[:]
    merged.trunk_ids.extend(desired.trunk_ids)
    merged.rule.CopyFrom(desired.rule)
    merged.room_config.CopyFrom(desired.room_config)
    return merged


# -- reconcile --------------------------------------------------------------------------------


async def ensure_inbound_trunk(
    sip: SipApi, setup: SipSetup, *, dry_run: bool = False
) -> tuple[str, Action]:
    desired = build_inbound_trunk(setup)
    trunks = (await sip.list_inbound_trunk(api.ListSIPInboundTrunkRequest())).items
    # Match by name; otherwise adopt a trunk that already owns one of our numbers (LiveKit
    # would route the number ambiguously if two trunks claimed it).
    existing = next((t for t in trunks if t.name == setup.trunk_name), None) or next(
        (t for t in trunks if set(t.numbers) & set(setup.numbers)), None
    )
    if existing is None:
        if dry_run:
            return "(new trunk)", "would create"
        created = await sip.create_inbound_trunk(api.CreateSIPInboundTrunkRequest(trunk=desired))
        return created.sip_trunk_id, "created"
    if not _trunk_differs(existing, desired):
        return existing.sip_trunk_id, "unchanged"
    if dry_run:
        return existing.sip_trunk_id, "would update"
    updated = await sip.update_inbound_trunk(existing.sip_trunk_id, _merge_trunk(existing, desired))
    return updated.sip_trunk_id, "updated"


async def ensure_dispatch_rule(
    sip: SipApi, setup: SipSetup, trunk_id: str, *, dry_run: bool = False
) -> tuple[str, Action]:
    desired = build_dispatch_rule(setup, trunk_id)
    rules = (await sip.list_dispatch_rule(api.ListSIPDispatchRuleRequest())).items
    existing = next((r for r in rules if r.name == setup.rule_name), None)
    others = [r for r in rules if r is not existing and trunk_id in r.trunk_ids]
    for rule in others:
        print(
            f"warning: dispatch rule {rule.sip_dispatch_rule_id} ({rule.name or 'unnamed'}) "
            f"also targets trunk {trunk_id}; LiveKit may reject or misroute calls",
            file=sys.stderr,
        )
    if existing is None:
        if dry_run:
            return "(new rule)", "would create"
        created = await sip.create_dispatch_rule(
            api.CreateSIPDispatchRuleRequest(dispatch_rule=desired)
        )
        return created.sip_dispatch_rule_id, "created"
    if not _rule_differs(existing, desired):
        return existing.sip_dispatch_rule_id, "unchanged"
    if dry_run:
        return existing.sip_dispatch_rule_id, "would update"
    updated = await sip.update_dispatch_rule(
        existing.sip_dispatch_rule_id, _merge_rule(existing, desired)
    )
    return updated.sip_dispatch_rule_id, "updated"


async def apply(
    setup: SipSetup,
    *,
    dry_run: bool = False,
    client_factory: Callable[[], api.LiveKitAPI] = api.LiveKitAPI,
) -> Result:
    async with client_factory() as lkapi:
        trunk_id, trunk_action = await ensure_inbound_trunk(lkapi.sip, setup, dry_run=dry_run)
        rule_id, rule_action = await ensure_dispatch_rule(
            lkapi.sip, setup, trunk_id, dry_run=dry_run
        )
    return Result(trunk_id, trunk_action, rule_id, rule_action)


# -- CLI --------------------------------------------------------------------------------------


def _e164(value: str) -> str:
    digits = value[1:] if value.startswith("+") else ""
    if not (digits.isdigit() and 8 <= len(digits) <= 15):
        raise argparse.ArgumentTypeError(f"{value!r} is not an E.164 number like +15551234567")
    return value


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="setup_sip.py",
        description="Create/update the LiveKit inbound trunk + dispatch rule for the receptionist.",
    )
    parser.add_argument(
        "--number",
        dest="numbers",
        action="append",
        type=_e164,
        required=True,
        help="phone number the trunk accepts calls for, E.164 (repeatable)",
    )
    parser.add_argument("--trunk-name", default=DEFAULT_TRUNK_NAME)
    parser.add_argument("--rule-name", default=DEFAULT_RULE_NAME)
    parser.add_argument(
        "--allowed-address",
        dest="allowed_addresses",
        action="append",
        default=[],
        metavar="CIDR",
        help="only accept SIP from this IP/CIDR, e.g. your carrier's signalling range (repeatable)",
    )
    parser.add_argument(
        "--allowed-caller",
        dest="allowed_callers",
        action="append",
        default=[],
        type=_e164,
        metavar="E164",
        help="only accept calls from this caller ID, e.g. your own phone while testing (repeatable)",
    )
    parser.add_argument(
        "--no-krisp", dest="krisp_enabled", action="store_false", help="disable noise cancellation"
    )
    parser.add_argument("--mode", choices=_MODES, default=DEFAULT_DISPATCH_METADATA["mode"])
    parser.add_argument(
        "--language", choices=SUPPORTED_LANGUAGES, default=DEFAULT_DISPATCH_METADATA["language"]
    )
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true", help="read current state, change nothing")
    group.add_argument(
        "--print-json",
        action="store_true",
        help="print the create requests as `lk sip … create` JSON and exit (no network)",
    )
    return parser.parse_args(argv)


def setup_from_args(args: argparse.Namespace) -> SipSetup:
    return SipSetup(
        numbers=tuple(args.numbers),
        trunk_name=args.trunk_name,
        rule_name=args.rule_name,
        allowed_addresses=tuple(args.allowed_addresses),
        allowed_callers=tuple(args.allowed_callers),
        krisp_enabled=args.krisp_enabled,
        agent_name=os.getenv("AGENT_NAME") or DEFAULT_AGENT_NAME,
        dispatch_metadata={
            "mode": args.mode,
            "language": args.language,
        },
    )


def _to_json(message: object) -> str:
    return json_format.MessageToJson(message, preserving_proto_field_name=True, indent=2)  # type: ignore[arg-type]


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    if not args.print_json:
        try:
            from dotenv import load_dotenv
        except ImportError:  # pragma: no cover - python-dotenv is a project dependency
            pass
        else:
            load_dotenv(".env")  # the current directory only, never the script's own repo
    setup = setup_from_args(args)

    if args.print_json:
        trunk = api.CreateSIPInboundTrunkRequest(trunk=build_inbound_trunk(setup))
        rule = api.CreateSIPDispatchRuleRequest(
            dispatch_rule=build_dispatch_rule(setup, "ST_REPLACE_WITH_TRUNK_ID")
        )
        print("# lk sip inbound create inbound-trunk.json")
        print(_to_json(trunk))
        print("# lk sip dispatch create dispatch-rule.json")
        print(_to_json(rule))
        return 0

    missing = [
        name
        for name in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET")
        if not os.getenv(name)
    ]
    if missing:
        print(f"error: set {', '.join(missing)} (or run with --print-json)", file=sys.stderr)
        return 2

    try:
        result = asyncio.run(apply(setup, dry_run=args.dry_run))
    except api.TwirpError as exc:
        print(f"error: LiveKit rejected the request: {exc}", file=sys.stderr)
        return 1

    print(f"inbound trunk  {result.trunk_id:<24} {result.trunk_action}")
    print(f"dispatch rule  {result.rule_id:<24} {result.rule_action}")
    if not args.dry_run:
        print(
            "\nNext: point your carrier at your project's SIP URI (Settings -> Project in LiveKit "
            "Cloud,\ne.g. sip:<project-id>.sip.livekit.cloud) and call "
            f"{', '.join(setup.numbers)}."
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
