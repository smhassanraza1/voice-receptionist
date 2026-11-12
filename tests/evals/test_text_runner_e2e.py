"""End-to-end, offline: the real agent classes and services, driven by scripted models.

A scripted LLM plays the receptionist and a scripted simulator plays the caller, so no API
keys are needed. Everything else is real: the MCP server, billing agent and web server run
as subprocesses with JWT auth, the agent's LiveKit MCP toolset calls them over HTTP, and the
run is graded on the clinic database's final state. A renamed tool, a broken auth path or a
regression in identity capture fails this test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fakes import Reply, ScriptedLLM, last_item, last_user_text
from livekit.agents.llm import ChatContext

from receptionist.agent.settings import AgentSettings
from receptionist.evals.scenarios import load_scenarios
from receptionist.evals.services import EvalServices
from receptionist.evals.text_runner import run_text_scenario

SCENARIOS = Path(__file__).parents[2] / "evals" / "scenarios"


offered: dict[str, str] = {}


def receptionist_script(ctx: ChatContext, tools) -> Reply:
    item = last_item(ctx)
    if item.type == "function_call_output":
        if item.is_error or not item.output.startswith("{"):  # e.g. end_call's instruction
            return Reply("Goodbye!")
        output = json.loads(item.output)
        if item.name == "find_available_slots":
            slot = output["slots"][0]
            offered["slot_id"] = slot["slot_id"]
            return Reply(
                f"I have {slot['weekday']} {slot['local_date']} at {slot['local_time']}. Does that work?"
            )
        if item.name == "book_appointment":
            return Reply(f"You're booked. Your code is {output['confirmation_code']}.")
        return Reply("Done.")

    said = last_user_text(ctx).lower()
    if not said:
        return Reply("Thank you for calling Lakeside Family Clinic, this is Maya. How can I help?")
    if "dermatolog" in said:
        return Reply(
            tool_calls=[
                ("find_available_slots", {"specialty": "dermatology", "date_from": "2026-11-09"})
            ]
        )
    if "daniel brooks" in said:
        args = {
            "slot_id": offered["slot_id"],
            "patient_name": "Daniel Brooks",
            "date_of_birth": "1990-06-02",
            "phone": "555-010-2020",
            "reason": "mole check",
        }
        return Reply(tool_calls=[("book_appointment", args)])
    if "bye" in said:
        return Reply(tool_calls=[("end_call", {})])
    return Reply("Anything else?")


def caller_script(ctx: ChatContext, tools) -> Reply:
    transcript = last_user_text(ctx)
    if "booked" in transcript:
        return Reply("Great, thanks, bye!")
    if "Does that work?" in transcript:
        return Reply("Yes. Daniel Brooks, June 2nd 1990, 555-010-2020.")
    if "AGENT:" in transcript and "Anything else" in transcript:
        return Reply("<END_CALL>")
    return Reply("I need a dermatology appointment next week.")


@pytest.fixture(scope="module")
def services(tmp_path_factory) -> EvalServices:
    with EvalServices(tmp_path_factory.mktemp("services")) as svc:
        yield svc


async def test_booking_scenario_end_to_end(services: EvalServices, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    scenario = load_scenarios(SCENARIOS, ids=["new-patient-dermatology"])[0]
    result = await run_text_scenario(
        scenario,
        "pipeline",
        services,
        base_settings=AgentSettings(_env_file=None),
        simulator_llm=ScriptedLLM(caller_script),
        judge_llm=None,
        agent_llm=ScriptedLLM(receptionist_script),
    )

    assert result.error is None, result.error
    calls = [e.name for e in result.events if e.role == "tool_call"]
    assert calls[:2] == ["find_available_slots", "book_appointment"]
    assert result.tools.recall == 1.0 and result.tools.arg_accuracy == 1.0
    assert all(o.passed for o in result.outcomes), [o.detail for o in result.outcomes]
    assert result.passed
    # identity capture came from the server's verified booking result, not from the LLM
    booked = next(
        e for e in result.events if e.role == "tool_result" and e.name == "book_appointment"
    )
    assert json.loads(booked.text)["patient_id"].startswith("P-")


async def test_simulator_sees_no_tool_traffic_and_the_run_is_priced(
    services: EvalServices, monkeypatch
):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    scenario = load_scenarios(SCENARIOS, ids=["new-patient-dermatology"])[0]
    simulator = ScriptedLLM(caller_script, model="gpt-4.1-mini", usage=(1000, 20))
    result = await run_text_scenario(
        scenario,
        "pipeline",
        services,
        base_settings=AgentSettings(_env_file=None),
        simulator_llm=simulator,
        judge_llm=None,
        agent_llm=ScriptedLLM(receptionist_script, model="gpt-4.1-mini", usage=(2000, 50)),
    )

    assert result.error is None, result.error
    prompts = [last_user_text(ctx) for ctx in simulator.calls]
    assert any("AGENT:" in p for p in prompts)
    assert not any("[tool" in p for p in prompts)
    agent_calls = sum(1 for e in result.events if e.role == "agent") + sum(
        1 for e in result.events if e.role == "tool_call"
    )
    assert result.usage["cost"]["by_model"]["openai/gpt-4.1-mini"] > 0
    assert result.usage["cost"]["total_usd"] <= agent_calls * (2000 * 0.4 + 50 * 1.6) / 1e6
    overhead = result.usage["eval_overhead"]["cost"]
    assert overhead["total_usd"] == pytest.approx(
        len(simulator.calls) * (1000 * 0.4 + 20 * 1.6) / 1e6, abs=1e-5
    )


async def test_a_judge_failure_is_recorded_not_raised(services: EvalServices, monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    scenario = load_scenarios(SCENARIOS, ids=["new-patient-dermatology"])[0]

    def broken_judge(ctx: ChatContext, tools) -> Reply:
        raise RuntimeError("judge unavailable")

    result = await run_text_scenario(
        scenario,
        "pipeline",
        services,
        base_settings=AgentSettings(_env_file=None),
        simulator_llm=ScriptedLLM(caller_script),
        judge_llm=ScriptedLLM(broken_judge),
        agent_llm=ScriptedLLM(receptionist_script),
    )
    assert result.error is None
    assert result.verdict is None
    assert "judge unavailable" in (result.judge_error or "")
    assert not result.passed
    assert result.to_dict()["judge_error"] == result.judge_error


async def test_unauthenticated_agent_gets_no_tools(services: EvalServices, monkeypatch):
    from receptionist.agent.state import CallState
    from receptionist.agent.tools import build_mcp_toolset

    settings = AgentSettings(
        _env_file=None, mcp_server_url=services.mcp_url, mcp_auth_token="not-a-jwt"
    )
    state = CallState()
    toolset = build_mcp_toolset(settings, state)
    with pytest.raises(Exception):  # noqa: B017 - the MCP client surfaces the 401 as a transport error
        await toolset.setup()
