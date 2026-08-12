# Copyright 2026 ProofStitch contributors
# Licensed under the Apache License, Version 2.0 (the "License").

"""Auditable workflow orchestration for the ProofStitch product demo."""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from collections import OrderedDict
from datetime import UTC, datetime
from threading import RLock
from typing import Literal

from pydantic import BaseModel

from app.demo import build_demo_packet, trusted_demo_evidence_ids
from app.gate import GatePacket, GateReport, approval_scope, evaluate_packet


class WorkflowAction(BaseModel):
    """One concrete action completed by the release-audit workflow."""

    code: str
    label: str
    status: Literal["completed", "waiting"]
    detail: str
    observed_at: datetime


class EvidenceReceipt(BaseModel):
    """Portable, non-sensitive receipt for one evaluated packet."""

    run_id: str
    packet_fingerprint: str
    decision: str
    verified_requirements: int
    mandatory_requirements: int
    approval_scope: str | None
    issued_at: datetime
    receipt_sha256: str
    external_action_executed: Literal[False] = False


class WorkflowRun(BaseModel):
    """Stored state returned by the workflow API and dashboard."""

    run_id: str
    packet: GatePacket
    report: GateReport
    receipt: EvidenceReceipt
    actions: list[WorkflowAction]
    external_action_executed: Literal[False] = False


class WorkflowCapacityError(RuntimeError):
    """Raised instead of evicting an active workflow run."""


_MAX_RUNS = 32
_RUN_TTL_SECONDS = 15 * 60
_RUNS: OrderedDict[str, tuple[float, WorkflowRun]] = OrderedDict()
_RUNS_LOCK = RLock()


def _prune_runs(*, now: float | None = None) -> None:
    cutoff = (now if now is not None else time.monotonic()) - _RUN_TTL_SECONDS
    expired = [run_id for run_id, (stored_at, _) in _RUNS.items() if stored_at < cutoff]
    for run_id in expired:
        _RUNS.pop(run_id, None)


def _receipt_for(
    packet: GatePacket,
    report: GateReport,
    *,
    issued_at: datetime,
) -> EvidenceReceipt:
    payload = {
        "run_id": packet.run_id,
        "packet_fingerprint": report.packet_fingerprint,
        "decision": report.status,
        "verified_requirements": report.verified_requirements,
        "mandatory_requirements": report.mandatory_requirements,
        "approval_scope": report.approval_scope,
        "issued_at": issued_at.isoformat(),
        "external_action_executed": False,
    }
    receipt_sha256 = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return EvidenceReceipt(**payload, receipt_sha256=receipt_sha256)


def _action(
    code: str,
    label: str,
    detail: str,
    *,
    observed_at: datetime,
    status: Literal["completed", "waiting"] = "completed",
) -> WorkflowAction:
    return WorkflowAction(
        code=code,
        label=label,
        status=status,
        detail=detail,
        observed_at=observed_at,
    )


def execute_demo_workflow(*, now: datetime | None = None) -> WorkflowRun:
    """Run the safe evidence workflow and stop at the human-approval boundary."""

    observed_at = now or datetime.now(UTC)
    run_id = f"demo-audit-{secrets.token_hex(16)}"
    packet = build_demo_packet(1, now=observed_at, run_id=run_id)
    report = evaluate_packet(
        packet,
        now=observed_at,
        trusted_evidence_ids=trusted_demo_evidence_ids(packet),
    )
    receipt = _receipt_for(packet, report, issued_at=observed_at)
    actions = [
        _action(
            "PACKET_INGESTED",
            "Release packet ingested",
            "Parsed requirements and bound the audit to one immutable ref.",
            observed_at=observed_at,
        ),
        _action(
            "EVIDENCE_COLLECTED",
            "Required evidence collected",
            "Attached test, privacy, and media receipts to their requirements.",
            observed_at=observed_at,
        ),
        _action(
            "INTEGRITY_VERIFIED",
            "Integrity and freshness verified",
            "Validated SHA-256 digests, timestamps, evidence kinds, and source refs.",
            observed_at=observed_at,
        ),
        _action(
            "PRIVACY_SCANNED",
            "Public metadata scanned",
            "Rejected credential, email, and unmasked phone-number shapes.",
            observed_at=observed_at,
        ),
        _action(
            "RECEIPT_STORED",
            "Evidence receipt stored",
            f"Recorded receipt {receipt.receipt_sha256[:12]} without sensitive values.",
            observed_at=observed_at,
        ),
        _action(
            "HUMAN_APPROVAL_REQUIRED",
            "Exact human approval requested",
            f"Waiting for scope {report.approval_scope}.",
            observed_at=observed_at,
            status="waiting",
        ),
    ]
    workflow_run = WorkflowRun(
        run_id=run_id,
        packet=packet,
        report=report,
        receipt=receipt,
        actions=actions,
    )
    with _RUNS_LOCK:
        _prune_runs()
        if len(_RUNS) >= _MAX_RUNS:
            raise WorkflowCapacityError("workflow capacity reached")
        _RUNS[run_id] = (time.monotonic(), workflow_run)
    return workflow_run.model_copy(deep=True)


def record_demo_human_approval(
    run_id: str,
    *,
    now: datetime | None = None,
) -> WorkflowRun:
    """Record a user-triggered synthetic approval for an existing demo run."""

    with _RUNS_LOCK:
        _prune_runs()
        stored = _RUNS.pop(run_id, None)
    if stored is None:
        raise KeyError(run_id)
    _, existing = stored

    observed_at = now or datetime.now(UTC)
    packet = existing.packet.model_copy(deep=True)
    report = evaluate_packet(
        packet,
        now=observed_at,
        trusted_evidence_ids=trusted_demo_evidence_ids(packet),
        verified_approval_scope=approval_scope(packet),
    )
    receipt = _receipt_for(packet, report, issued_at=observed_at)
    actions = [
        *[item.model_copy(deep=True) for item in existing.actions[:-1]],
        _action(
            "HUMAN_APPROVAL_RECORDED",
            "Synthetic human approval recorded",
            f"Matched the exact scope {report.approval_scope}.",
            observed_at=observed_at,
        ),
        _action(
            "HANDOFF_READY",
            "Human-action handoff prepared",
            "The external action remains outside the agent and has not been executed.",
            observed_at=observed_at,
        ),
    ]
    workflow_run = WorkflowRun(
        run_id=run_id,
        packet=packet,
        report=report,
        receipt=receipt,
        actions=actions,
    )
    return workflow_run.model_copy(deep=True)


def get_workflow_run(run_id: str) -> WorkflowRun | None:
    """Return an isolated copy of a stored workflow run, if present."""

    with _RUNS_LOCK:
        _prune_runs()
        stored = _RUNS.get(run_id)
        return stored[1].model_copy(deep=True) if stored else None
