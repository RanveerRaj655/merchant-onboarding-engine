from pathlib import Path
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from apps.api.config import get_settings
from apps.api.db.models import (
    Application,
    AuditEvent,
    Document,
    ExtractionResult,
    MerchantAccount,
    ReviewCase,
    VerificationCallback,
    VerificationRequest,
)
from apps.api.db.session import get_db
from apps.api.services.applications import ApplicationService

TEMPLATES_DIR = Path(__file__).resolve().parents[2] / "operator_web" / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

router = APIRouter(tags=["ui"])


STATUS_TABS = [
    ("review", "Needs review", ["IN_REVIEW"]),
    ("missing", "Missing info", ["MISSING_INFO"]),
    ("verification", "In verification", [
        "SUBMITTED",
        "EXTRACTING",
        "PENDING_VERIFICATION",
        "AWAITING_CALLBACKS",
        "VERIFICATION_PARTIAL",
        "CHECKS_PASSED",
        "PROVISIONING",
    ]),
    ("approved", "Approved", ["APPROVED"]),
    ("rejected", "Rejected", ["REJECTED", "FAILED"]),
]


def _metrics(db: Session) -> dict:
    total = db.query(Application).count()
    in_review = db.query(Application).filter(Application.status == "IN_REVIEW").count()
    missing_info = db.query(Application).filter(Application.status == "MISSING_INFO").count()
    approved = db.query(Application).filter(Application.status == "APPROVED").count()
    rejected = db.query(Application).filter(Application.status.in_(["REJECTED", "FAILED"])).count()
    in_verification = (
        db.query(Application)
        .filter(Application.status.in_([
            "SUBMITTED", "EXTRACTING", "PENDING_VERIFICATION",
            "AWAITING_CALLBACKS", "VERIFICATION_PARTIAL", "CHECKS_PASSED", "PROVISIONING"
        ]))
        .count()
    )
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
        "missing_info": missing_info,
        "in_verification": in_verification,
        "approved": approved,
        "rejected": rejected,
        "exception_rate": (in_review / total) if total else 0,
        "callback_duplicate_rate": (duplicates / callbacks) if callbacks else 0,
        "verification_timeout_rate": (timeouts / sent) if sent else 0,
        "tab_counts": {
            "review": in_review,
            "missing": missing_info,
            "verification": in_verification,
            "approved": approved,
            "rejected": rejected,
        },
    }


def _review_cases(db: Session) -> list[dict]:
    cases = (
        db.query(ReviewCase)
        .filter(ReviewCase.status.in_(["OPEN", "IN_REVIEW", "WAITING_MERCHANT"]))
        .order_by(ReviewCase.priority.desc(), ReviewCase.created_at.desc())
        .limit(100)
        .all()
    )
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


