from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from app.gate import (
    Evidence,
    GatePacket,
    Requirement,
    approval_scope,
    evaluate_packet,
)
from app.tools import evaluate_release_gate

NOW = datetime(2026, 8, 9, 12, 0, tzinfo=UTC)
REF = "abc1234-immutable-release-ref"


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


def _packet(*, include_evidence: bool = True) -> GatePacket:
    requirement = Requirement(
        id="tests",
        text="Tests pass on the immutable ref.",
        accepted_kinds=["test_receipt"],
        expected_ref=REF,
        max_age_hours=24,
    )
    evidence = []
    if include_evidence:
        evidence.append(
            Evidence(
                id="test-proof",
                requirement_id="tests",
                kind="test_receipt",
                status="passed",
                observed_at=NOW,
                sha256=_digest("test-proof"),
                source_ref=REF,
                summary="18 tests passed.",
            )
        )
    return GatePacket(
        run_id="release-001",
        project_slug="proofstitch",
        requested_action="submit",
        expected_ref=REF,
        requirements=[requirement],
        evidence=evidence,
    )


def test_missing_mandatory_evidence_blocks() -> None:
    report = evaluate_packet(_packet(include_evidence=False), now=NOW)

    assert report.status == "BLOCKED"
    assert report.coverage == 0
    assert {finding.code for finding in report.findings} == {"EVIDENCE_MISSING"}
    assert report.external_action_executed is False


def test_verified_external_action_waits_for_exact_human_approval() -> None:
    packet = _packet()
    report = evaluate_packet(
        packet,
        now=NOW,
        trusted_evidence_ids={"test-proof"},
    )

    assert report.status == "AWAITING_APPROVAL"
    assert report.coverage == 1
    assert report.approval_scope == approval_scope(packet)
    assert report.approval_scope.endswith(report.packet_fingerprint)
    assert "APPROVAL_REQUIRED" in {item.code for item in report.findings}


def test_matching_human_approval_never_executes_the_action() -> None:
    packet = _packet()
    report = evaluate_packet(
        packet,
        now=NOW,
        trusted_evidence_ids={"test-proof"},
        verified_approval_scope=approval_scope(packet),
    )

    assert report.status == "READY_FOR_HUMAN_ACTION"
    assert report.external_action_executed is False


def test_wrong_approval_scope_is_rejected() -> None:
    packet = _packet()
    report = evaluate_packet(
        packet,
        now=NOW,
        trusted_evidence_ids={"test-proof"},
        verified_approval_scope="proofstitch:submit:different-ref",
    )

    assert report.status == "AWAITING_APPROVAL"


def test_caller_supplied_evidence_is_untrusted_by_default() -> None:
    report = evaluate_packet(_packet(), now=NOW)

    assert report.status == "BLOCKED"
    assert "EVIDENCE_UNTRUSTED" in {item.code for item in report.findings}


def test_unknown_action_fails_closed_at_human_boundary() -> None:
    packet = _packet()
    packet.requested_action = "deploy"

    report = evaluate_packet(
        packet,
        now=NOW,
        trusted_evidence_ids={"test-proof"},
    )

    assert report.status == "AWAITING_APPROVAL"
    assert report.approval_scope == approval_scope(packet)


def test_stale_evidence_blocks() -> None:
    packet = _packet()
    packet.evidence[0].observed_at = NOW - timedelta(hours=25)

    codes = {
        item.code
        for item in evaluate_packet(
            packet,
            now=NOW,
            trusted_evidence_ids={"test-proof"},
        ).findings
    }

    assert "EVIDENCE_STALE" in codes
    assert "EVIDENCE_MISSING" in codes


def test_mismatched_ref_blocks() -> None:
    packet = _packet()
    packet.evidence[0].source_ref = "different-immutable-ref"

    report = evaluate_packet(
        packet,
        now=NOW,
        trusted_evidence_ids={"test-proof"},
    )

    assert report.status == "BLOCKED"
    assert "REF_MISMATCH" in {item.code for item in report.findings}


def test_sensitive_metadata_blocks_without_echoing_value() -> None:
    packet = _packet()
    sensitive_value = "demo" + chr(64) + "sample.invalid"
    packet.evidence[0].summary = f"Receipt owner: {sensitive_value}"

    report = evaluate_packet(
        packet,
        now=NOW,
        trusted_evidence_ids={"test-proof"},
    )
    rendered = report.model_dump_json()

    assert report.status == "BLOCKED"
    assert "SENSITIVE_METADATA" in {item.code for item in report.findings}
    assert sensitive_value not in rendered


def test_tool_returns_structured_block_for_invalid_json() -> None:
    result = evaluate_release_gate("not-json")

    assert result["status"] == "BLOCKED"
    assert result["error"] == "PACKET_INVALID"
    assert result["detail"] == "Packet validation failed."
    assert result["external_action_executed"] is False


def test_sensitive_value_anywhere_in_packet_is_rejected_without_echo() -> None:
    packet = _packet().model_dump(mode="json")
    sensitive_value = "SENTINEL-secret=do-not-echo"
    packet["requirements"][0]["text"] = sensitive_value

    with pytest.raises(ValidationError):
        GatePacket.model_validate(packet)

    result = evaluate_release_gate(json.dumps(packet))
    assert result["status"] == "BLOCKED"
    assert result["detail"] == "Packet validation failed."
    assert sensitive_value not in json.dumps(result)


def test_oversized_tool_packet_is_rejected_before_parsing() -> None:
    result = evaluate_release_gate(" " * 65_537)

    assert result["status"] == "BLOCKED"
    assert result["error"] == "PACKET_TOO_LARGE"
    assert result["detail"] == "Packet exceeds the 64 KiB limit."


def test_packet_fingerprint_changes_when_evidence_changes() -> None:
    first = _packet()
    second = GatePacket.model_validate_json(first.model_dump_json())
    second.evidence[0].summary = "19 tests passed."

    first_report = evaluate_release_gate(first.model_dump_json())
    second_report = evaluate_release_gate(json.dumps(second.model_dump(mode="json")))

    assert first_report["packet_fingerprint"] != second_report["packet_fingerprint"]


def test_approval_scope_is_bound_to_exact_evidence_contract() -> None:
    first = _packet()
    second = GatePacket.model_validate_json(first.model_dump_json())
    second.evidence[0].summary = "19 tests passed."

    assert approval_scope(first) != approval_scope(second)


def test_requirement_policy_ref_must_match_packet_ref() -> None:
    packet = _packet().model_dump(mode="json")
    packet["requirements"][0]["expected_ref"] = "different-immutable-ref"

    with pytest.raises(ValidationError):
        GatePacket.model_validate(packet)


def test_evidence_kind_values_are_bounded() -> None:
    packet = _packet().model_dump(mode="json")
    packet["requirements"][0]["accepted_kinds"] = ["x" * 65]

    with pytest.raises(ValidationError):
        GatePacket.model_validate(packet)
