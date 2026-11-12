from receptionist.evals.outcomes import evaluate
from receptionist.evals.scenarios import Expected, ExpectedCall, Outcome
from receptionist.evals.scoring import (
    Event,
    args_match,
    format_conversation,
    format_transcript,
    handoff_ok,
    score_tool_calls,
)


def call(name: str, **args) -> Event:
    return Event("tool_call", name=name, args=args)


def result(name: str, *, error: bool = False, text: str = "{}") -> Event:
    return Event("tool_result", text, name=name, is_error=error)


def test_args_match_normalizes_case_whitespace_and_phone_format():
    assert args_match({"patient_name": "Daniel Brooks"}, {"patient_name": "  daniel   brooks "})
    assert args_match({"phone": "+15550102020"}, {"phone": "(555) 010-2020"})
    assert not args_match({"date_of_birth": "1990-06-02"}, {"date_of_birth": "1990-02-06"})
    assert not args_match({"specialty": "dermatology"}, {})


def test_tool_score_recall_args_and_write_precision():
    expected = Expected(
        tool_calls=[
            ExpectedCall(name="find_available_slots", args={"specialty": "dermatology"}),
            ExpectedCall(name="book_appointment", args={"date_of_birth": "1990-06-02"}),
        ],
        forbidden_tools=["cancel_appointment"],
    )
    events = [
        call("search_knowledge_base", query="hours"),  # extra reads are fine
        call("find_available_slots", specialty="Dermatology"),
        call("book_appointment", date_of_birth="1990-02-06"),  # wrong DOB
        call("cancel_appointment", confirmation_code="LKC-2B6D"),
    ]
    score = score_tool_calls(expected, events)
    assert score.recall == 0.5
    assert score.name_matched == 2 and score.arg_accuracy == 0.5
    assert score.forbidden_violations == ["cancel_appointment"]
    # a booking with the wrong date of birth is not a correct write, just an unexpected one
    assert score.unexpected_writes == ["book_appointment", "cancel_appointment"]
    assert score.write_precision == 0.0
    assert not score.perfect


def test_a_failed_write_is_not_a_correct_write():
    expected = Expected(tool_calls=[ExpectedCall(name="book_appointment", args={"slot_id": "S1"})])
    events = [call("book_appointment", slot_id="S1"), result("book_appointment", error=True)]
    score = score_tool_calls(expected, events)
    assert score.recall == 1.0  # the right call was made
    assert score.write_precision == 0.0  # but it did not go through
    assert score.unexpected_writes == []


def test_a_successful_retry_counts_and_the_failed_attempt_does_not():
    expected = Expected(tool_calls=[ExpectedCall(name="book_appointment", args={"slot_id": "S1"})])
    events = [
        call("book_appointment", slot_id="S1"),
        result("book_appointment", error=True),
        call("book_appointment", slot_id="S1"),
        result("book_appointment"),
    ]
    score = score_tool_calls(expected, events)
    assert score.correct_writes == 1 and score.write_calls == 2
    assert score.write_precision == 0.5
    assert score.unexpected_writes == []


def test_duplicate_writes_count_against_precision():
    expected = Expected(tool_calls=[ExpectedCall(name="cancel_appointment")])
    events = [call("cancel_appointment"), call("cancel_appointment")]
    score = score_tool_calls(expected, events)
    assert score.write_precision == 0.5
    assert score.unexpected_writes == ["cancel_appointment"]


def test_results_pair_with_calls_by_call_id():
    expected = Expected(tool_calls=[ExpectedCall(name="book_appointment", args={"slot_id": "S2"})])
    events = [
        Event("tool_call", name="book_appointment", args={"slot_id": "S1"}, call_id="a"),
        Event("tool_call", name="book_appointment", args={"slot_id": "S2"}, call_id="b"),
        # results arrive out of order: the S2 booking succeeded, the S1 one failed
        Event("tool_result", "{}", name="book_appointment", call_id="b"),
        Event("tool_result", "taken", name="book_appointment", is_error=True, call_id="a"),
    ]
    score = score_tool_calls(expected, events)
    assert score.correct_writes == 1 and score.write_precision == 0.5
    assert score.unexpected_writes == []


