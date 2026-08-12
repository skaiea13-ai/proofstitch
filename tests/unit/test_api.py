import hashlib
import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app import api as api_module

app = FastAPI()
app.include_router(api_module.router)
client = TestClient(app)
WORKFLOW_HEADERS = {
    "X-ProofStitch-Workflow-Intent": "fixed-synthetic-workflow",
}
MODEL_DEMO_TOKEN = "a" * 64


@pytest.fixture(autouse=True)
def reset_request_limits() -> None:
    with api_module._AGENT_CALLS_LOCK:
        api_module._AGENT_CALLS.clear()
    with api_module._WORKFLOW_CALLS_LOCK:
        api_module._WORKFLOW_CALLS.clear()


def _enable_model_demo(monkeypatch) -> None:
    now = int(time.time())
    monkeypatch.setenv(
        "PROOFSTITCH_MODEL_DEMO_TOKEN_SHA256",
        hashlib.sha256(MODEL_DEMO_TOKEN.encode("ascii")).hexdigest(),
    )
    monkeypatch.setenv("PROOFSTITCH_MODEL_DEMO_NOT_BEFORE", str(now - 1))
    monkeypatch.setenv("PROOFSTITCH_MODEL_DEMO_EXPIRES_AT", str(now + 599))


def test_healthz() -> None:
    assert client.get("/healthz").json() == {
        "status": "ok",
        "service": "proofstitch",
    }


def test_read_only_demo_stages_never_create_approval() -> None:
    stage_zero = client.get("/api/v1/demo/0").json()["report"]
    stage_one = client.get("/api/v1/demo/1").json()["report"]

    assert stage_zero["status"] == "BLOCKED"
    assert stage_one["status"] == "AWAITING_APPROVAL"
    assert client.get("/api/v1/demo/2").status_code == 404
    assert all(
        report["external_action_executed"] is False
        for report in (stage_zero, stage_one)
    )


def test_invalid_demo_stage_is_404() -> None:
    assert client.get("/api/v1/demo/7").status_code == 404


def test_dashboard_is_served() -> None:
    response = client.get("/proofstitch")

    assert response.status_code == 200
    assert "Ship the proof" in response.text
    assert 'type="password"' in response.text
    assert "X-ProofStitch-Demo-Token" in response.text
    assert "localStorage" not in response.text
    assert "sessionStorage" not in response.text


def test_workflow_executes_actions_and_waits_at_human_boundary() -> None:
    response = client.post("/api/v1/workflows/demo", headers=WORKFLOW_HEADERS)

    assert response.status_code == 201
    workflow = response.json()
    assert workflow["report"]["status"] == "AWAITING_APPROVAL"
    assert workflow["external_action_executed"] is False
    assert [item["code"] for item in workflow["actions"]] == [
        "PACKET_INGESTED",
        "EVIDENCE_COLLECTED",
        "INTEGRITY_VERIFIED",
        "PRIVACY_SCANNED",
        "RECEIPT_STORED",
        "HUMAN_APPROVAL_REQUIRED",
    ]
    assert workflow["actions"][-1]["status"] == "waiting"


def test_synthetic_approval_preserves_packet_fingerprint() -> None:
    initial = client.post(
        "/api/v1/workflows/demo", headers=WORKFLOW_HEADERS
    ).json()
    response = client.post(
        f"/api/v1/workflows/{initial['run_id']}/synthetic-approval",
        headers=WORKFLOW_HEADERS,
    )

    assert response.status_code == 200
    approved = response.json()
    assert approved["report"]["status"] == "READY_FOR_HUMAN_ACTION"
    assert (
        approved["report"]["packet_fingerprint"]
        == initial["report"]["packet_fingerprint"]
    )
    assert approved["external_action_executed"] is False
    assert approved["actions"][-1]["code"] == "HANDOFF_READY"


def test_synthetic_approval_rejects_unknown_run() -> None:
    response = client.post(
        "/api/v1/workflows/demo-audit-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/"
        "synthetic-approval",
        headers=WORKFLOW_HEADERS,
    )

    assert response.status_code == 404


