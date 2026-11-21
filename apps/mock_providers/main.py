"""Simulated PAN / GST / BANK verification against synthetic fixture registry.

Looks up PAN/GSTIN/bank in fixtures/merchants.json (not the public internet).
"""

import asyncio
import hashlib
import logging
import uuid
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field

from apps.api.config import get_settings
from shared.fixtures import lookup_bank, lookup_gstin, lookup_pan

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mock_providers")

app = FastAPI(title="AMOP Mock Verification Providers", version="0.2.0")
settings = get_settings()
_seen_idempotency: set[str] = set()


class VerifyRequest(BaseModel):
    correlation_id: str
    idempotency_key: str
    callback_url: str
    payload: dict[str, Any] = Field(default_factory=dict)


def _decide_outcome(provider: str, payload: dict[str, Any]) -> tuple[str, float, dict[str, Any]]:
    """Return (outcome, delay_seconds, details). Registry is source of truth."""
    scenario = (payload or {}).get("scenario")
    provider = provider.upper()
    details: dict[str, Any] = {"provider": provider, "mock": True, "source": "fixture_registry"}

    if scenario == "timeout":
        return "PASS", 120.0, {**details, "note": "timeout scenario — delayed callback"}

    # Scenario overrides still supported for demos without registry hits
    if scenario == "gst_fail" and provider == "GST":
        return "FAIL", 5.0, {**details, "reason": "scenario_gst_fail"}
    if scenario == "pan_fail" and provider == "PAN":
        return "FAIL", 3.0, {**details, "reason": "scenario_pan_fail"}
    if scenario == "bank_fail" and provider == "BANK":
        return "FAIL", 4.0, {**details, "reason": "scenario_bank_fail"}

    delays = {"PAN": 2.0, "GST": 5.0, "BANK": 3.0, "RISK": 4.0}
    delay = delays.get(provider, 3.0)

    if provider == "PAN":
        pan = (payload or {}).get("pan") or ""
        row = lookup_pan(pan)
        if row is None:
            return "FAIL", delay, {**details, "reason": "PAN_NOT_IN_REGISTRY"}
        details["merchant_id"] = row["id"]
        details["legal_name"] = row["legal_name"]
        if row.get("pan_status") != "ACTIVE":
            return "FAIL", delay, {**details, "reason": "PAN_INACTIVE", "pan_status": row["pan_status"]}
        return "PASS", delay, details

    if provider == "GST":
        gstin = (payload or {}).get("gstin") or ""
        row = lookup_gstin(gstin)
        if row is None:
            return "FAIL", delay, {**details, "reason": "GSTIN_NOT_IN_REGISTRY"}
        details["merchant_id"] = row["id"]
        details["legal_name"] = row["legal_name"]
        if row.get("gst_status") != "ACTIVE":
            return "FAIL", delay, {**details, "reason": "GST_NOT_ACTIVE", "gst_status": row["gst_status"]}
        return "PASS", delay, details

    if provider == "BANK":
        account = (payload or {}).get("account") or ""
        ifsc = (payload or {}).get("ifsc") or ""
        row = lookup_bank(account, ifsc)
        if row is None:
            return "FAIL", delay, {**details, "reason": "BANK_NOT_IN_REGISTRY"}
        details["merchant_id"] = row["id"]
        if row.get("bank_status") != "ACTIVE":
            return "FAIL", delay, {**details, "reason": "BANK_INACTIVE"}
        return "PASS", delay, details

    return "PASS", delay, details


async def _send_callback(
    callback_url: str,
    provider: str,
    correlation_id: str,
    outcome: str,
    delay: float,
    details: dict[str, Any],
):
    await asyncio.sleep(delay)
    event_id = f"{provider.lower()}-{uuid.uuid4()}"
    body = {
        "correlation_id": correlation_id,
        "provider_event_id": event_id,
        "outcome": outcome,
        "details": details,
    }
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                callback_url,
                json=body,
                headers={"X-Webhook-Secret": settings.webhook_secret},
            )
            logger.info(
                "callback %s %s → %s (%s)",
                provider,
                correlation_id,
                resp.status_code,
                outcome,
            )
    except Exception:
        logger.exception("callback failed for %s %s", provider, correlation_id)


@app.get("/health")
def health():
    return {"status": "ok", "service": "mock_providers"}


@app.get("/registry")
def registry_summary():
    from shared.fixtures import load_registry

    rows = load_registry()
    return {
        "count": len(rows),
        "merchants": [
            {
                "id": m["id"],
                "legal_name": m["legal_name"],
                "pan": m["pan"],
                "gstin": m["gstin"],
                "pan_status": m.get("pan_status"),
                "gst_status": m.get("gst_status"),
                "scenario": m.get("scenario"),
            }
            for m in rows
        ],
    }


@app.post("/verify/{provider}")
async def verify(
    provider: str,
    body: VerifyRequest,
    x_webhook_secret: str | None = Header(default=None),
):
    if x_webhook_secret and x_webhook_secret != settings.webhook_secret:
        raise HTTPException(401, "bad secret")

    if body.idempotency_key in _seen_idempotency:
        raise HTTPException(409, "duplicate idempotency key")
    _seen_idempotency.add(body.idempotency_key)

    outcome, delay, details = _decide_outcome(provider, body.payload)
    details["fingerprint"] = hashlib.sha256(body.idempotency_key.encode()).hexdigest()[:12]
    asyncio.create_task(
        _send_callback(
            body.callback_url,
            provider.upper(),
            body.correlation_id,
            outcome,
            delay,
            details,
        )
    )
    return {
        "accepted": True,
        "correlation_id": body.correlation_id,
        "estimated_callback_seconds": delay,
        "preview_outcome": outcome,
    }
