import random

from fakes import Reply, ScriptedLLM, last_user_text

from receptionist.evals.judge import judge_call
from receptionist.evals.scenarios import Persona, Scenario
from receptionist.evals.simulator import END_TOKEN, CallerSimulator, add_asr_noise


def scenario(**persona) -> Scenario:
    return Scenario(
        id="s",
        title="t",
        persona=Persona(name="Ana", traits="calm", **persona),
        goal="Ask about hours.",
        expectations="Answers from the KB.",
    )


async def test_judge_recomputes_the_pass_gate():
    # a lenient judge that says passed=True despite a failing faithfulness score
    llm = ScriptedLLM(
        lambda ctx, tools: Reply(
            tool_calls=[
                (
                    "submit_verdict",
                    {
                        "task_success": 5,
                        "policy_adherence": 5,
                        "faithfulness": 2,
                        "voice_style": 9,
                        "language": 5,
                        "passed": True,
                        "rationale": "Invented a price in turn 3.",
                    },
                )
            ]
        )
    )
    verdict = await judge_call(
        llm, goal="g", expectations="e", transcript="CALLER: hi\nAGENT: hello"
    )
    assert verdict.passed is False
    assert verdict.voice_style == 5  # clamped to the 1-5 scale
    assert "turn 3" in verdict.rationale
    # the transcript and rubric reach the judge
    assert "CALLER: hi" in llm.calls[0].items[-1].text_content


async def test_simulator_strips_role_prefix_and_detects_hang_up():
    replies = iter(["CALLER: What time do you open?", f"Thanks, bye! {END_TOKEN}"])
    llm = ScriptedLLM(lambda ctx, tools: Reply(text=next(replies)))
    sim = CallerSimulator(llm, scenario())
    assert await sim.next_turn("") == "What time do you open?"
    assert await sim.next_turn("AGENT: We open at 8.") is None
    system = llm.calls[0].items[0].text_content
    assert "Ana" in system and "English" in system


async def test_simulator_speaks_the_persona_language():
    llm = ScriptedLLM(lambda ctx, tools: Reply(text="Hola"))
    await CallerSimulator(llm, scenario(language="es")).next_turn("")
    assert "Spanish" in llm.calls[0].items[0].text_content
    assert "Conversation so far" in last_user_text(llm.calls[0])


def test_asr_noise_is_deterministic_and_bounded():
    text = "I would like to request a prescription refill for my blood pressure medication please"
    a = add_asr_noise(text, 0.3, random.Random(1))
    b = add_asr_noise(text, 0.3, random.Random(1))
    assert a == b and a != text
    assert add_asr_noise(text, 0.0, random.Random(1)) == text
    assert len(a.split()) >= len(text.split()) // 2


async def test_reasoning_models_get_no_temperature():
    from receptionist.evals.llm_utils import complete

    for name, sent in (("gpt-4.1-mini", {"temperature": 0.8}), ("gpt-5-mini", {}), ("o4-mini", {})):
        llm = ScriptedLLM(lambda ctx, tools: Reply(text="ok"), model=name)
        await complete(llm, system="s", user="u", temperature=0.8)
        assert llm.extra_kwargs == [sent], name


async def test_metered_counts_each_model_once_and_detaches():
    from receptionist.evals.llm_utils import metered

    sim = ScriptedLLM(lambda ctx, tools: Reply(text="Hi"), model="gpt-4.1-mini", usage=(100, 10))
    with metered(sim, sim, None) as usage:
        await CallerSimulator(sim, scenario()).next_turn("")
    await CallerSimulator(sim, scenario()).next_turn("")  # outside the block: not counted
    [summary] = usage.flatten()
    assert (summary.model, summary.input_tokens, summary.output_tokens) == ("gpt-4.1-mini", 100, 10)
