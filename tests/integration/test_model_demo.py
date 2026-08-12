import hashlib
import os
import time

import pytest
from fastapi.testclient import TestClient

from app.fast_api_app import app

pytestmark = pytest.mark.skipif(
    os.getenv("RUN_LIVE_MODEL_TESTS") != "1",
    reason="live model tests require an explicit no-cost test opt-in",
)


def test_fixed_agent_demo_returns_bounded_text(monkeypatch) -> None:
    demo_token = os.environ["PROOFSTITCH_MODEL_DEMO_TOKEN"]
    now = int(time.time())
    monkeypatch.setenv(
        "PROOFSTITCH_MODEL_DEMO_TOKEN_SHA256",
        hashlib.sha256(demo_token.encode("ascii")).hexdigest(),
    )
    monkeypatch.setenv("PROOFSTITCH_MODEL_DEMO_NOT_BEFORE", str(now))
    monkeypatch.setenv("PROOFSTITCH_MODEL_DEMO_EXPIRES_AT", str(now + 10 * 60))
    response = TestClient(app).post(
        "/api/v1/agent/demo",
        headers={
            "X-ProofStitch-Demo-Intent": "fixed-synthetic-demo",
            "X-ProofStitch-Demo-Token": demo_token,
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["mode"] == "fixed_synthetic_demo"
    assert 1 <= len(payload["response"]) <= 4_000
    assert payload["external_action_executed"] is False
