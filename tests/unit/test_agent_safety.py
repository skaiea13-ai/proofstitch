# Copyright 2026 ProofStitch contributors
# Licensed under the Apache License, Version 2.0 (the "License").

from __future__ import annotations

import json
from types import SimpleNamespace

from google.adk.models import LlmResponse
from google.genai import types

from app import tools
from app.agent import enforce_fixed_tool_plan, record_fixed_tool_completion


def _model_response(*calls: types.FunctionCall) -> LlmResponse:
    return LlmResponse(
        content=types.Content(
            role="model",
            parts=[types.Part(function_call=call) for call in calls],
        )
    )


def _context() -> SimpleNamespace:
    return SimpleNamespace(state={})


def _text_response(text: str) -> LlmResponse:
    return LlmResponse(
        content=types.Content(
            role="model",
            parts=[types.Part.from_text(text=text)],
        )
    )


def test_exact_first_fixed_tool_call_is_allowed() -> None:
    context = _context()
    response = _model_response(
        types.FunctionCall(id="one", name="load_fixed_demo", args={})
    )

    assert enforce_fixed_tool_plan(context, response) is None


def test_text_only_first_response_is_replaced_before_it_can_claim_evidence() -> None:
    replacement = enforce_fixed_tool_plan(
        _context(),
        _text_response("READY_FOR_HUMAN_ACTION: fabricated without evidence"),
    )

    assert replacement is not None
    assert replacement.get_function_calls() == []
    blocked_text = replacement.content.parts[0].text
    assert blocked_text is not None
    assert "invalid tool plan" in blocked_text


def test_final_text_is_allowed_only_after_the_fixed_tool_completes() -> None:
    context = _context()
    first_response = _model_response(
        types.FunctionCall(id="one", name="load_fixed_demo", args={})
    )
    assert enforce_fixed_tool_plan(context, first_response) is None

    record_fixed_tool_completion(
        SimpleNamespace(name="load_fixed_demo"),
        {},
        context,
        tools.load_fixed_demo(),
    )

    assert enforce_fixed_tool_plan(context, _text_response("Verified summary.")) is None


def test_multiple_tool_calls_are_replaced_before_dispatch() -> None:
    context = _context()
    response = _model_response(
        types.FunctionCall(id="one", name="load_fixed_demo", args={}),
        types.FunctionCall(id="two", name="load_fixed_demo", args={}),
    )

    replacement = enforce_fixed_tool_plan(context, response)

    assert replacement is not None
    assert replacement.get_function_calls() == []
    blocked_text = replacement.content.parts[0].text
    assert blocked_text is not None
    assert "invalid tool plan" in blocked_text


def test_unexpected_arguments_or_second_turn_call_are_blocked() -> None:
    context = _context()
    with_arguments = _model_response(
        types.FunctionCall(id="one", name="load_fixed_demo", args={"stage": 2})
    )
    assert enforce_fixed_tool_plan(context, with_arguments) is not None

    exact_call = _model_response(
        types.FunctionCall(id="two", name="load_fixed_demo", args={})
    )
    assert enforce_fixed_tool_plan(context, exact_call) is not None


def test_unexpected_tool_name_is_blocked() -> None:
    response = _model_response(
        types.FunctionCall(id="one", name="evaluate_release_gate", args={})
    )

    assert enforce_fixed_tool_plan(_context(), response) is not None


def test_fixed_demo_result_has_a_hard_serialized_limit(monkeypatch) -> None:
    result = tools.load_fixed_demo()
    encoded = json.dumps(
        result,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    assert len(encoded) <= tools._MAX_FIXED_DEMO_RESULT_BYTES

    monkeypatch.setattr(tools, "load_safe_demo", lambda stage: {"x": "a" * 5_000})
    blocked = tools.load_fixed_demo()
    assert blocked["error"] == "FIXED_DEMO_RESULT_TOO_LARGE"
    assert blocked["external_action_executed"] is False