def test_ratios_are_none_when_not_applicable():
    score = score_tool_calls(Expected(), [call("search_knowledge_base", query="hours")])
    assert score.recall is None and score.arg_accuracy is None and score.write_precision is None
    assert score.to_dict()["recall"] is None
    # expected but never called: recall is 0, argument accuracy has nothing to judge
    missed = score_tool_calls(Expected(tool_calls=[ExpectedCall(name="find_available_slots")]), [])
    assert missed.recall == 0.0 and missed.arg_accuracy is None


def test_each_expected_call_consumes_one_actual_call():
    expected = Expected(tool_calls=[ExpectedCall(name="lookup_appointments")] * 2)
    assert score_tool_calls(expected, [call("lookup_appointments")]).recall == 0.5


def test_handoff_rules():
    billing = [call("transfer_to_billing")]
    assert handoff_ok(Expected(handoff="billing"), billing)
    assert handoff_ok(Expected(), billing)  # unrequested billing handoff isn't an escalation
    assert not handoff_ok(Expected(), [call("transfer_to_human", reason="x", summary="y")])
    assert handoff_ok(Expected(handoff="human"), [Event("handoff", name="human")])


def test_the_caller_sees_only_the_conversation():
    events = [
        Event("caller", "Any dermatology next week?"),
        call("find_available_slots", specialty="dermatology"),
        result("find_available_slots", text='{"slots": ["S1"]}'),
        Event("agent", "I have Monday at 9."),
    ]
    assert format_conversation(events) == (
        "CALLER: Any dermatology next week?\nAGENT: I have Monday at 9."
    )
    assert "[tool result]" in format_transcript(events)


def test_the_judge_sees_whole_tool_results():
    kb = "x" * 2500  # a typical knowledge-base result
    transcript = format_transcript([result("search_knowledge_base", text=kb + "FACT")])
    assert kb + "FACT" in transcript and "truncated" not in transcript


def test_transcript_stays_within_budget_on_long_calls():
    from receptionist.evals.scoring import TRANSCRIPT_BUDGET_CHARS

    events = [result("find_available_slots", text="y" * 5000) for _ in range(40)]
    transcript = format_transcript(events)
    assert len(transcript) <= TRANSCRIPT_BUDGET_CHARS * 1.1
    assert "[...truncated]" in transcript


def appt(code: str, start: str, status: str = "booked", specialty: str = "dermatology") -> dict:
    return {
        "confirmation_code": code,
        "start": start,
        "status": status,
        "specialty": specialty,
        "local_date": start[:10],
    }


def test_outcome_booked_requires_exactly_one_new_matching_booking():
    before = [appt("LKC-AAAA", "2026-11-05T10:00")]
    after = before + [appt("LKC-BBBB", "2026-11-09T14:00")]
    o = Outcome(kind="booked", date_of_birth="1990-06-02", specialty="dermatology")
    assert evaluate(o, before, after).passed
    assert not evaluate(o, before, after + [appt("LKC-CCCC", "2026-11-10T09:00")]).passed
    assert not evaluate(o.model_copy(update={"specialty": "cardiology"}), before, after).passed


def test_outcome_cancel_reschedule_unchanged():
    before = [appt("LKC-2B6D", "2026-11-05T11:00")]
    cancelled = [appt("LKC-2B6D", "2026-11-05T11:00", status="cancelled")]
    moved = [appt("LKC-2B6D", "2026-11-12T09:00")]
    base = {"date_of_birth": "1948-01-30", "confirmation_code": "LKC-2B6D"}
    assert evaluate(Outcome(kind="cancelled", **base), before, cancelled).passed
    assert evaluate(Outcome(kind="rescheduled", **base), before, moved).passed
    assert not evaluate(Outcome(kind="rescheduled", **base), before, before).passed
    assert evaluate(Outcome(kind="unchanged", **base), before, before).passed
    assert not evaluate(Outcome(kind="unchanged", **base), before, cancelled).passed
    assert evaluate(Outcome(kind="no_booking", date_of_birth="1961-04-04"), [], []).passed
