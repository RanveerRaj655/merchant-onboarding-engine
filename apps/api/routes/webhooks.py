from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from apps.api.config import get_settings
from apps.api.db.models import VerificationRequest
from apps.api.db.session import get_db
from apps.api.services.verification import VerificationOrchestrator
from shared.enums import VerificationOutcome

router = APIRouter(prefix="/webhooks/verifications", tags=["webhooks"])


class CallbackBody(BaseModel):
    correlation_id: str
    provider_event_id: str
    outcome: VerificationOutcome
    details: dict[str, Any] = Field(default_factory=dict)
    scenario: str | None = None


class SimulateBody(BaseModel):
    correlation_id: str
    outcome: VerificationOutcome = VerificationOutcome.PASS
    provider_event_id: str | None = None
    mode: str = "normal"  # normal | duplicate | late


def _check_secret(x_webhook_secret: str | None) -> None:
    settings = get_settings()
    if x_webhook_secret != settings.webhook_secret:
        raise HTTPException(401, "Invalid webhook secret")


@router.post("/{provider}")
def verification_callback(
    provider: str,
    body: CallbackBody,
    db: Session = Depends(get_db),
    x_webhook_secret: str | None = Header(default=None),
):
    _check_secret(x_webhook_secret)
    orch = VerificationOrchestrator(db)
    result = orch.apply_callback(provider.upper(), body.model_dump())
    return {"ok": True, **result}


@router.post("/{provider}/simulate")
def simulate_callback(
    provider: str,
    body: SimulateBody,
    db: Session = Depends(get_db),
    x_webhook_secret: str | None = Header(default=None),
):
    """Demo-only: force pass/fail/late/duplicate callbacks."""
    _check_secret(x_webhook_secret)
    import uuid

    orch = VerificationOrchestrator(db)
    event_id = body.provider_event_id or f"sim-{uuid.uuid4()}"

    if body.mode == "late":
        req = (
            db.query(VerificationRequest)
            .filter(VerificationRequest.correlation_id == body.correlation_id)
            .first()
        )
        if req:
            from shared.enums import VerificationStatus

            req.status = VerificationStatus.TIMED_OUT.value
            db.flush()

    payload = {
        "correlation_id": body.correlation_id,
        "provider_event_id": event_id,
        "outcome": body.outcome.value,
        "details": {"simulated": True, "mode": body.mode},
    }
    result = orch.apply_callback(provider.upper(), payload)

    if body.mode == "duplicate":
        # Send same event again
        result2 = orch.apply_callback(provider.upper(), payload)
        return {"ok": True, "first": result, "second": result2}

    return {"ok": True, **result}