def _application_detail(db: Session, application_id: UUID) -> dict:
    app = db.get(Application, application_id)
    if not app:
        return {}
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

    # Identify latest document IDs per type for active highlighting
    latest_doc_ids = set()
    docs = (
        db.query(Document)
        .filter(Document.application_id == application_id)
        .order_by(Document.created_at.desc())
        .all()
    )
    seen_types = set()
    for d in docs:
        if d.type not in seen_types:
            seen_types.add(d.type)
            latest_doc_ids.add(d.id)

    extractions = (
        db.query(ExtractionResult)
        .filter(ExtractionResult.application_id == application_id)
        .order_by(ExtractionResult.created_at.desc())
        .all()
    )
    audit = (
        db.query(AuditEvent)
        .filter(AuditEvent.application_id == application_id)
        .order_by(AuditEvent.created_at.desc())
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

    # Document details with preview links
    detail["documents"] = [
        {
            "id": str(d.id),
            "type": d.type,
            "filename": Path(d.storage_key).name if d.storage_key else f"{d.type}.png",
            "has_file": bool(d.storage_key),
            "preview_url": f"/ops/documents/{d.id}/file" if d.storage_key else None,
            "is_latest": d.id in latest_doc_ids,
            "created_at": d.created_at.isoformat() if d.created_at else None,
        }
        for d in docs
    ]

    detail["extractions"] = [
        {
            "id": str(e.id),
            "document_id": str(e.document_id) if e.document_id else None,
            "preview_url": f"/ops/documents/{e.document_id}/file" if e.document and e.document.storage_key else None,
            "doc_type": e.document.type if e.document else "DOCUMENT",
            "is_latest": e.document_id in latest_doc_ids,
            "fields": {k: v for k, v in (e.fields or {}).items() if not k.startswith("_")},
            "quality": (e.fields or {}).get("_quality", {}),
            "raw_fields": e.fields,
            "overall_confidence": e.overall_confidence,
            "conflicts": e.conflicts,
            "status": e.status,
            "extractor": e.extractor,
            "created_at": e.created_at.isoformat() if e.created_at else None,
        }
        for e in extractions
    ]

    # Calculate 3-Way Reconciliation matrix (Form vs Document vs Registry)
    latest_extracted_pan = None
    latest_extracted_gst = None
    latest_extracted_bank = None
    latest_extracted_ifsc = None

    for e in extractions:
        if e.document_id in latest_doc_ids:
            fields = e.fields or {}
            if "pan" in fields and not latest_extracted_pan:
                v = fields["pan"]
                latest_extracted_pan = v.get("value") if isinstance(v, dict) else v
            if "gstin" in fields and not latest_extracted_gst:
                v = fields["gstin"]
                latest_extracted_gst = v.get("value") if isinstance(v, dict) else v
            if "bank_account_number" in fields and not latest_extracted_bank:
                v = fields["bank_account_number"]
                latest_extracted_bank = v.get("value") if isinstance(v, dict) else v
            if "ifsc" in fields and not latest_extracted_ifsc:
                v = fields["ifsc"]
                latest_extracted_ifsc = v.get("value") if isinstance(v, dict) else v

    verif_by_provider = {}
    for v in verifications:
        if v.status != "SUPERSEDED":
            verif_by_provider[v.provider] = v

    def _eval_row(form_val, doc_val, verif_req):
        f = (form_val or "").strip().upper()
        d = (doc_val or "").strip().upper()
        has_f = bool(f)
        has_d = bool(d)
        is_pass = verif_req and (verif_req.outcome == "PASS" or verif_req.status in ("PASS", "SUCCEEDED"))
        is_fail = verif_req and (verif_req.outcome == "FAIL" or verif_req.status in ("FAIL", "FAILED"))

        if has_f and has_d and f == d and is_pass:
            return "MATCH", "Verified & Consistent"
        if has_f and has_d and f != d:
            return "MISMATCH", f"Form ({f[-4:]}) differs from Doc ({d[-4:]})"
        if is_fail:
            return "FAILED", "Registry rejected value"
        if is_pass:
            return "MATCH", "Registry verified"
        if not has_d:
            return "UNEXTRACTED", "Awaiting clear document"
        return "PENDING", "Verification pending"

    v_pan = verif_by_provider.get("PAN")
    v_gst = verif_by_provider.get("GST")
    v_bank = verif_by_provider.get("BANK")

    pan_status, pan_note = _eval_row(app.pan_raw, latest_extracted_pan, v_pan)
    gst_status, gst_note = _eval_row(app.gstin_raw, latest_extracted_gst, v_gst)
    bank_status, bank_note = _eval_row(app.bank_account_raw, latest_extracted_bank, v_bank)

    detail["reconciliation"] = [
        {
            "field": "PAN Number",
            "code": "PAN",
            "form_val": app.pan_raw or "—",
            "form_masked": app.pan_masked or "—",
            "doc_val": latest_extracted_pan or "Not extracted",
            "doc_masked": (latest_extracted_pan[:2] + "*****" + latest_extracted_pan[-1:]) if latest_extracted_pan and len(latest_extracted_pan) == 10 else (latest_extracted_pan or "—"),
            "provider": "PAN (NSDL / ITD)",
            "verif_status": v_pan.status if v_pan else "NOT_STARTED",
            "verif_outcome": v_pan.outcome if v_pan else None,
            "status": pan_status,
            "note": pan_note,
        },
        {
            "field": "GSTIN Identifier",
            "code": "GSTIN",
            "form_val": app.gstin_raw or "—",
            "form_masked": app.gstin_masked or "—",
            "doc_val": latest_extracted_gst or "Not extracted",
            "doc_masked": (latest_extracted_gst[:2] + "***********" + latest_extracted_gst[-2:]) if latest_extracted_gst and len(latest_extracted_gst) == 15 else (latest_extracted_gst or "—"),
            "provider": "GST (Govt Portal)",
            "verif_status": v_gst.status if v_gst else "NOT_STARTED",
            "verif_outcome": v_gst.outcome if v_gst else None,
            "status": gst_status,
            "note": gst_note,
        },
        {
            "field": "Bank Account",
            "code": "BANK",
            "form_val": app.bank_account_raw or "—",
            "form_masked": app.bank_account_masked or "—",
            "doc_val": latest_extracted_bank or "Not extracted",
            "doc_masked": ("**********" + latest_extracted_bank[-4:]) if latest_extracted_bank and len(latest_extracted_bank) >= 4 else (latest_extracted_bank or "—"),
            "provider": "BANK (Penny Drop)",
            "verif_status": v_bank.status if v_bank else "NOT_STARTED",
            "verif_outcome": v_bank.outcome if v_bank else None,
            "status": bank_status,
            "note": bank_note,
        },
        {
            "field": "IFSC Code",
            "code": "IFSC",
            "form_val": app.ifsc or "—",
            "form_masked": app.ifsc or "—",
            "doc_val": latest_extracted_ifsc or (app.ifsc or "—"),
            "doc_masked": app.ifsc or "—",
            "provider": "BANK (Routing)",
            "verif_status": v_bank.status if v_bank else "NOT_STARTED",
            "verif_outcome": v_bank.outcome if v_bank else None,
            "status": "MATCH" if (app.ifsc and v_bank and v_bank.outcome == "PASS") else ("PENDING" if app.ifsc else "MISSING"),
            "note": "Bank branch routing confirmed" if (v_bank and v_bank.outcome == "PASS") else "Awaiting bank check",
        },
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


@router.get("/", response_class=HTMLResponse)
@router.get("/ops", response_class=HTMLResponse)
def queue_page(request: Request, tab: str = "review", db: Session = Depends(get_db)):
    tab_map = {t[0]: t[2] for t in STATUS_TABS}
    statuses = tab_map.get(tab, tab_map["review"])
    apps = (
        db.query(Application)
        .filter(Application.status.in_(statuses))
        .order_by(Application.updated_at.desc())
        .limit(50)
        .all()
    )
    review_cases = _review_cases(db) if tab == "review" else []
    m = _metrics(db)
    resp = templates.TemplateResponse(
        request,
        "queue.html",
        {
            "tab": tab,
            "tabs": STATUS_TABS,
            "apps": apps,
            "review_cases": review_cases,
            "metrics": m,
        },
    )
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


def _detail_context(
    db: Session,
    application_id: UUID,
    *,
    reupload_error: str | None = None,
    hitl_error: str | None = None,
    saved: bool = False,
) -> dict:
    detail = _application_detail(db, application_id)
    app_row = db.get(Application, application_id)
    open_case = (
        db.query(ReviewCase)
        .filter(
            ReviewCase.application_id == application_id,
            ReviewCase.status.in_(["OPEN", "IN_REVIEW", "WAITING_MERCHANT"]),
        )
        .order_by(ReviewCase.created_at.desc())
        .first()
    )
    hitl_defaults = {}
    if app_row and app_row.status in ("MISSING_INFO", "IN_REVIEW", "EXTRACTION_FAILED"):
        suggested_pan = "AABCA1234A"
        suggested_gstin = "27AABCA1234A1Z5"
        reasons = list((open_case.reason_codes if open_case else None) or [])
        prefill_reason = ""
        pan_val = app_row.pan_raw or ""
        gstin_val = app_row.gstin_raw or ""
        note_default = ""
        hitl_defaults = {
            "legal_name": app_row.legal_name,
            "pan": pan_val,
            "gstin": gstin_val,
            "bank_account_number": app_row.bank_account_raw or "",
            "ifsc": app_row.ifsc or "",
            "suggested_pan": suggested_pan,
            "suggested_gstin": suggested_gstin,
            "prefill_reason": prefill_reason,
            "note_default": note_default,
        }
    active_states = {"SUBMITTED", "EXTRACTING", "PENDING_VERIFICATION",
                     "AWAITING_CALLBACKS", "VERIFICATION_PARTIAL", "PROVISIONING"}
    poll_live = detail.get("status") in active_states
    return {
        "app": detail,
        "open_case": open_case,
        "reupload_error": reupload_error,
        "hitl_error": hitl_error,
        "hitl_defaults": hitl_defaults,
        "saved": saved,
        "poll_live": poll_live,
    }


@router.get("/ops/applications/{application_id}", response_class=HTMLResponse)
def detail_page(
    request: Request,
    application_id: UUID,
    db: Session = Depends(get_db),
    reupload_error: str | None = None,
    hitl_error: str | None = None,
    saved: int = 0,
):
    ctx = _detail_context(
        db,
        application_id,
        reupload_error=reupload_error,
        hitl_error=hitl_error,
        saved=bool(saved),
    )
    resp = templates.TemplateResponse(request, "detail.html", ctx)
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


@router.get("/ops/applications/{application_id}/live", response_class=HTMLResponse)
def detail_live_partial(
    request: Request, application_id: UUID, db: Session = Depends(get_db)
):
    """HTMX poll target — refreshes status, profile fields, verifications, merchant."""
    ctx = _detail_context(db, application_id)
    resp = templates.TemplateResponse(request, "partials/detail_live.html", ctx)
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/ops/documents/{document_id}/file")
def get_document_file(document_id: UUID, db: Session = Depends(get_db)):
    """Serve uploaded or fixture document image for visual inspector."""
    doc = db.get(Document, document_id)
    if not doc or not doc.storage_key:
        raise HTTPException(404, "Document has no stored file")
    settings = get_settings()
    path = Path(settings.doc_storage_path) / doc.storage_key
    if path.exists():
        return FileResponse(path, media_type=doc.content_type or "image/png")
    # Fallback to fixtures directory
    fixture_path = Path("fixtures") / doc.storage_key
    if fixture_path.exists():
        return FileResponse(fixture_path, media_type=doc.content_type or "image/png")
    # Check if doc.storage_key is an absolute or direct relative path
    direct_path = Path(doc.storage_key)
    if direct_path.exists():
        return FileResponse(direct_path, media_type=doc.content_type or "image/png")
    raise HTTPException(404, "Document file not found on disk")


@router.post("/ops/applications/{application_id}/hitl-correct")
async def hitl_correct_form(
    request: Request,
    application_id: UUID,
    note: str = Form(""),
    mode: str = Form("auto"),
    legal_name: str = Form(""),
    pan: str = Form(""),
    gstin: str = Form(""),
    bank_account_number: str = Form(""),
    ifsc: str = Form(""),
    pan_file: UploadFile | None = File(None),
    gst_file: UploadFile | None = File(None),
    cheque_file: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    from shared.enums import DocumentType

    docs: list[tuple] = []
    for dtype, uf in (
        (DocumentType.PAN_CARD, pan_file),
        (DocumentType.GST_CERT, gst_file),
        (DocumentType.CANCELLED_CHEQUE, cheque_file),
    ):
        if uf is not None and uf.filename:
            data = await uf.read()
            if data:
                docs.append(
                    (
                        dtype,
                        uf.filename or f"{dtype.value}.bin",
                        uf.content_type or "application/octet-stream",
                        data,
                    )
                )

    svc = ApplicationService(db)
    try:
        svc.hitl_correct(
            application_id,
            pan=pan or None,
            gstin=gstin or None,
            bank_account_number=bank_account_number or None,
            ifsc=ifsc or None,
            legal_name=legal_name or None,
            note=note,
            docs=docs,
            actor="operator:demo",
            mode=mode or "auto",
        )
    except Exception as e:
        db.rollback()
        return detail_page(
            request, application_id, db, hitl_error=str(e)
        )

    return RedirectResponse(
        url=f"/ops/applications/{application_id}?saved=1",
        status_code=303,
        headers={"Cache-Control": "no-store"},
    )


@router.post("/ops/applications/{application_id}/reupload")
async def reupload_documents(
    request: Request,
    application_id: UUID,
    pan_file: UploadFile | None = File(None),
    gst_file: UploadFile | None = File(None),
    cheque_file: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    from shared.enums import ApplicationStatus, DocumentType

    app = db.get(Application, application_id)
    if not app:
        return HTMLResponse("Application not found", status_code=404)
    if app.status != ApplicationStatus.MISSING_INFO.value:
        return RedirectResponse(
            url=f"/ops/applications/{application_id}", status_code=303
        )

    uploads: list[tuple[DocumentType, UploadFile, bytes]] = []
    for dtype, uf in (
        (DocumentType.PAN_CARD, pan_file),
        (DocumentType.GST_CERT, gst_file),
        (DocumentType.CANCELLED_CHEQUE, cheque_file),
    ):
        if uf is not None and uf.filename:
            data = await uf.read()
            if data:
                uploads.append((dtype, uf, data))

    if not uploads:
        return detail_page(
            request,
            application_id,
            db,
            reupload_error="Select at least one document file to re-upload.",
        )

    svc = ApplicationService(db)
    try:
        for dtype, uf, data in uploads:
            svc.add_upload(
                application_id,
                doc_type=dtype,
                filename=uf.filename or f"{dtype.value.lower()}.bin",
                content_type=uf.content_type or "application/octet-stream",
                data=data,
            )
        app.missing_info = []
        db.commit()
        svc.kickoff(application_id)
    except Exception as e:
        db.rollback()
        return detail_page(
            request, application_id, db, reupload_error=str(e)
        )

    return RedirectResponse(
        url=f"/ops/applications/{application_id}", status_code=303
    )


@router.post("/ops/applications/{application_id}/actions")
def ui_action(
    request: Request,
    application_id: UUID,
    action: str = Form(...),
    note: str = Form(...),
    version: int = Form(...),
    case_id: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    svc = ApplicationService(db)
    try:
        if not case_id:
            case = ReviewCase(
                application_id=application_id,
                reason_codes=["OPERATOR_ESCALATION"],
                status="OPEN",
                priority=60,
            )
            db.add(case)
            db.flush()
            case_id = str(case.id)

        svc.operator_action(
            UUID(case_id),
            action=action,
            note=note,
            version=version,
            actor="operator:demo",
        )
    except Exception as e:
        db.rollback()
        return detail_page(
            request, application_id, db, hitl_error=str(e)
        )

    return RedirectResponse(
        url=f"/ops/applications/{application_id}", status_code=303
    )


@router.get("/ops/submit", response_class=HTMLResponse)
def submit_page(request: Request, error: str | None = None):
    from shared.fixtures import load_registry

    resp = templates.TemplateResponse(
        request,
        "submit.html",
        {"merchants": load_registry(), "error": error},
    )
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    return resp


@router.post("/ops/submit-new")
async def submit_new_merchant(
    request: Request,
    legal_name: str = Form(...),
    pan: str = Form(""),
    gstin: str = Form(""),
    contact_email: str = Form(...),
    trade_name: str = Form(""),
    bank_account_number: str = Form(""),
    ifsc: str = Form(""),
    contact_phone: str = Form(""),
    business_category: str = Form(""),
    scenario: str = Form(""),
    pan_file: UploadFile = File(...),
    gst_file: UploadFile = File(...),
    cheque_file: UploadFile | None = File(None),
    db: Session = Depends(get_db),
):
    from shared.enums import DocumentType
    from shared.schemas import ApplicationCreate, ConfigChecks

    pan_bytes = await pan_file.read()
    gst_bytes = await gst_file.read()
    if not pan_bytes or not gst_bytes:
        return submit_page(request, error="PAN and GST document files are required.")

    cheque_bytes = b""
    if cheque_file is not None and cheque_file.filename:
        cheque_bytes = await cheque_file.read()

    cfg = ConfigChecks(scenario=scenario or None)
    if scenario == "timeout":
        cfg.verification_timeout_ms = 8000

    body = ApplicationCreate(
        legal_name=legal_name.strip(),
        trade_name=trade_name.strip() or None,
        pan=(pan or "").strip().upper(),
        gstin=(gstin or "").strip().upper(),
        bank_account_number=bank_account_number.strip() or None,
        ifsc=ifsc.strip().upper() or None,
        contact_email=contact_email.strip(),
        contact_phone=contact_phone.strip() or None,
        business_category=business_category.strip() or None,
        config_checks=cfg,
    )

    svc = ApplicationService(db)
    try:
        app = svc.create(body)
        svc.add_upload(
            app.id,
            doc_type=DocumentType.PAN_CARD,
            filename=pan_file.filename or "pan.png",
            content_type=pan_file.content_type or "application/octet-stream",
            data=pan_bytes,
        )
        svc.add_upload(
            app.id,
            doc_type=DocumentType.GST_CERT,
            filename=gst_file.filename or "gst.png",
            content_type=gst_file.content_type or "application/octet-stream",
            data=gst_bytes,
        )
        if cheque_bytes:
            svc.add_upload(
                app.id,
                doc_type=DocumentType.CANCELLED_CHEQUE,
                filename=cheque_file.filename if cheque_file else "cheque.png",
                content_type=(
                    cheque_file.content_type
                    if cheque_file and cheque_file.content_type
                    else "application/octet-stream"
                ),
                data=cheque_bytes,
            )
        svc.kickoff(app.id)
    except Exception as e:
        return submit_page(request, error=str(e))

    return RedirectResponse(url=f"/ops/applications/{app.id}", status_code=303)


@router.post("/ops/submit-fixture")
def submit_fixture_form(
    merchant_id: str = Form(...),
    db: Session = Depends(get_db),
):
    svc = ApplicationService(db)
    try:
        app = svc.create_from_fixture(merchant_id)
    except ValueError as e:
        return HTMLResponse(f"<h1>Error</h1><p>{e}</p>", status_code=400)
    return RedirectResponse(url=f"/ops/applications/{app.id}", status_code=303)
