"""telephony/: the JSON templates parse as LiveKit requests, and setup_sip.py is idempotent."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType

import pytest
from google.protobuf import json_format
from livekit import api

TELEPHONY = Path(__file__).resolve().parents[2] / "telephony"


@pytest.fixture(scope="module")
def setup_sip() -> ModuleType:
    spec = importlib.util.spec_from_file_location("setup_sip", TELEPHONY / "setup_sip.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["setup_sip"] = module  # dataclasses resolve annotations through sys.modules
    spec.loader.exec_module(module)  # importing must not touch the network or the environment
    return module


def parse(path: Path, message: object) -> object:
    # ignore_unknown_fields=False: a misspelled field fails here instead of being dropped by lk.
    return json_format.Parse(path.read_text(), message, ignore_unknown_fields=False)


def test_inbound_trunk_template_matches_the_script(setup_sip: ModuleType) -> None:
    request = parse(TELEPHONY / "inbound-trunk.json", api.CreateSIPInboundTrunkRequest())
    assert isinstance(request, api.CreateSIPInboundTrunkRequest)
    expected = setup_sip.build_inbound_trunk(setup_sip.SipSetup(numbers=("+1XXXXXXXXXX",)))
    assert request.trunk == expected


def test_dispatch_rule_template_matches_contract(setup_sip: ModuleType) -> None:
    request = parse(TELEPHONY / "dispatch-rule.json", api.CreateSIPDispatchRuleRequest())
    assert isinstance(request, api.CreateSIPDispatchRuleRequest)
    rule = request.dispatch_rule
    assert rule.rule.WhichOneof("rule") == "dispatch_rule_individual"
    assert rule.rule.dispatch_rule_individual.room_prefix == "call-"
    (agent,) = rule.room_config.agents
    assert agent.agent_name == "receptionist"
    assert json.loads(agent.metadata) == {
        "mode": "realtime",
        "language": "auto",
    }
    expected = setup_sip.build_dispatch_rule(
        setup_sip.SipSetup(numbers=("+1XXXXXXXXXX",)), "ST_REPLACE_WITH_TRUNK_ID"
    )
    assert rule == expected


class FakeSip:
    """In-memory stand-in for `LiveKitAPI().sip` recording every write."""

    def __init__(self) -> None:
        self.trunks: dict[str, api.SIPInboundTrunkInfo] = {}
        self.rules: dict[str, api.SIPDispatchRuleInfo] = {}
        self.writes: list[str] = []

    async def list_inbound_trunk(
        self, list: api.ListSIPInboundTrunkRequest
    ) -> api.ListSIPInboundTrunkResponse:
        return api.ListSIPInboundTrunkResponse(items=self.trunks.values())

    async def create_inbound_trunk(
        self, create: api.CreateSIPInboundTrunkRequest
    ) -> api.SIPInboundTrunkInfo:
        info = api.SIPInboundTrunkInfo()
        info.CopyFrom(create.trunk)
        info.sip_trunk_id = f"ST_{len(self.trunks) + 1:04d}"
        self.trunks[info.sip_trunk_id] = info
        self.writes.append("create_trunk")
        return info

    async def update_inbound_trunk(
        self, trunk_id: str, trunk: api.SIPInboundTrunkInfo
    ) -> api.SIPInboundTrunkInfo:
        info = api.SIPInboundTrunkInfo()
        info.CopyFrom(trunk)
        info.sip_trunk_id = trunk_id
        self.trunks[trunk_id] = info
        self.writes.append("update_trunk")
        return info

    async def list_dispatch_rule(
        self, list: api.ListSIPDispatchRuleRequest
    ) -> api.ListSIPDispatchRuleResponse:
        return api.ListSIPDispatchRuleResponse(items=self.rules.values())

    async def create_dispatch_rule(
        self, create: api.CreateSIPDispatchRuleRequest
    ) -> api.SIPDispatchRuleInfo:
        info = api.SIPDispatchRuleInfo()
        info.CopyFrom(create.dispatch_rule)
        info.sip_dispatch_rule_id = f"SDR_{len(self.rules) + 1:04d}"
        self.rules[info.sip_dispatch_rule_id] = info
        self.writes.append("create_rule")
        return info

    async def update_dispatch_rule(
        self, rule_id: str, rule: api.SIPDispatchRuleInfo
    ) -> api.SIPDispatchRuleInfo:
        info = api.SIPDispatchRuleInfo()
        info.CopyFrom(rule)
        info.sip_dispatch_rule_id = rule_id
        self.rules[rule_id] = info
        self.writes.append("update_rule")
        return info


class FakeLiveKitAPI:
    def __init__(self, sip: FakeSip) -> None:
        self.sip = sip

    async def __aenter__(self) -> FakeLiveKitAPI:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


async def test_apply_creates_then_is_idempotent(setup_sip: ModuleType) -> None:
    sip = FakeSip()
    setup = setup_sip.SipSetup(numbers=("+15551230000",))

    first = await setup_sip.apply(setup, client_factory=lambda: FakeLiveKitAPI(sip))
    assert (first.trunk_action, first.rule_action) == ("created", "created")
    assert sip.rules[first.rule_id].trunk_ids == [first.trunk_id]

    second = await setup_sip.apply(setup, client_factory=lambda: FakeLiveKitAPI(sip))
    assert (second.trunk_action, second.rule_action) == ("unchanged", "unchanged")
    assert (second.trunk_id, second.rule_id) == (first.trunk_id, first.rule_id)
    assert sip.writes == ["create_trunk", "create_rule"]


async def test_apply_updates_in_place_and_keeps_manual_fields(setup_sip: ModuleType) -> None:
    sip = FakeSip()
    factory = lambda: FakeLiveKitAPI(sip)  # noqa: E731
    first = await setup_sip.apply(
        setup_sip.SipSetup(numbers=("+15551230000",)), client_factory=factory
    )
    sip.trunks[first.trunk_id].auth_realm = "set-by-hand"

    changed = setup_sip.SipSetup(
        numbers=("+15551230000", "+15551230001"),
        allowed_callers=("+15557654321",),
        dispatch_metadata={"mode": "pipeline", "language": "es"},
    )
    result = await setup_sip.apply(changed, client_factory=factory)
    assert (result.trunk_action, result.rule_action) == ("updated", "updated")
    assert len(sip.trunks) == 1 and len(sip.rules) == 1

    trunk = sip.trunks[first.trunk_id]
    assert list(trunk.numbers) == ["+15551230000", "+15551230001"]
    assert list(trunk.allowed_numbers) == ["+15557654321"]
    assert trunk.auth_realm == "set-by-hand"
    metadata = json.loads(sip.rules[first.rule_id].room_config.agents[0].metadata)
    assert metadata["mode"] == "pipeline" and metadata["language"] == "es"


async def test_dry_run_writes_nothing(setup_sip: ModuleType) -> None:
    sip = FakeSip()
    result = await setup_sip.apply(
        setup_sip.SipSetup(numbers=("+15551230000",)),
        dry_run=True,
        client_factory=lambda: FakeLiveKitAPI(sip),
    )
    assert (result.trunk_action, result.rule_action) == ("would create", "would create")
    assert sip.writes == []


def test_cli_rejects_non_e164_numbers(setup_sip: ModuleType) -> None:
    with pytest.raises(SystemExit):
        setup_sip.parse_args(["--number", "555-0100"])


def test_print_json_works_offline(
    setup_sip: ModuleType, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        monkeypatch.delenv(name, raising=False)
    assert setup_sip.main(["--number", "+15551230000", "--language", "ja", "--print-json"]) == 0
    out = capsys.readouterr().out
    assert '"room_prefix": "call-"' in out
    assert '\\"language\\": \\"ja\\"' in out


def test_dispatch_rule_targets_agent_name(
    setup_sip: ModuleType, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGENT_NAME", "receptionist-staging")
    assert setup_sip.main(["--number", "+15551230000", "--print-json"]) == 0
    assert '"agent_name": "receptionist-staging"' in capsys.readouterr().out


def test_cli_offers_the_agents_modes_and_languages(setup_sip: ModuleType) -> None:
    from receptionist.agent.settings import SUPPORTED_LANGUAGES

    for language in SUPPORTED_LANGUAGES:
        assert setup_sip.parse_args(["--number", "+15551230000", "--language", language])
    with pytest.raises(SystemExit):
        setup_sip.parse_args(["--number", "+15551230000", "--mode", "duplex"])


def test_missing_credentials_exit_code(
    setup_sip: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)  # no .env here
    for name in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        monkeypatch.delenv(name, raising=False)
    assert setup_sip.main(["--number", "+15551230000"]) == 2
