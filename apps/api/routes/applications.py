from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from sqlalchemy.orm import Session

from apps.api.db.models import Application, AuditEvent, Document, ExtractionResult
from apps.api.db.session import get_db
from apps.api.services.applications import ApplicationService
from shared.enums import DocumentType
from shared.schemas import ApplicationCreate, ApplicationUpdate, StructuredDocumentPayload

router = APIRouter(prefix="/api/v1/applications", tags=["applications"])


@router.post("")
def create_application(body: ApplicationCreate, db: Session = Depends(get_db)):
    svc = ApplicationService(db)
    app = svc.create(body)
    return svc.to_public_dict(app)


@router.get("/{application_id}")
def get_application(application_id: UUID, db: Session = Depends(get_db)):
    app = db.get(Application, application_id)
    if not app:
        raise HTTPException(404, "Application not found")
    svc = ApplicationService(db)
    return svc.to_public_dict(app)


@router.patch("/{application_id}")
def patch_application(
    application_id: UUID,
    body: ApplicationUpdate,
    db: Session = Depends(get_db),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    svc = ApplicationService(db)
    try:
        app = svc.update(application_id, body)
    except ValueError as e:
        raise HTTPException(409, str(e)) from e
    return svc.to_public_dict(app)


@router.post("/{application_id}/documents")
async def upload_document(
    application_id: UUID,
    document_type: str = Form(...),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    try:
        dtype = DocumentType(document_type)
    except ValueError as e:
        raise HTTPException(400, f"Invalid document_type: {document_type}") from e
    data = await file.read()
    svc = ApplicationService(db)
    try:
        doc = svc.add_upload(
            application_id,
            doc_type=dtype,
            filename=file.filename or "upload.bin",
            content_type=file.content_type or "application/octet-stream",
            data=data,
        )
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    return {
        "id": str(doc.id),
        "type": doc.type,
        "source": doc.source,
        "storage_key": doc.storage_key,
        "byte_size": doc.byte_size,
    }


@router.post("/{application_id}/documents/structured")
def structured_document(
    application_id: UUID,
    body: StructuredDocumentPayload,
    db: Session = Depends(get_db),
):
    svc = ApplicationService(db)
    try:
        doc = svc.add_structured_document(application_id, body)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    return {
        "id": str(doc.id),
        "type": doc.type,
        "source": doc.source,
    }


@router.post("/{application_id}/process")
def kickoff_processing(application_id: UUID, db: Session = Depends(get_db)):
    """Start extraction → verification pipeline after documents are attached."""
    svc = ApplicationService(db)
    try:
        app = svc.kickoff(application_id)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    return svc.to_public_dict(app)


@router.get("/{application_id}/extractions")
def list_extractions(application_id: UUID, db: Session = Depends(get_db)):
    rows = (
        db.query(ExtractionResult)
        .filter(ExtractionResult.application_id == application_id)
        .all()
    )
    return [
        {
            "id": str(r.id),
            "document_id": str(r.document_id),
            "fields": r.fields,
            "overall_confidence": r.overall_confidence,
            "conflicts": r.conflicts,
            "status": r.status,
            "extractor": r.extractor,
        }
        for r in rows
    ]


@router.get("/{application_id}/documents")
def list_documents(application_id: UUID, db: Session = Depends(get_db)):
    rows = db.query(Document).filter(Document.application_id == application_id).all()
    return [
        {
            "id": str(d.id),
            "type": d.type,
            "source": d.source,
            "content_type": d.content_type,
            "byte_size": d.byte_size,
        }
        for d in rows
    ]


@router.get("/{application_id}/audit")
def list_audit(application_id: UUID, db: Session = Depends(get_db)):
    rows = (
        db.query(AuditEvent)
        .filter(AuditEvent.application_id == application_id)
        .order_by(AuditEvent.created_at.asc())
        .all()
    )
    return [
        {
            "id": str(e.id),
            "actor": e.actor,
            "action": e.action,
            "from_status": e.from_status,
            "to_status": e.to_status,
            "payload": e.payload,
            "created_at": e.created_at.isoformat(),
        }
        for e in rows
    ]