def test_synthetic_approval_requires_explicit_intent() -> None:
    initial = client.post(
        "/api/v1/workflows/demo", headers=WORKFLOW_HEADERS
    ).json()

    missing = client.post(
        f"/api/v1/workflows/{initial['run_id']}/synthetic-approval"
    )
    cross_site = client.post(
        f"/api/v1/workflows/{initial['run_id']}/synthetic-approval",
        headers={
            **WORKFLOW_HEADERS,
            "Sec-Fetch-Site": "cross-site",
        },
    )
    approved = client.post(
        f"/api/v1/workflows/{initial['run_id']}/synthetic-approval",
        headers=WORKFLOW_HEADERS,
    )

    assert missing.status_code == 403
    assert cross_site.status_code == 403
    assert approved.status_code == 200


def test_agent_demo_requires_explicit_intent(monkeypatch) -> None:
    async def unexpected_model_call() -> str:
        raise AssertionError("missing intent must not reach the model")

    monkeypatch.setattr("app.api._run_fixed_agent_demo", unexpected_model_call)

    response = client.post("/api/v1/agent/demo")

    assert response.status_code == 403
    assert response.json() == {"detail": "Explicit request intent is required."}


def test_agent_demo_rejects_cross_site_fetch_metadata(monkeypatch) -> None:
    async def unexpected_model_call() -> str:
        raise AssertionError("cross-site intent must not reach the model")

    monkeypatch.setattr("app.api._run_fixed_agent_demo", unexpected_model_call)

    response = client.post(
        "/api/v1/agent/demo",
        headers={
            "X-ProofStitch-Demo-Intent": "fixed-synthetic-demo",
            "Sec-Fetch-Site": "cross-site",
        },
    )

    assert response.status_code == 403
    assert response.json() == {
        "detail": "Cross-site state-changing requests are not allowed."
    }


def test_agent_demo_accepts_explicit_same_origin_intent(monkeypatch) -> None:
    async def fixed_model_response() -> str:
        return "Verified fixed synthetic model response."

    monkeypatch.setattr("app.api._run_fixed_agent_demo", fixed_model_response)
    monkeypatch.setattr("app.api._reserve_agent_call", lambda: True)
    _enable_model_demo(monkeypatch)

    response = client.post(
        "/api/v1/agent/demo",
        headers={
            "X-ProofStitch-Demo-Intent": "fixed-synthetic-demo",
            "X-ProofStitch-Demo-Token": MODEL_DEMO_TOKEN,
            "Sec-Fetch-Site": "same-origin",
        },
    )

    assert response.status_code == 200
    assert response.json()["response"] == "Verified fixed synthetic model response."


def test_agent_demo_is_disabled_without_a_bounded_window(monkeypatch) -> None:
    async def unexpected_model_call() -> str:
        raise AssertionError("disabled demo must not reach the model")

    monkeypatch.setattr("app.api._run_fixed_agent_demo", unexpected_model_call)
    for name in (
        "PROOFSTITCH_MODEL_DEMO_TOKEN_SHA256",
        "PROOFSTITCH_MODEL_DEMO_NOT_BEFORE",
        "PROOFSTITCH_MODEL_DEMO_EXPIRES_AT",
    ):
        monkeypatch.delenv(name, raising=False)

    response = client.post(
        "/api/v1/agent/demo",
        headers={
            "X-ProofStitch-Demo-Intent": "fixed-synthetic-demo",
            "X-ProofStitch-Demo-Token": MODEL_DEMO_TOKEN,
        },
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "Agent demo is disabled."}


def test_agent_demo_rejects_wrong_capability(monkeypatch) -> None:
    async def unexpected_model_call() -> str:
        raise AssertionError("wrong capability must not reach the model")

    monkeypatch.setattr("app.api._run_fixed_agent_demo", unexpected_model_call)
    _enable_model_demo(monkeypatch)

    response = client.post(
        "/api/v1/agent/demo",
        headers={
            "X-ProofStitch-Demo-Intent": "fixed-synthetic-demo",
            "X-ProofStitch-Demo-Token": "b" * 64,
        },
    )

    assert response.status_code == 403
    assert response.json() == {"detail": "Valid agent demo capability is required."}


