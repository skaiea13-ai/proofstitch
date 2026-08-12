# Copyright 2026 ProofStitch contributors
# Licensed under the Apache License, Version 2.0 (the "License").

"""Non-sensitive synthetic packets used by the public product demo."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from app.gate import Evidence, GatePacket, Requirement


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def build_demo_packet(
    stage: int,
    *,
    now: datetime | None = None,
    run_id: str = "demo-release-run",
) -> GatePacket:
    """Build one of three deterministic demo states without external side effects."""

    if stage not in {0, 1, 2}:
        raise ValueError("demo stage must be 0, 1, or 2")
    observed_at = now or datetime.now(UTC)
    expected_ref = "8f31c20-demo-immutable-ref"
    requirements = [
        Requirement(
            id="tests",
            text="Automated tests pass on the immutable release ref.",
            accepted_kinds=["test_receipt"],
            expected_ref=expected_ref,
        ),
        Requirement(
            id="privacy",
            text="Public evidence contains no credentials or personal data.",
            accepted_kinds=["privacy_scan"],
            expected_ref=expected_ref,
        ),
        Requirement(
            id="video",
            text="The public demo video is within the duration limit.",
            accepted_kinds=["media_probe"],
            expected_ref=expected_ref,
        ),
    ]
    evidence = [
        Evidence(
            id="tests-proof",
            requirement_id="tests",
            kind="test_receipt",
            status="passed",
            observed_at=observed_at,
            sha256=_digest("tests-proof"),
            source_ref=expected_ref,
            summary="Automated tests passed on the immutable demo ref.",
        ),
        Evidence(
            id="privacy-proof",
            requirement_id="privacy",
            kind="privacy_scan",
            status="passed",
            observed_at=observed_at,
            sha256=_digest("privacy-proof"),
            source_ref=expected_ref,
            summary="Credential and personal-data patterns: zero findings.",
        ),
    ]
    if stage >= 1:
        evidence.append(
            Evidence(
                id="video-proof",
                requirement_id="video",
                kind="media_probe",
                status="passed",
                observed_at=observed_at,
                sha256=_digest("video-proof"),
                source_ref=expected_ref,
                summary="Public demo duration verified at 03:42.",
            )
        )
    packet = GatePacket(
        run_id=run_id,
        project_slug="proofstitch-demo",
        requested_action="submit",
        expected_ref=expected_ref,
        requirements=requirements,
        evidence=evidence,
    )
    return packet


def trusted_demo_evidence_ids(packet: GatePacket) -> frozenset[str]:
    """Return evidence IDs from the server-built synthetic demo only."""

    if (
        packet.project_slug != "proofstitch-demo"
        or packet.expected_ref != "8f31c20-demo-immutable-ref"
    ):
        return frozenset()
    return frozenset(item.id for item in packet.evidence)
