# Copyright 2026 ProofStitch contributors
# Licensed under the Apache License, Version 2.0 (the "License").

"""Product API and safe synthetic demo routes."""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import secrets
import time
from collections import deque
from pathlib import Path
from threading import Lock
from typing import Annotated, Literal

from fastapi import APIRouter, Header, HTTPException, Request, status
from fastapi import Path as ApiPath
from fastapi.responses import FileResponse, RedirectResponse
from google.adk.agents.run_config import RunConfig, StreamingMode
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types
from pydantic import BaseModel, ConfigDict, Field

from app.demo import build_demo_packet, trusted_demo_evidence_ids
from app.gate import GatePacket, GateReport, approval_scope, evaluate_packet
from app.workflow import (
    WorkflowCapacityError,
    execute_demo_workflow,
    record_demo_human_approval,
)

router = APIRouter()
_STATIC_DIR = Path(__file__).resolve().parent / "static"
_AGENT_DEMO_PROMPT = (
    "Call load_fixed_demo exactly once with no arguments. Then explain the verified "
    "result, the exact human-approval boundary, and that no external action was "
    "executed. Use at most five concise sentences and do not ask for user data."
)
_AGENT_CALL_LIMIT = 3
_AGENT_CALL_WINDOW_SECONDS = 10 * 60
_AGENT_DEMO_INTENT = "fixed-synthetic-demo"
_AGENT_DEMO_TOKEN_HASH_ENV = "PROOFSTITCH_MODEL_DEMO_TOKEN_SHA256"
_AGENT_DEMO_NOT_BEFORE_ENV = "PROOFSTITCH_MODEL_DEMO_NOT_BEFORE"
_AGENT_DEMO_EXPIRES_AT_ENV = "PROOFSTITCH_MODEL_DEMO_EXPIRES_AT"
_AGENT_DEMO_MAX_WINDOW_SECONDS = 10 * 60
_AGENT_CALLS: deque[float] = deque()
_AGENT_CALLS_LOCK = Lock()
_AGENT_SEMAPHORE = asyncio.Semaphore(1)
_WORKFLOW_CALL_LIMIT = 3
_WORKFLOW_CALL_WINDOW_SECONDS = 10 * 60
_WORKFLOW_DEMO_INTENT = "fixed-synthetic-workflow"
_WORKFLOW_CALLS: deque[float] = deque()
_WORKFLOW_CALLS_LOCK = Lock()


class AgentDemoResponse(BaseModel):
    """Bounded response from the fixed, non-interactive ADK demonstration."""

    model_config = ConfigDict(extra="forbid")

    response: str = Field(min_length=1, max_length=4_000)
    mode: Literal["fixed_synthetic_demo"] = "fixed_synthetic_demo"
    external_action_executed: Literal[False] = False


def _reserve_call(
    calls: deque[float],
    calls_lock: Lock,
    *,
    limit: int,
    window_seconds: int,
) -> bool:
    now = time.monotonic()
    with calls_lock:
        cutoff = now - window_seconds
        while calls and calls[0] < cutoff:
            calls.popleft()
        if len(calls) >= limit:
            return False
        calls.append(now)
        return True


def _reserve_agent_call() -> bool:
    return _reserve_call(
        _AGENT_CALLS,
        _AGENT_CALLS_LOCK,
        limit=_AGENT_CALL_LIMIT,
        window_seconds=_AGENT_CALL_WINDOW_SECONDS,
    )


def _reserve_workflow_call() -> bool:
    return _reserve_call(
        _WORKFLOW_CALLS,
        _WORKFLOW_CALLS_LOCK,
        limit=_WORKFLOW_CALL_LIMIT,
        window_seconds=_WORKFLOW_CALL_WINDOW_SECONDS,
    )


def _require_same_origin_intent(
    request: Request,
    supplied_intent: str | None,
    *,
    expected_intent: str,
) -> None:
    if request.headers.get("sec-fetch-site", "").lower() == "cross-site":
        raise HTTPException(
            status_code=403,
            detail="Cross-site state-changing requests are not allowed.",
        )
    if not secrets.compare_digest(supplied_intent or "", expected_intent):
        raise HTTPException(
            status_code=403,
            detail="Explicit request intent is required.",
        )


def _require_active_model_demo_capability(demo_token: str | None) -> None:
    expected_hash = os.getenv(_AGENT_DEMO_TOKEN_HASH_ENV, "")
    try:
        not_before = int(os.getenv(_AGENT_DEMO_NOT_BEFORE_ENV, ""))
        expires_at = int(os.getenv(_AGENT_DEMO_EXPIRES_AT_ENV, ""))
    except ValueError:
        not_before = 0
        expires_at = 0

    now = int(time.time())
    window_seconds = expires_at - not_before
    configured = bool(re.fullmatch(r"[0-9a-f]{64}", expected_hash))
    active = (
        configured
        and 0 < window_seconds <= _AGENT_DEMO_MAX_WINDOW_SECONDS
        and not_before <= now < expires_at
    )
    if not active:
        raise HTTPException(
            status_code=503,
            detail="Agent demo is disabled.",
        )

    if not re.fullmatch(r"[0-9a-f]{64}", demo_token or ""):
        raise HTTPException(
            status_code=403,
            detail="Valid agent demo capability is required.",
        )
    supplied_hash = hashlib.sha256((demo_token or "").encode("ascii")).hexdigest()
    if not secrets.compare_digest(supplied_hash, expected_hash):
        raise HTTPException(
            status_code=403,
            detail="Valid agent demo capability is required.",
        )


def _require_cloud_workflow_capability(demo_token: str | None) -> None:
    """Keep local workflows offline while protecting Cloud Run shared state."""

    if os.getenv("K_SERVICE"):
        _require_active_model_demo_capability(demo_token)


