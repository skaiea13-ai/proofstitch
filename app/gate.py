# Copyright 2026 ProofStitch contributors
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.

"""Deterministic evidence and human-approval gates for release workflows."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Collection, Iterator
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

GateStatus = Literal[
    "BLOCKED",
    "AWAITING_APPROVAL",
    "READY",
    "READY_FOR_HUMAN_ACTION",
]

SAFE_LOCAL_ACTIONS = frozenset({"analyze", "draft", "test", "validate"})
EvidenceKind = Annotated[
    str,
    Field(
        min_length=2,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_-]{1,63}$",
    ),
]

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_PATTERNS = (
    (
        "email_address",
        re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE),
    ),
    ("unmasked_phone", re.compile(r"(?<![\d*•xX])\+\d{8,15}\b")),
    (
        "secret_assignment",
        re.compile(
            r"\b(?:api[_-]?key|access[_-]?token|secret|password)\s*[:=]\s*[^\s,;]+",
            re.IGNORECASE,
        ),
    ),
    (
        "credential_shape",
        re.compile(r"\b(?:AIza[\w-]{20,}|gh[pousr]_[\w]{20,}|sk-[\w-]{20,})\b"),
    ),
    ("jwt_shape", re.compile(r"\beyJ[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{8,}")),
    (
        "korean_phone",
        re.compile(r"(?<!\d)01[016789][-. ]?\d{3,4}[-. ]?\d{4}(?!\d)"),
    ),
    (
        "user_home_path",
        re.compile(r"(?:/Users/|[A-Za-z]:\\Users\\)[^/\\\s]+"),
    ),
)


class StrictModel(BaseModel):
    """Reject undeclared input fields and normalize surrounding whitespace."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Requirement(StrictModel):
    """One verifiable condition extracted from a release or contest rule."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")
    text: str = Field(min_length=3, max_length=500)
    mandatory: bool = True
    accepted_kinds: list[EvidenceKind] = Field(min_length=1, max_length=16)
    max_age_hours: int = Field(default=24, ge=1, le=24 * 30)
    expected_ref: str = Field(min_length=4, max_length=128)


class Evidence(StrictModel):
    """A bounded, non-sensitive proof attached to one requirement."""

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")
    requirement_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")
    kind: EvidenceKind
    status: Literal["passed", "failed"]
    observed_at: datetime
    sha256: str = Field(min_length=64, max_length=64, pattern=r"^[0-9a-f]{64}$")
    source_ref: str = Field(min_length=4, max_length=128)
    summary: str = Field(min_length=1, max_length=256)


class GatePacket(StrictModel):
    """Complete input for a deterministic release-readiness decision."""

    run_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")
    project_slug: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{1,63}$")
    requested_action: str = Field(
        min_length=2,
        max_length=64,
        pattern=r"^[a-z][a-z0-9_-]{1,63}$",
    )
    expected_ref: str = Field(min_length=4, max_length=128)
    requirements: list[Requirement] = Field(min_length=1, max_length=32)
    evidence: list[Evidence] = Field(default_factory=list, max_length=64)

    @model_validator(mode="after")
    def validate_unique_ids(self) -> GatePacket:
        requirement_ids = [item.id for item in self.requirements]
        evidence_ids = [item.id for item in self.evidence]
        if len(requirement_ids) != len(set(requirement_ids)):
            raise ValueError("requirement ids must be unique")
        if len(evidence_ids) != len(set(evidence_ids)):
            raise ValueError("evidence ids must be unique")
        if any(item.expected_ref != self.expected_ref for item in self.requirements):
            raise ValueError("requirement refs must match the packet ref")
        if any(
            _sensitive_labels(value)
            for value in _iter_strings(self.model_dump(mode="json"))
        ):
            raise ValueError("packet contains disallowed sensitive data")
        return self


class Finding(StrictModel):
    """One auditable reason contributing to the gate decision."""

    code: str
    severity: Literal["blocker", "warning", "info"]
    subject_id: str
    message: str


class GateReport(StrictModel):
    """Stable result returned to the agent, API, and dashboard."""

    run_id: str
    status: GateStatus
    coverage: float = Field(ge=0, le=1)
    verified_requirements: int
    mandatory_requirements: int
    findings: list[Finding]
    next_actions: list[str]
    approval_scope: str | None
    packet_fingerprint: str
    evaluated_at: datetime
    external_action_executed: Literal[False] = False


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _sensitive_labels(value: str) -> list[str]:
    return [label for label, pattern in _SENSITIVE_PATTERNS if pattern.search(value)]


def _iter_strings(value: Any) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _iter_strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _iter_strings(item)


def requires_human_approval(action: str) -> bool:
    """Fail closed: only a small local-only action set bypasses approval."""

    return action not in SAFE_LOCAL_ACTIONS


def approval_scope(packet: GatePacket) -> str:
    """Return the exact scope a human approval must match."""

    return (
        f"{packet.project_slug}:{packet.requested_action}:{packet.expected_ref}:"
        f"{packet_fingerprint(packet)}"
    )


def packet_fingerprint(packet: GatePacket) -> str:
    """Hash the evidence contract without mutable approval artifacts."""

    canonical = json.dumps(
        packet.model_dump(mode="json"),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def evaluate_packet(
    packet: GatePacket,
    *,
    now: datetime | None = None,
    trusted_evidence_ids: Collection[str] = (),
    verified_approval_scope: str | None = None,
) -> GateReport:
    """Evaluate evidence freshness, integrity, privacy, and approval boundaries."""

    evaluated_at = _as_utc(now or datetime.now(UTC))
    findings: list[Finding] = []
    requirement_map = {item.id: item for item in packet.requirements}
    valid_evidence: dict[str, list[Evidence]] = {
        item.id: [] for item in packet.requirements
    }
    trusted_ids = frozenset(trusted_evidence_ids)

    for item in packet.evidence:
        requirement = requirement_map.get(item.requirement_id)
        if requirement is None:
            findings.append(
                Finding(
                    code="UNKNOWN_REQUIREMENT",
                    severity="blocker",
                    subject_id=item.id,
                    message="Evidence points to an unknown requirement.",
                )
            )
            continue

        item_valid = True
        if item.id not in trusted_ids:
            item_valid = False
            findings.append(
                Finding(
                    code="EVIDENCE_UNTRUSTED",
                    severity="blocker",
                    subject_id=item.id,
                    message="Evidence was not issued by a trusted server-side collector.",
                )
            )
        if item.status != "passed":
            item_valid = False
            findings.append(
                Finding(
                    code="EVIDENCE_FAILED",
                    severity="blocker",
                    subject_id=item.id,
                    message="The attached check did not pass.",
                )
            )
        if not _SHA256_RE.fullmatch(item.sha256):
            item_valid = False
            findings.append(
                Finding(
                    code="DIGEST_INVALID",
                    severity="blocker",
                    subject_id=item.id,
                    message="Evidence must carry a lowercase SHA-256 digest.",
                )
            )
        if item.kind not in requirement.accepted_kinds:
            item_valid = False
            findings.append(
                Finding(
                    code="EVIDENCE_KIND_REJECTED",
                    severity="blocker",
                    subject_id=item.id,
                    message="Evidence kind is not accepted for this requirement.",
                )
            )
        observed_at = _as_utc(item.observed_at)
        if observed_at > evaluated_at + timedelta(minutes=5):
            item_valid = False
            findings.append(
                Finding(
                    code="EVIDENCE_FROM_FUTURE",
                    severity="blocker",
                    subject_id=item.id,
                    message="Evidence timestamp is later than the evaluation clock.",
                )
            )
        if evaluated_at - observed_at > timedelta(hours=requirement.max_age_hours):
            item_valid = False
            findings.append(
                Finding(
                    code="EVIDENCE_STALE",
                    severity="blocker",
                    subject_id=item.id,
                    message="Evidence is older than the requirement permits.",
                )
            )
        if item.source_ref != requirement.expected_ref:
            item_valid = False
            findings.append(
                Finding(
                    code="REF_MISMATCH",
                    severity="blocker",
                    subject_id=item.id,
                    message="Evidence was collected from a different immutable ref.",
                )
            )
        sensitive = _sensitive_labels(f"{item.source_ref}\n{item.summary}")
        if sensitive:
            item_valid = False
            findings.append(
                Finding(
                    code="SENSITIVE_METADATA",
                    severity="blocker",
                    subject_id=item.id,
                    message="Evidence metadata contains disallowed sensitive data: "
                    + ", ".join(sensitive),
                )
            )
        if item_valid:
            valid_evidence[item.requirement_id].append(item)

    mandatory = [item for item in packet.requirements if item.mandatory]
    for requirement in mandatory:
        if not valid_evidence[requirement.id]:
            findings.append(
                Finding(
                    code="EVIDENCE_MISSING",
                    severity="blocker",
                    subject_id=requirement.id,
                    message="No valid evidence satisfies this mandatory requirement.",
                )
            )

    verified = sum(1 for item in mandatory if valid_evidence[item.id])
    coverage = verified / len(mandatory) if mandatory else 1.0
    blockers = [item for item in findings if item.severity == "blocker"]
    scope = (
        approval_scope(packet)
        if requires_human_approval(packet.requested_action)
        else None
    )

    if blockers:
        status: GateStatus = "BLOCKED"
        next_actions = [
            "Replace each blocked or missing item with fresh, non-sensitive evidence."
        ]
    elif scope is not None:
        if verified_approval_scope != scope:
            status = "AWAITING_APPROVAL"
            findings.append(
                Finding(
                    code="APPROVAL_REQUIRED",
                    severity="info",
                    subject_id=packet.requested_action,
                    message="A human must approve the exact action, project, and ref.",
                )
            )
            next_actions = [f"Request human approval for scope: {scope}"]
        else:
            status = "READY_FOR_HUMAN_ACTION"
            next_actions = [
                "A human operator may perform the scoped action; re-evaluate after any change."
            ]
    else:
        status = "READY"
        next_actions = ["Continue with the verified local workflow."]

    return GateReport(
        run_id=packet.run_id,
        status=status,
        coverage=coverage,
        verified_requirements=verified,
        mandatory_requirements=len(mandatory),
        findings=findings,
        next_actions=next_actions,
        approval_scope=scope,
        packet_fingerprint=packet_fingerprint(packet),
        evaluated_at=evaluated_at,
    )
