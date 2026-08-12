# Copyright 2026 ProofStitch contributors
# Licensed under the Apache License, Version 2.0 (the "License").

"""Bounded function tools exposed to the Google ADK agent."""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import ValidationError

from app.demo import build_demo_packet, trusted_demo_evidence_ids
from app.gate import (
    GatePacket,
    approval_scope,
    evaluate_packet,
    requires_human_approval,
)

_MAX_PACKET_BYTES = 64 * 1024
_MAX_FIXED_DEMO_RESULT_BYTES = 4 * 1024
_ACTION_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")


def evaluate_release_gate(packet_json: str) -> dict[str, Any]:
    """Evaluate a JSON release packet without executing commands or external actions.

    Args:
        packet_json: A JSON object matching the ProofStitch GatePacket schema.

    Returns:
        A deterministic decision with evidence findings and an exact approval scope.
    """

    if len(packet_json.encode("utf-8")) > _MAX_PACKET_BYTES:
        return {
            "status": "BLOCKED",
            "error": "PACKET_TOO_LARGE",
            "detail": "Packet exceeds the 64 KiB limit.",
            "external_action_executed": False,
        }
    try:
        packet = GatePacket.model_validate(json.loads(packet_json))
    except (json.JSONDecodeError, ValidationError):
        return {
            "status": "BLOCKED",
            "error": "PACKET_INVALID",
            "detail": "Packet validation failed.",
            "external_action_executed": False,
        }
    return evaluate_packet(packet).model_dump(mode="json")


def load_safe_demo(stage: int = 0) -> dict[str, Any]:
    """Load a synthetic three-stage demo packet and its deterministic verdict.

    Args:
        stage: 0 is missing evidence, 1 awaits approval, 2 has human approval.

    Returns:
        A non-sensitive packet and verdict. No real external action is performed.
    """

    try:
        packet = build_demo_packet(stage)
    except ValueError as exc:
        return {
            "status": "BLOCKED",
            "error": "DEMO_STAGE_INVALID",
            "detail": str(exc),
            "external_action_executed": False,
        }
    return {
        "packet": packet.model_dump(mode="json"),
        "report": evaluate_packet(
            packet,
            trusted_evidence_ids=trusted_demo_evidence_ids(packet),
            verified_approval_scope=(approval_scope(packet) if stage == 2 else None),
        ).model_dump(mode="json"),
    }


def load_fixed_demo() -> dict[str, Any]:
    """Load the one fixed, non-sensitive model demonstration.

    Returns:
        The stage-one packet and deterministic verdict, bounded to 4 KiB when
        serialized. No caller input or real external action is involved.
    """

    result = load_safe_demo(stage=1)
    serialized = json.dumps(
        result,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    if len(serialized) > _MAX_FIXED_DEMO_RESULT_BYTES:
        return {
            "status": "BLOCKED",
            "error": "FIXED_DEMO_RESULT_TOO_LARGE",
            "detail": "The fixed demonstration exceeded its result limit.",
            "external_action_executed": False,
        }
    return result


def explain_authority_boundary(action: str) -> dict[str, Any]:
    """Explain whether an action requires exact human approval.

    Args:
        action: The requested action name, such as publish, submit, or test.

    Returns:
        The fixed authority rule. This tool never creates an approval.
    """

    normalized = action.strip().lower()
    if not _ACTION_RE.fullmatch(normalized):
        return {
            "action": "invalid",
            "requires_human_approval": True,
            "agent_can_create_approval": False,
            "external_action_executed": False,
        }
    requires_approval = requires_human_approval(normalized)
    return {
        "action": normalized,
        "requires_human_approval": requires_approval,
        "agent_can_create_approval": False,
        "external_action_executed": False,
    }
