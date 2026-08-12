# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
import time
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.api import _require_active_model_demo_capability
from app.api import router as proofstitch_router

_MAX_REQUEST_BODY_BYTES = 128 * 1024
_CLOUD_DEMO_NOT_BEFORE_ENV = "PROOFSTITCH_MODEL_DEMO_NOT_BEFORE"
_CLOUD_DEMO_EXPIRES_AT_ENV = "PROOFSTITCH_MODEL_DEMO_EXPIRES_AT"
_CLOUD_DEMO_MAX_WINDOW_SECONDS = 10 * 60
_CLOUD_DEMO_TOKEN_HEADER = b"x-proofstitch-demo-token"


class _BodyTooLarge(Exception):
    pass


class CloudDemoWindowMiddleware:
    """Fail closed outside the one-time Cloud Run demonstration window."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not os.getenv("K_SERVICE"):
            await self.app(scope, receive, send)
            return

        try:
            not_before = int(os.getenv(_CLOUD_DEMO_NOT_BEFORE_ENV, ""))
            expires_at = int(os.getenv(_CLOUD_DEMO_EXPIRES_AT_ENV, ""))
        except ValueError:
            not_before = 0
            expires_at = 0
        now = int(time.time())
        window_seconds = expires_at - not_before
        if not (
            0 < window_seconds <= _CLOUD_DEMO_MAX_WINDOW_SECONDS
            and not_before <= now < expires_at
        ):
            await JSONResponse(
                {"detail": "ProofStitch cloud demo is disabled."},
                status_code=503,
            )(scope, receive, send)
            return
        await self.app(scope, receive, send)


class CloudPostCapabilityMiddleware:
    """Authenticate Cloud POST requests before any request-body read."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or not os.getenv("K_SERVICE")
            or scope.get("method") != "POST"
        ):
            await self.app(scope, receive, send)
            return

        token_values = [
            value
            for name, value in scope.get("headers", [])
            if name.lower() == _CLOUD_DEMO_TOKEN_HEADER
        ]
        demo_token: str | None = None
        if len(token_values) == 1:
            try:
                demo_token = token_values[0].decode("ascii")
            except UnicodeDecodeError:
                pass

        try:
            _require_active_model_demo_capability(demo_token)
        except HTTPException as exc:
            await JSONResponse(
                {"detail": exc.detail},
                status_code=exc.status_code,
                headers=exc.headers,
            )(scope, receive, send)
            return

        await self.app(scope, receive, send)


class BodyLimitMiddleware:
    """Reject declared and streamed request bodies over a fixed limit."""

    def __init__(self, app: ASGIApp, *, max_body_bytes: int) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers", []))
        content_length = headers.get(b"content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError:
                await JSONResponse(
                    {"detail": "Invalid Content-Length."}, status_code=400
                )(scope, receive, send)
                return
            if declared_length > self.max_body_bytes:
                await self._reject(scope, receive, send)
                return

        received = 0

        async def limited_receive() -> Message:
            nonlocal received
            message = await receive()
            if message["type"] == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_body_bytes:
                    raise _BodyTooLarge
            return message

        try:
            await self.app(scope, limited_receive, send)
        except _BodyTooLarge:
            await self._reject(scope, receive, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        await JSONResponse(
            {"detail": "Request body too large."},
            status_code=413,
        )(scope, receive, send)


app = FastAPI(
    title="ProofStitch",
    description="Evidence-first release readiness agent",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.add_middleware(
    BodyLimitMiddleware,
    max_body_bytes=_MAX_REQUEST_BODY_BYTES,
)
app.add_middleware(CloudPostCapabilityMiddleware)
app.add_middleware(CloudDemoWindowMiddleware)
app.include_router(proofstitch_router)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(
    _request: Request,
    _exc: RequestValidationError,
) -> JSONResponse:
    """Return a stable validation error without reflecting rejected input."""

    return JSONResponse(
        {"detail": "Request validation failed."},
        status_code=422,
    )


@app.middleware("http")
async def security_headers(request: Request, call_next: Any) -> Any:
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-store"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


def run_local() -> None:
    """Run the development server on loopback only."""

    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)


if __name__ == "__main__":
    run_local()
