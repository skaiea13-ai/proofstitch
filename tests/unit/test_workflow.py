from __future__ import annotations

import re
from collections import OrderedDict
from datetime import UTC, datetime, timedelta

import pytest

from app import workflow as workflow_module
from app.workflow import (
    WorkflowCapacityError,
    execute_demo_workflow,
    get_workflow_run,
    record_demo_human_approval,
)


def test_complete_workflow_stops_at_exact_human_boundary() -> None:
    observed_at = datetime(2026, 8, 9, 1, 2, 3, tzinfo=UTC)

    workflow = execute_demo_workflow(now=observed_at)

    assert workflow.report.status == "AWAITING_APPROVAL"
    assert workflow.report.coverage == 1
    assert workflow.report.approval_scope is not None
    assert workflow.report.approval_scope.startswith(
        "proofstitch-demo:submit:8f31c20-demo-immutable-ref:"
    )
    assert workflow.report.approval_scope.endswith(workflow.report.packet_fingerprint)
    assert len(workflow.actions) == 6
    assert workflow.actions[-1].status == "waiting"
    assert re.fullmatch(r"[0-9a-f]{64}", workflow.receipt.receipt_sha256)
    assert workflow.external_action_executed is False


def test_human_approval_changes_decision_without_changing_evidence_contract() -> None:
    started_at = datetime(2026, 8, 9, 1, 2, 3, tzinfo=UTC)
    initial = execute_demo_workflow(now=started_at)

    approved = record_demo_human_approval(
        initial.run_id,
        now=started_at + timedelta(minutes=1),
    )

    assert approved.report.status == "READY_FOR_HUMAN_ACTION"
    assert approved.report.packet_fingerprint == initial.report.packet_fingerprint
    assert approved.receipt.receipt_sha256 != initial.receipt.receipt_sha256
    assert approved.actions[-2].code == "HUMAN_APPROVAL_RECORDED"
    assert approved.actions[-1].code == "HANDOFF_READY"
    assert approved.external_action_executed is False


def test_workflow_store_returns_isolated_copies() -> None:
    workflow = execute_demo_workflow(now=datetime(2026, 8, 9, 4, 5, 6, tzinfo=UTC))
    workflow.actions.clear()

    stored = get_workflow_run(workflow.run_id)

    assert stored is not None
    assert len(stored.actions) == 6


def test_workflow_approval_is_one_shot() -> None:
    workflow = execute_demo_workflow()

    record_demo_human_approval(workflow.run_id)

    try:
        record_demo_human_approval(workflow.run_id)
    except KeyError:
        pass
    else:
        raise AssertionError("a consumed approval run must not be replayable")


def test_workflow_store_rejects_capacity_without_evicting(monkeypatch) -> None:
    monkeypatch.setattr(workflow_module, "_MAX_RUNS", 2)
    monkeypatch.setattr(workflow_module, "_RUNS", OrderedDict())

    first = execute_demo_workflow()
    second = execute_demo_workflow()

    with pytest.raises(WorkflowCapacityError, match="workflow capacity reached"):
        execute_demo_workflow()

    assert get_workflow_run(first.run_id) is not None
    assert get_workflow_run(second.run_id) is not None


def test_workflow_run_ids_are_unpredictable() -> None:
    workflow = execute_demo_workflow()

    assert re.fullmatch(r"demo-audit-[0-9a-f]{32}", workflow.run_id)
