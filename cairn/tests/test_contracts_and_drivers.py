from __future__ import annotations

import json

import pytest

from cairn.dispatcher.contracts import (
    parse_json_output,
    validate_bootstrap_execute_payload,
    validate_explore_payload,
    validate_reason_payload,
)
from cairn.dispatcher.runtime.process import ManagedProcess
from cairn.dispatcher.workers.adapters.pi import PiDriver
from cairn.server.models import CreateIntentRequest


def test_parse_json_output_extracts_object_from_markdown_noise() -> None:
    assert parse_json_output('result:\n```json\n{"accepted": true, "data": {}}\n```') == {
        "accepted": True,
        "data": {},
    }


def test_reason_payload_limits_number_of_intents() -> None:
    kind, intents, recommended_next = validate_reason_payload(
        {
            "accepted": True,
            "data": {
                "intents": [
                    {"from": ["f001"], "description": "one"},
                    {"from": ["f001"], "description": "two"},
                ]
            },
        },
        open_intents_empty=True,
        max_intents=1,
    )

    assert kind == "intents"
    assert intents == [{"from": ["f001"], "description": "one"}]
    assert recommended_next is None


def test_reason_payload_requires_intent_when_none_are_open() -> None:
    with pytest.raises(ValueError, match="intents is required"):
        validate_reason_payload(
            {"accepted": True, "data": {}},
            open_intents_empty=True,
            max_intents=3,
        )


def test_reason_payload_complete_normalizes_goals() -> None:
    kind, complete, recommended_next = validate_reason_payload(
        {
            "accepted": True,
            "data": {
                "complete": {"from": ["f001"], "goals": [" g001", "g001"], "description": "done"}
            },
        },
        open_intents_empty=False,
        max_intents=3,
    )

    assert kind == "complete"
    assert complete["goals"] == ["g001"]
    assert recommended_next is None


def test_intent_payload_deduplicates_from_ids() -> None:
    request = CreateIntentRequest.model_validate(
        {"from": ["origin", " origin"], "description": "d", "creator": "c"}
    )

    assert request.from_ == ["origin"]


def test_reason_payload_complete_rejects_empty_goals_list() -> None:
    with pytest.raises(ValueError, match="goals"):
        validate_reason_payload(
            {
                "accepted": True,
                "data": {
                    "complete": {"from": ["f001"], "goals": [], "description": "done"}
                },
            },
            open_intents_empty=False,
            max_intents=3,
        )


def test_reason_payload_parses_recommended_next() -> None:
    kind, intents, recommended_next = validate_reason_payload(
        {
            "accepted": True,
            "data": {
                "intents": [{"from": ["f001"], "description": "one"}],
                "recommended_next": " i003 ",
            },
        },
        open_intents_empty=False,
        max_intents=3,
    )

    assert kind == "intents"
    assert recommended_next == "i003"


def test_reason_payload_recommended_next_accepts_null_and_bare_payload() -> None:
    kind, _data, recommended_next = validate_reason_payload(
        {"intents": [{"from": ["f001"], "description": "one"}], "recommended_next": None},
        open_intents_empty=False,
        max_intents=3,
    )

    assert kind == "intents"
    assert recommended_next is None


def test_reason_payload_recommended_next_passes_blank_string_through() -> None:
    _kind, _data, recommended_next = validate_reason_payload(
        {
            "accepted": True,
            "data": {
                "intents": [{"from": ["f001"], "description": "one"}],
                "recommended_next": "   ",
            },
        },
        open_intents_empty=False,
        max_intents=3,
    )

    assert recommended_next == ""


def test_reason_payload_bare_recommended_next_only_payload_is_noop() -> None:
    kind, data, recommended_next = validate_reason_payload(
        {"recommended_next": " i003 "},
        open_intents_empty=False,
        max_intents=3,
    )

    assert kind == "noop"
    assert data is None
    assert recommended_next == "i003"


def test_reason_payload_rejects_bare_empty_payload() -> None:
    with pytest.raises(ValueError, match="accepted must be true or false"):
        validate_reason_payload({}, open_intents_empty=False, max_intents=3)


def test_reason_payload_recommended_next_rejects_non_string() -> None:
    with pytest.raises(ValueError, match="recommended_next"):
        validate_reason_payload(
            {
                "accepted": True,
                "data": {
                    "intents": [{"from": ["f001"], "description": "one"}],
                    "recommended_next": 3,
                },
            },
            open_intents_empty=False,
            max_intents=3,
        )


def test_bootstrap_payload_complete_normalizes_goals() -> None:
    kind, result = validate_bootstrap_execute_payload(
        {
            "accepted": True,
            "data": {
                "fact": {"description": "found"},
                "complete": {"goals": ["g001"], "description": "met"},
            },
        }
    )

    assert kind == "complete"
    assert result["complete_goals"] == ["g001"]


def test_bootstrap_payload_complete_goals_are_required() -> None:
    with pytest.raises(ValueError, match="goals"):
        validate_bootstrap_execute_payload(
            {
                "accepted": True,
                "data": {
                    "fact": {"description": "found"},
                    "complete": {"description": "met"},
                },
            }
        )


def test_explore_payload_rejects_planning_text() -> None:
    with pytest.raises(ValueError):
        validate_explore_payload(parse_json_output("Need inspect files and keep working."))


def test_pi_driver_extracts_session_and_last_assistant_text() -> None:
    driver = PiDriver()
    stdout = "\n".join(
        [
            json.dumps({"type": "session", "id": "session-123"}),
            json.dumps(
                {
                    "type": "turn_end",
                    "message": {
                        "role": "assistant",
                        "content": [{"type": "text", "text": '{"accepted":true,"data":{}}'}],
                    },
                }
            ),
        ]
    )

    assert driver.extract_session(None, stdout, "") == "session-123"
    assert driver.extract_response_text(stdout, "") == '{"accepted":true,"data":{}}'


def test_close_stream_closes_response_even_when_stream_close_fails() -> None:
    class Response:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class Stream:
        def __init__(self) -> None:
            self._response = Response()

        def close(self) -> None:
            raise ValueError("already closed")

    stream = Stream()
    ManagedProcess._close_stream(stream)

    assert stream._response.closed