async def _run_fixed_agent_demo() -> str:
    from app.agent import app as adk_app

    session_service = InMemorySessionService()
    user_id = "proofstitch-fixed-demo"
    session_id = secrets.token_hex(16)
    await session_service.create_session(
        app_name=adk_app.name,
        user_id=user_id,
        session_id=session_id,
    )
    runner = Runner(app=adk_app, session_service=session_service)
    message = types.Content(
        role="user",
        parts=[types.Part.from_text(text=_AGENT_DEMO_PROMPT)],
    )
    response_text = ""
    try:
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=message,
            run_config=RunConfig(
                streaming_mode=StreamingMode.NONE,
                max_llm_calls=2,
            ),
        ):
            if not event.is_final_response() or not event.content:
                continue
            response_text = "".join(
                part.text or "" for part in event.content.parts or []
            ).strip()
    finally:
        await session_service.delete_session(
            app_name=adk_app.name,
            user_id=user_id,
            session_id=session_id,
        )
    if not response_text:
        raise RuntimeError("model returned no final text")
    return response_text[:4_000]


@router.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    """Send local viewers directly to the product demonstration."""

    return RedirectResponse(url="/proofstitch", status_code=307)


@router.get("/healthz")
def healthz() -> dict[str, str]:
    """Return a non-sensitive liveness response."""

    return {"status": "ok", "service": "proofstitch"}


@router.post("/api/v1/gates/evaluate", response_model=GateReport)
def evaluate_gate(packet: GatePacket) -> GateReport:
    """Evaluate caller input as untrusted evidence without executing its action."""

    return evaluate_packet(packet)


@router.get("/api/v1/demo/{stage}")
def demo_stage(stage: int) -> dict[str, object]:
    """Return a synthetic transition from blocked to human-action ready."""

    try:
        packet = build_demo_packet(stage)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {
        "packet": packet.model_dump(mode="json"),
        "report": evaluate_packet(
            packet,
            trusted_evidence_ids=trusted_demo_evidence_ids(packet),
            verified_approval_scope=(approval_scope(packet) if stage == 2 else None),
        ).model_dump(mode="json"),
    }


@router.post("/api/v1/agent/demo", response_model=AgentDemoResponse)
async def run_agent_demo(
    request: Request,
    demo_intent: Annotated[
        str | None,
        Header(alias="X-ProofStitch-Demo-Intent"),
    ] = None,
    demo_token: Annotated[
        str | None,
        Header(alias="X-ProofStitch-Demo-Token", max_length=128),
    ] = None,
) -> AgentDemoResponse:
    """Run one rate-limited fixed prompt through Gemini and Google ADK."""

    _require_same_origin_intent(
        request,
        demo_intent,
        expected_intent=_AGENT_DEMO_INTENT,
    )
    _require_active_model_demo_capability(demo_token)
    if not _reserve_agent_call():
        raise HTTPException(
            status_code=429,
            detail="Agent demo rate limit reached.",
        )
    try:
        async with _AGENT_SEMAPHORE:
            response_text = await asyncio.wait_for(
                _run_fixed_agent_demo(),
                timeout=45,
            )
    except TimeoutError as exc:
        raise HTTPException(
            status_code=504,
            detail="Agent demo timed out.",
        ) from exc
    except Exception as exc:
        raise HTTPException(
            status_code=503,
            detail="Agent demo is temporarily unavailable.",
        ) from exc
    return AgentDemoResponse(response=response_text)


@router.post("/api/v1/workflows/demo", status_code=status.HTTP_201_CREATED)
def run_demo_workflow(
    request: Request,
    workflow_intent: Annotated[
        str | None,
        Header(alias="X-ProofStitch-Workflow-Intent", max_length=64),
    ] = None,
    demo_token: Annotated[
        str | None,
        Header(alias="X-ProofStitch-Demo-Token", max_length=128),
    ] = None,
) -> dict[str, object]:
    """Execute the complete synthetic audit and persist its receipt in memory."""

    _require_same_origin_intent(
        request,
        workflow_intent,
        expected_intent=_WORKFLOW_DEMO_INTENT,
    )
    _require_cloud_workflow_capability(demo_token)
    if not _reserve_workflow_call():
        raise HTTPException(
            status_code=429,
            detail="Workflow demo rate limit reached.",
        )
    try:
        return execute_demo_workflow().model_dump(mode="json")
    except WorkflowCapacityError as exc:
        raise HTTPException(
            status_code=503,
            detail="Workflow demo capacity reached.",
        ) from exc


@router.post("/api/v1/workflows/{run_id}/synthetic-approval")
def approve_demo_workflow(
    request: Request,
    run_id: Annotated[
        str,
        ApiPath(pattern=r"^demo-audit-[0-9a-f]{32}$"),
    ],
    workflow_intent: Annotated[
        str | None,
        Header(alias="X-ProofStitch-Workflow-Intent", max_length=64),
    ] = None,
    demo_token: Annotated[
        str | None,
        Header(alias="X-ProofStitch-Demo-Token", max_length=128),
    ] = None,
) -> dict[str, object]:
    """Record an explicit, user-triggered approval in the synthetic demo only."""

    _require_same_origin_intent(
        request,
        workflow_intent,
        expected_intent=_WORKFLOW_DEMO_INTENT,
    )
    _require_cloud_workflow_capability(demo_token)
    try:
        workflow_run = record_demo_human_approval(run_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="workflow run not found") from exc
    return workflow_run.model_dump(mode="json")


@router.get("/proofstitch", include_in_schema=False)
def dashboard() -> FileResponse:
    """Serve the contest demo dashboard."""

    return FileResponse(_STATIC_DIR / "index.html")
