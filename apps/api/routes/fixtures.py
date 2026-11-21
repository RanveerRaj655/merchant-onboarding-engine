from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from apps.api.db.session import get_db
from apps.api.services.applications import ApplicationService
from shared.fixtures import FIXTURES_PATH, fixture_doc_path, load_registry

router = APIRouter(prefix="/api/v1/fixtures", tags=["fixtures"])


@router.get("/merchants")
def list_merchants():
    return {
        "source": str(FIXTURES_PATH),
        "note": "Synthetic format-valid KYC fixtures only — not scraped real PII",
        "merchants": [
            {
                "id": m["id"],
                "legal_name": m["legal_name"],
                "trade_name": m.get("trade_name"),
                "pan": m["pan"],
                "gstin": m["gstin"],
                "scenario": m.get("scenario"),
                "pan_status": m.get("pan_status"),
                "gst_status": m.get("gst_status"),
                "documents": m.get("documents"),
            }
            for m in load_registry()
        ],
    }


@router.get("/docs/{filename}")
def get_fixture_doc(filename: str):
    if "/" in filename or ".." in filename:
        raise HTTPException(400, "Invalid filename")
    path = fixture_doc_path(f"docs/{filename}")
    if not path.exists():
        raise HTTPException(404, "Fixture document not found")
    return FileResponse(path, media_type="image/png", filename=filename)


@router.post("/merchants/{merchant_id}/submit")
def submit_fixture_merchant(merchant_id: str, db: Session = Depends(get_db)):
    """Create app from fixture registry + OCR image docs + kickoff."""
    svc = ApplicationService(db)
    try:
        app = svc.create_from_fixture(merchant_id)
    except ValueError as e:
        raise HTTPException(404, str(e)) from e
    return svc.to_public_dict(app)
