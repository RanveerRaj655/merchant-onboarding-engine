from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.orm import Session

from apps.api.config import get_settings
from apps.api.db.models import (
    Application,
    AuditEvent,
    ExtractionResult,
    MerchantAccount,
    ReviewCase,
    VerificationCallback,
    VerificationRequest,
)
from apps.api.db.session import get_db
from apps.api.services.applications import ApplicationService
from apps.api.services.state_machine import StateMachineService
from shared.schemas import ReviewActionRequest

router = APIRouter(prefix="/api/v1/ops", tags=["ops"])


def require_operator(authorization: str | None = Header(default=None)) -> str:
    settings = get_settings()
    if not authorization:
        # Allow missing auth in local demo with warning via header absence
        return "operator:demo"
    token = authorization.removeprefix("Bearer ").strip()
    if token != settings.operator_token:
        raise HTTPException(401, "Invalid operator token")
    return "operator:demo"


@router.get("/applications")
def list_applications(
    status: str | None = None,
    db: Session = Depends(get_db),
    _actor: str = Depends(require_operator),
):
    q = db.query(Application).order_by(Application.updated_at.desc())
    if status:
        q = q.filter(Application.status == status)
    apps = q.limit(100).all()
    svc = ApplicationService(db)
    return [svc.to_public_dict(a) for a in apps]


@router.get("/applications/{application_id}")
def application_detail(
    application_id: UUID,
    db: Session = Depends(get_db),
    _actor: str = Depends(require_operator),
):
    app = db.get(Application, application_id)
    if not app:
        raise HTTPException(404, "Not found")
    svc = ApplicationService(db)
    detail = svc.to_public_dict(app)

    verifications = (
        db.query(VerificationRequest)
        .filter(VerificationRequest.application_id == application_id)
        .order_by(VerificationRequest.created_at.asc())
        .all()
    )
    callbacks = []
    for v in verifications:
        for cb in v.callbacks:
            callbacks.append(
                {
                    "id": str(cb.id),
                    "provider": cb.provider,
                    "provider_event_id": cb.provider_event_id,
                    "outcome": cb.outcome,
                    "processing_result": cb.processing_result,
                    "received_at": cb.received_at.isoformat(),
                }
            )

    extractions = (
        db.query(ExtractionResult)
        .filter(ExtractionResult.application_id == application_id)
        .all()
    )
    audit = (
        db.query(AuditEvent)
        .filter(AuditEvent.application_id == application_id)
        .order_by(AuditEvent.created_at.asc())
        .all()
    )
    reviews = (
        db.query(ReviewCase).filter(ReviewCase.application_id == application_id).all()
    )
    merchant = (
        db.query(MerchantAccount)
        .filter(MerchantAccount.application_id == application_id)
        .first()
    )

    detail["verifications"] = [
        {
            "id": str(v.id),
            "provider": v.provider,
            "status": v.status,
            "attempt": v.attempt,
            "correlation_id": v.correlation_id,
            "outcome": v.outcome,
            "timeout_at": v.timeout_at.isoformat() if v.timeout_at else None,
        }
        for v in verifications
    ]
    detail["callbacks"] = callbacks
    detail["extractions"] = [
        {
            "id": str(e.id),
            "fields": e.fields,
            "overall_confidence": e.overall_confidence,
            "conflicts": e.conflicts,
            "status": e.status,
            "extractor": e.extractor,
        }
        for e in extractions
    ]
    detail["audit"] = [
        {
            "id": str(e.id),
            "actor": e.actor,
            "action": e.action,
            "from_status": e.from_status,
            "to_status": e.to_status,
            "payload": e.payload,
            "created_at": e.created_at.isoformat(),
        }
        for e in audit
    ]
    detail["review_cases"] = [
        {
            "id": str(r.id),
            "reason_codes": r.reason_codes,
            "status": r.status,
            "priority": r.priority,
            "notes": r.notes,
            "resolution": r.resolution,
        }
        for r in reviews
    ]
    detail["merchant"] = (
        {
            "id": str(merchant.id),
            "capabilities": merchant.capabilities,
            "provisioning_status": merchant.provisioning_status,
        }
        if merchant
        else None
    )
    return detail


@router.post("/applications/{application_id}/reveal")
def reveal_fields(
    application_id: UUID,
    db: Session = Depends(get_db),
    actor: str = Depends(require_operator),
):
    app = db.get(Application, application_id)
    if not app:
        raise HTTPException(404, "Not found")
    StateMachineService(db).audit_only(
        application_id,
        actor=actor,
        action="FIELD_MASKED_VIEW",
        payload={"fields": ["pan", "gstin", "bank_account_number"]},
    )
    db.commit()
    svc = ApplicationService(db)
    return svc.to_public_dict(app, reveal=True)


@router.get("/review-cases")
def list_review_cases(
    status: str | None = Query(default=None),
    db: Session = Depends(get_db),
    _actor: str = Depends(require_operator),
):
    q = db.query(ReviewCase).order_by(ReviewCase.priority.desc(), ReviewCase.created_at.desc())
    if status:
        q = q.filter(ReviewCase.status == status)
    else:
        q = q.filter(ReviewCase.status.in_(["OPEN", "IN_REVIEW", "WAITING_MERCHANT"]))
    cases = q.limit(100).all()
    result = []
    for c in cases:
        app = db.get(Application, c.application_id)
        result.append(
            {
                "id": str(c.id),
                "application_id": str(c.application_id),
                "reference": app.reference if app else None,
                "legal_name": app.legal_name if app else None,
                "app_status": app.status if app else None,
                "reason_codes": c.reason_codes,
                "status": c.status,
                "priority": c.priority,
                "notes": c.notes,
                "version": app.version if app else None,
                "created_at": c.created_at.isoformat(),
            }
        )
    return result


@router.post("/review-cases/{case_id}/actions")
def review_action(
    case_id: UUID,
    body: ReviewActionRequest,
    db: Session = Depends(get_db),
    actor: str = Depends(require_operator),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    svc = ApplicationService(db)
    try:
        app = svc.operator_action(
            case_id,
            action=body.action,
            note=body.note,
            version=body.version,
            missing_info=body.missing_info,
            actor=actor,
        )
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    return svc.to_public_dict(app)


@router.get("/metrics")
def metrics(db: Session = Depends(get_db), _actor: str = Depends(require_operator)):
    total = db.query(Application).count()
    in_review = db.query(Application).filter(Application.status == "IN_REVIEW").count()
    approved = db.query(Application).filter(Application.status == "APPROVED").count()
    rejected = db.query(Application).filter(Application.status == "REJECTED").count()
    callbacks = db.query(VerificationCallback).count()
    duplicates = (
        db.query(VerificationCallback)
        .filter(VerificationCallback.processing_result == "DUPLICATE")
        .count()
    )
    timeouts = (
        db.query(VerificationRequest)
        .filter(VerificationRequest.status == "TIMED_OUT")
        .count()
    )
    sent = db.query(VerificationRequest).count()
    return {
        "applications_total": total,
        "in_review": in_review,
        "approved": approved,
        "rejected": rejected,
        "exception_rate": (in_review / total) if total else 0,
        "callback_duplicate_rate": (duplicates / callbacks) if callbacks else 0,
        "verification_timeout_rate": (timeouts / sent) if sent else 0,
    }
