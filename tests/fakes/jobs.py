"""Committed delivery snapshots as the core's preflight hands them to a channel."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from wortly.jobs.models import Claim, DeliveryFacts, DeliverySnapshot, JobContext


NOW = datetime(2026, 10, 6, 9, 0, tzinfo=timezone.utc)


def envelope(kind: str = "access_approved", *, language: str = "ru", telegram_user_id: str = "4242",
             body: dict[str, Any] | None = None, schema: int = 1) -> dict[str, Any]:
    return {"schema": schema, "kind": kind, "ui_language": language,
            "recipient": {"telegram_user_id": telegram_user_id}, "body": {} if body is None else body}


def snapshot(kind: str = "access_approved", facts: dict[str, Any] | None = None) -> DeliverySnapshot:
    job_id, profile_id = uuid4(), uuid4()
    return DeliverySnapshot(
        claim=Claim(job_id, profile_id, "delivery-test", 1, NOW + timedelta(minutes=1)),
        attempt_id=uuid4(), attempt=1,
        context=JobContext(job_id, profile_id, f"{kind}:test", kind, 1, NOW),
        facts=DeliveryFacts.from_values(envelope(kind) if facts is None else facts, {"access": 1}),
        sending_at=NOW)