def test_agent_demo_rejects_expired_window(monkeypatch) -> None:
    async def unexpected_model_call() -> str:
        raise AssertionError("expired demo must not reach the model")

    monkeypatch.setattr("app.api._run_fixed_agent_demo", unexpected_model_call)
    now = int(time.time())
    monkeypatch.setenv(
        "PROOFSTITCH_MODEL_DEMO_TOKEN_SHA256",
        hashlib.sha256(MODEL_DEMO_TOKEN.encode("ascii")).hexdigest(),
    )
    monkeypatch.setenv("PROOFSTITCH_MODEL_DEMO_NOT_BEFORE", str(now - 601))
    monkeypatch.setenv("PROOFSTITCH_MODEL_DEMO_EXPIRES_AT", str(now - 1))

    response = client.post(
        "/api/v1/agent/demo",
        headers={
            "X-ProofStitch-Demo-Intent": "fixed-synthetic-demo",
            "X-ProofStitch-Demo-Token": MODEL_DEMO_TOKEN,
        },
    )

    assert response.status_code == 503
    assert response.json() == {"detail": "Agent demo is disabled."}


def test_workflow_requires_explicit_same_origin_intent(monkeypatch) -> None:
    def unexpected_reservation() -> bool:
        raise AssertionError("missing intent must not reserve workflow quota")

    monkeypatch.setattr("app.api._reserve_workflow_call", unexpected_reservation)

    missing = client.post("/api/v1/workflows/demo")
    cross_site = client.post(
        "/api/v1/workflows/demo",
        headers={
            **WORKFLOW_HEADERS,
            "Sec-Fetch-Site": "cross-site",
        },
    )

    assert missing.status_code == 403
    assert missing.json() == {"detail": "Explicit request intent is required."}
    assert cross_site.status_code == 403
    assert cross_site.json() == {
        "detail": "Cross-site state-changing requests are not allowed."
    }


def test_workflow_rate_limit_fails_before_state_creation(monkeypatch) -> None:
    def unexpected_workflow_call() -> None:
        raise AssertionError("rate-limited request must not create workflow state")

    monkeypatch.setattr("app.api._reserve_workflow_call", lambda: False)
    monkeypatch.setattr("app.api.execute_demo_workflow", unexpected_workflow_call)

    response = client.post("/api/v1/workflows/demo", headers=WORKFLOW_HEADERS)

    assert response.status_code == 429
    assert response.json() == {"detail": "Workflow demo rate limit reached."}


def test_cloud_workflow_requires_operator_capability(monkeypatch) -> None:
    monkeypatch.setenv("K_SERVICE", "proofstitch")
    _enable_model_demo(monkeypatch)

    missing = client.post("/api/v1/workflows/demo", headers=WORKFLOW_HEADERS)
    wrong = client.post(
        "/api/v1/workflows/demo",
        headers={
            **WORKFLOW_HEADERS,
            "X-ProofStitch-Demo-Token": "b" * 64,
        },
    )
    authorized = client.post(
        "/api/v1/workflows/demo",
        headers={
            **WORKFLOW_HEADERS,
            "X-ProofStitch-Demo-Token": MODEL_DEMO_TOKEN,
        },
    )

    assert missing.status_code == 403
    assert wrong.status_code == 403
    assert authorized.status_code == 201


def test_cloud_synthetic_approval_requires_operator_capability(monkeypatch) -> None:
    initial = client.post(
        "/api/v1/workflows/demo",
        headers=WORKFLOW_HEADERS,
    ).json()
    monkeypatch.setenv("K_SERVICE", "proofstitch")
    _enable_model_demo(monkeypatch)

    missing = client.post(
        f"/api/v1/workflows/{initial['run_id']}/synthetic-approval",
        headers=WORKFLOW_HEADERS,
    )
    authorized = client.post(
        f"/api/v1/workflows/{initial['run_id']}/synthetic-approval",
        headers={
            **WORKFLOW_HEADERS,
            "X-ProofStitch-Demo-Token": MODEL_DEMO_TOKEN,
        },
    )

    assert missing.status_code == 403
    assert authorized.status_code == 200
