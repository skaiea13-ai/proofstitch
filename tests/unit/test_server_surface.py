import asyncio
import hashlib
import time

from fastapi.testclient import TestClient

from app import fast_api_app

app = fast_api_app.app
client = TestClient(app)


def test_production_surface_excludes_development_and_state_routes() -> None:
    paths = set(app.openapi()["paths"])

    assert "/api/v1/agent/demo" in paths
    assert "/feedback" not in paths
    assert "/run_sse" not in paths
    assert not any(path.startswith("/a2a") for path in paths)
    assert not any(path.startswith("/apps/") for path in paths)
    assert not any("builder" in path for path in paths)
    assert not any(path.startswith("/eval") for path in paths)


def test_request_validation_does_not_reflect_rejected_input() -> None:
    sensitive_value = "SENTINEL-secret=do-not-reflect"
    response = client.post(
        "/api/v1/gates/evaluate",
        json={"run_id": sensitive_value},
    )

    assert response.status_code == 422
    assert response.json() == {"detail": "Request validation failed."}
    assert sensitive_value not in response.text


def test_request_body_limit_is_enforced() -> None:
    response = client.post(
        "/api/v1/gates/evaluate",
        content=b"x" * 131_073,
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json() == {"detail": "Request body too large."}


def test_cloud_gate_requires_capability_before_body_parsing(monkeypatch) -> None:
    token = "a" * 64
    now = int(time.time())
    monkeypatch.setenv("K_SERVICE", "proofstitch")
    monkeypatch.setenv(
        "PROOFSTITCH_MODEL_DEMO_TOKEN_SHA256",
        hashlib.sha256(token.encode("ascii")).hexdigest(),
    )
    monkeypatch.setenv("PROOFSTITCH_MODEL_DEMO_NOT_BEFORE", str(now - 1))
    monkeypatch.setenv("PROOFSTITCH_MODEL_DEMO_EXPIRES_AT", str(now + 599))
    packet = client.get("/api/v1/demo/0").json()["packet"]

    missing = client.post("/api/v1/gates/evaluate", json=packet)
    authorized = client.post(
        "/api/v1/gates/evaluate",
        json=packet,
        headers={"X-ProofStitch-Demo-Token": token},
    )

    assert missing.status_code == 403
    assert missing.json() == {
        "detail": "Valid agent demo capability is required."
    }
    assert authorized.status_code == 200


def test_cloud_post_rejects_invalid_capability_without_reading_body(
    monkeypatch,
) -> None:
    token = "a" * 64
    now = int(time.time())
    monkeypatch.setenv("K_SERVICE", "proofstitch")
    monkeypatch.setenv(
        "PROOFSTITCH_MODEL_DEMO_TOKEN_SHA256",
        hashlib.sha256(token.encode("ascii")).hexdigest(),
    )
    monkeypatch.setenv("PROOFSTITCH_MODEL_DEMO_NOT_BEFORE", str(now - 1))
    monkeypatch.setenv("PROOFSTITCH_MODEL_DEMO_EXPIRES_AT", str(now + 599))
    invalid_headers = [
        [],
        [(b"x-proofstitch-demo-token", b"b" * 64)],
        [(b"x-proofstitch-demo-token", b"\xff")],
        [
            (b"x-proofstitch-demo-token", token.encode("ascii")),
            (b"x-proofstitch-demo-token", token.encode("ascii")),
        ],
    ]

    async def exercise_rejection(headers):
        reached_downstream = False
        read_body = False
        sent: list[dict[str, object]] = []

        async def downstream(scope, receive, send) -> None:
            nonlocal reached_downstream
            reached_downstream = True
            await receive()

        async def receive() -> dict[str, object]:
            nonlocal read_body
            read_body = True
            raise AssertionError("rejected capability must not read the body")

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        middleware = fast_api_app.CloudPostCapabilityMiddleware(downstream)
        scope = {
            "type": "http",
            "method": "POST",
            "path": "/api/v1/gates/evaluate",
            "headers": headers,
        }

        await middleware(scope, receive, send)

        return reached_downstream, read_body, sent

    for headers in invalid_headers:
        reached_downstream, read_body, sent = asyncio.run(
            exercise_rejection(headers)
        )

        assert not reached_downstream
        assert not read_body
        assert next(
            message["status"]
            for message in sent
            if message["type"] == "http.response.start"
        ) == 403


def test_cloud_post_accepts_one_valid_capability(monkeypatch) -> None:
    token = "a" * 64
    now = int(time.time())
    monkeypatch.setenv("K_SERVICE", "proofstitch")
    monkeypatch.setenv(
        "PROOFSTITCH_MODEL_DEMO_TOKEN_SHA256",
        hashlib.sha256(token.encode("ascii")).hexdigest(),
    )
    monkeypatch.setenv("PROOFSTITCH_MODEL_DEMO_NOT_BEFORE", str(now - 1))
    monkeypatch.setenv("PROOFSTITCH_MODEL_DEMO_EXPIRES_AT", str(now + 599))
    reached_downstream = False
    read_body = False
    sent: list[dict[str, object]] = []

    async def downstream(scope, receive, send) -> None:
        nonlocal reached_downstream
        reached_downstream = True
        assert (await receive())["body"] == b"{}"
        await send({"type": "http.response.start", "status": 204, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    async def receive() -> dict[str, object]:
        nonlocal read_body
        read_body = True
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        sent.append(message)

    middleware = fast_api_app.CloudPostCapabilityMiddleware(downstream)
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/gates/evaluate",
        "headers": [
            (b"x-proofstitch-demo-token", token.encode("ascii")),
        ],
    }

    asyncio.run(middleware(scope, receive, send))

    assert reached_downstream
    assert read_body
    assert next(
        message["status"]
        for message in sent
        if message["type"] == "http.response.start"
    ) == 204


def test_local_entrypoint_binds_loopback(monkeypatch) -> None:
    observed: dict[str, object] = {}

    monkeypatch.setattr(
        "uvicorn.run",
        lambda _app, **kwargs: observed.update(kwargs),
    )
    fast_api_app.run_local()

    assert observed == {"host": "127.0.0.1", "port": 8000}


def test_cloud_surface_fails_closed_outside_bounded_window(monkeypatch) -> None:
    monkeypatch.setenv("K_SERVICE", "proofstitch")
    monkeypatch.delenv("PROOFSTITCH_MODEL_DEMO_NOT_BEFORE", raising=False)
    monkeypatch.delenv("PROOFSTITCH_MODEL_DEMO_EXPIRES_AT", raising=False)

    disabled = client.get("/healthz")

    now = int(time.time())
    monkeypatch.setenv("PROOFSTITCH_MODEL_DEMO_NOT_BEFORE", str(now - 1))
    monkeypatch.setenv("PROOFSTITCH_MODEL_DEMO_EXPIRES_AT", str(now + 599))
    active = client.get("/healthz")

    monkeypatch.setenv("PROOFSTITCH_MODEL_DEMO_NOT_BEFORE", str(now - 601))
    monkeypatch.setenv("PROOFSTITCH_MODEL_DEMO_EXPIRES_AT", str(now + 1))
    oversized = client.get("/healthz")

    assert disabled.status_code == 503
    assert disabled.json() == {"detail": "ProofStitch cloud demo is disabled."}
    assert active.status_code == 200
    assert oversized.status_code == 503
