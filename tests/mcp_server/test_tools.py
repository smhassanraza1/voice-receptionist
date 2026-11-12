"""The MCP surface, exercised in-process through `mcp.Client(server)`."""

import json
import logging
import sqlite3

import pytest
from mcp import Client

from receptionist.common import fixtures
from receptionist.mcp_server.db import IDENTITY_FAILED_MESSAGE, ClinicStore
from receptionist.mcp_server.security import INTERNAL_ERROR_MESSAGE, LOCKED_OUT_MESSAGE

from .conftest import connect, error_text, make_settings, payload

CONTRACT_TOOLS = {
    "search_knowledge_base",
    "list_providers",
    "find_available_slots",
    "book_appointment",
    "lookup_appointments",
    "reschedule_appointment",
    "cancel_appointment",
}
NEW_PATIENT = {
    "patient_name": "Nora Patel",
    "date_of_birth": "1990-06-15",
    "phone": "555-010-9999",
    "reason": "sore throat",
}


def appointment_count(settings) -> int:
    with sqlite3.connect(settings.db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM appointments").fetchone()[0]


async def first_slot(client: Client, **filters) -> dict:
    result = await client.call_tool("find_available_slots", filters)
    return payload(result)["slots"][0]


async def test_tools_match_the_contract(settings) -> None:
    async with connect(settings) as client:
        tools = {t.name: t for t in (await client.list_tools()).tools}
        assert set(tools) == CONTRACT_TOOLS
        assert all(t.output_schema for t in tools.values())
        for name in ("search_knowledge_base", "list_providers", "find_available_slots"):
            assert tools[name].annotations.read_only_hint is True
        assert tools["book_appointment"].annotations.destructive_hint is False
        assert tools["cancel_appointment"].annotations.destructive_hint is True
        assert tools["cancel_appointment"].annotations.idempotent_hint is True
        assert "$50" in tools["cancel_appointment"].description

        search = tools["search_knowledge_base"].input_schema["properties"]
        assert (search["query"]["minLength"], search["query"]["maxLength"]) == (2, 300)
        top_k = search["top_k"]
        assert (top_k["minimum"], top_k["maximum"], top_k["default"]) == (1, 8, 4)
        specialty = tools["list_providers"].input_schema["properties"]["specialty"]
        assert specialty["anyOf"][0]["enum"] == fixtures.SPECIALTIES


@pytest.mark.parametrize(
    ("tool", "arguments"),
    [
        ("search_knowledge_base", {"query": "x"}),
        ("search_knowledge_base", {"query": "parking", "top_k": 9}),
        ("list_providers", {"specialty": "neurology"}),
        ("list_providers", {"language": "Spanish"}),
        ("find_available_slots", {"date_from": "next tuesday"}),
        ("find_available_slots", {"limit": 50}),
        ("book_appointment", {**NEW_PATIENT, "slot_id": "'; DROP TABLE slots; --"}),
        ("book_appointment", {**NEW_PATIENT, "slot_id": "SL-PR01-20261103-1000", "phone": "12"}),
        (
            "book_appointment",
            {**NEW_PATIENT, "slot_id": "SL-PR01-20261103-1000", "reason": "x" * 201},
        ),
        (
            "book_appointment",
            {**NEW_PATIENT, "slot_id": "SL-PR01-20261103-1000", "date_of_birth": "2027-01-01"},
        ),
        ("lookup_appointments", {"date_of_birth": "1985-03-14", "confirmation_code": "7Q2M"}),
    ],
)
async def test_invalid_arguments_are_rejected_before_the_tool_runs(
    settings, tool: str, arguments: dict
) -> None:
    async with connect(settings) as client:
        result = await client.call_tool(tool, arguments)
    assert "validation error" in error_text(result)
    assert appointment_count(settings) == len(fixtures.SEED_APPOINTMENTS)  # body never ran


async def test_book_lookup_reschedule_and_cancel(settings) -> None:
    async with connect(settings) as client:
        slot = await first_slot(client, specialty="family_medicine", time_of_day="afternoon")
        booking = payload(
            await client.call_tool("book_appointment", {**NEW_PATIENT, "slot_id": slot["slot_id"]})
        )
        assert booking["identity_verified"] and booking["local_time"] == slot["local_time"]
        code = booking["confirmation_code"]

        lookup = payload(
            await client.call_tool(
                "lookup_appointments", {"date_of_birth": "1990-06-15", "phone": "+1 555 010 9999"}
            )
        )
        assert lookup["patient_name"] == "Nora Patel" and lookup["identity_verified"]
        assert [a["confirmation_code"] for a in lookup["appointments"]] == [code]

        later = await first_slot(client, specialty="family_medicine", date_from="2026-11-10")
        moved = payload(
            await client.call_tool(
                "reschedule_appointment",
                {
                    "confirmation_code": code,
                    "date_of_birth": "1990-06-15",
                    "new_slot_id": later["slot_id"],
                },
            )
        )
        assert (moved["old_start"], moved["new_start"]) == (slot["start"], later["start"])

        cancelled = payload(
            await client.call_tool(
                "cancel_appointment",
                {
                    "confirmation_code": code.lower(),
                    "date_of_birth": "1990-06-15",
                    "reason": "better",
                },
            )
        )
        assert cancelled == {
            "confirmation_code": code,
            "status": "cancelled",
            "start": later["start"],
            "late_cancellation": False,
            "fee_usd": 0.0,
        }


async def test_business_errors_are_short_and_speakable(settings) -> None:
    async with connect(settings) as client:
        taken = await client.call_tool(
            "book_appointment", {**NEW_PATIENT, "slot_id": "SL-PR02-20261103-1430"}
        )
        message = error_text(taken)
        assert "no longer available" in message
        assert "Traceback" not in message and "sqlite" not in message.lower()

        past = await client.call_tool("find_available_slots", {"date_from": "2026-10-01"})
        assert "Today is Monday, November 2, 2026" in error_text(past)


async def test_identity_failures_share_one_message_and_lock_the_client_out(tmp_path) -> None:
    async with connect(make_settings(tmp_path, lockout_max_failures=3)) as client:
        wrong_dob = {"confirmation_code": "LKC-7Q2M", "date_of_birth": "1985-03-15"}
        unknown_code = {"confirmation_code": "LKC-AAAA", "date_of_birth": "1985-03-14"}
        first = await client.call_tool("lookup_appointments", wrong_dob)
        second = await client.call_tool("cancel_appointment", unknown_code)
        assert error_text(first).endswith(IDENTITY_FAILED_MESSAGE)
        assert error_text(second).endswith(IDENTITY_FAILED_MESSAGE)
        await client.call_tool("lookup_appointments", wrong_dob)  # third strike

        correct = {"confirmation_code": "LKC-7Q2M", "date_of_birth": "1985-03-14"}
        locked = await client.call_tool("lookup_appointments", correct)
        assert error_text(locked).endswith(LOCKED_OUT_MESSAGE)
        # Booking does not verify an existing identity, so it is not locked.
        slot = await first_slot(client, specialty="dermatology")
        payload(
            await client.call_tool("book_appointment", {**NEW_PATIENT, "slot_id": slot["slot_id"]})
        )


async def test_unexpected_errors_do_not_leak_details(settings, monkeypatch, caplog) -> None:
    def explode(self, **kwargs):
        raise RuntimeError("disk I/O error at /var/secret/clinic.db")

    monkeypatch.setattr(ClinicStore, "lookup", explode)
    async with connect(settings) as client:
        with caplog.at_level(logging.ERROR):
            result = await client.call_tool(
                "lookup_appointments", {"date_of_birth": "1985-03-14", "phone": "5550101001"}
            )
        assert error_text(result).endswith(INTERNAL_ERROR_MESSAGE)
        assert "secret" not in error_text(result)
        assert "secret" in caplog.text  # the operator still gets the traceback


async def test_knowledge_base_search(settings) -> None:
    async with connect(settings) as client:
        hit = payload(
            await client.call_tool("search_knowledge_base", {"query": "presciption refil"})
        )
        assert hit["no_confident_match"] is False
        assert hit["results"][0]["doc_id"] == "prescription-refills"
        assert len(hit["results"]) == 4

        miss = payload(await client.call_tool("search_knowledge_base", {"query": "tell me a joke"}))
        assert miss == {"query": "tell me a joke", "results": [], "no_confident_match": True}


async def test_knowledge_base_text_is_sanitised(tmp_path) -> None:
    kb = tmp_path / "kb"
    kb.mkdir()
    (kb / "evil.md").write_text(
        "---\ndoc_id: evil\ntitle: Parking\n---\n## Parking\n"
        "Free parking\x1b[2J in the garage.‮ " + "garage parking " * 400,
        encoding="utf-8",
    )
    async with connect(make_settings(tmp_path, kb_dir=kb)) as c:
        result = payload(await c.call_tool("search_knowledge_base", {"query": "parking garage"}))
    text = result["results"][0]["text"]
    assert "\x1b" not in text and "‮" not in text
    assert len(text) <= 1200


async def test_audit_log_masks_personal_data(settings, caplog) -> None:
    async with connect(settings) as client:
        slot = await first_slot(client, specialty="pediatrics")
        with caplog.at_level(logging.INFO, logger="receptionist.audit"):
            await client.call_tool("book_appointment", {**NEW_PATIENT, "slot_id": slot["slot_id"]})
        (line,) = [r.getMessage() for r in caplog.records if r.name == "receptionist.audit"]
        event = json.loads(line)
        assert event["tool"] == "book_appointment" and event["outcome"] == "ok"
        assert event["args"] == {
            "slot_id": slot["slot_id"],
            "patient_name": "N. P.",
            "date_of_birth": "1990-**-**",
            "phone": "**********99",
            "reason": "<11 chars>",
        }
        for secret in ("Nora", "Patel", "06-15", "5550109999", "sore throat"):
            assert secret not in line
