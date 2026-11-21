from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from apps.api.config import get_settings
from apps.api.db.models import Application, Document, ExtractionResult, ReviewCase
from apps.api.services.ocr import extract_from_image
from apps.api.services.state_machine import StateMachineService
from shared.enums import (
    ApplicationStatus,
    DocumentSource,
    ExtractionStatus,
    ExtractorType,
    ReviewCaseStatus,
    ReviewReason,
)


class ExtractionService:
    def __init__(self, db: Session):
        self.db = db
        self.settings = get_settings()
        self.sm = StateMachineService(db)

    def run_for_application(self, application_id: UUID) -> None:
        app = self.db.get(Application, application_id)
        if app is None:
            return

        if app.status == ApplicationStatus.SUBMITTED.value:
            self.sm.transition(
                application_id,
                ApplicationStatus.EXTRACTING,
                actor="system",
                action="START_EXTRACTION",
            )
        elif app.status == ApplicationStatus.MISSING_INFO.value:
            # Merchant re-submitted docs — move back into extracting
            self.sm.transition(
                application_id,
                ApplicationStatus.EXTRACTING,
                actor="system",
                action="RESTART_EXTRACTION",
            )

        # Latest document wins per type (supports MISSING_INFO re-uploads)
        all_docs = (
            self.db.query(Document)
            .filter(Document.application_id == application_id)
            .order_by(Document.created_at.desc())
            .all()
        )
        seen_types: set[str] = set()
        docs = []
        for d in all_docs:
            if d.type in seen_types:
                continue
            seen_types.add(d.type)
            docs.append(d)
        if not docs:
            self.sm.transition(
                application_id,
                ApplicationStatus.MISSING_INFO,
                actor="system",
                action="EXTRACTION_MISSING_DOCS",
                payload={"reason": ReviewReason.MISSING_DOCS.value},
            )
            app.missing_info = [
                {
                    "field": "documents",
                    "message": "At least one document required (PAN card / GST cert)",
                    "action": "reupload",
                }
            ]
            self._open_review(app, [ReviewReason.MISSING_DOCS.value])
            self.db.commit()
            return

        all_conflicts: list[dict[str, Any]] = []
        min_confidence = 1.0
        unreadable_docs: list[dict[str, Any]] = []
        aggregated_fields: dict[str, Any] = {}

        for doc in docs:
            result = self._extract_document(app, doc)
            self.db.add(result)
            all_conflicts.extend(result.conflicts if isinstance(result.conflicts, list) else [])
            min_confidence = min(min_confidence, result.overall_confidence)
            if result.status == ExtractionStatus.UNREADABLE.value:
                quality = (result.fields or {}).get("_quality") or {}
                # quality stored separately below — pull from conflicts payload style
                reason = None
                if isinstance(result.conflicts, dict):
                    reason = result.conflicts.get("quality_reason")
                elif isinstance(result.conflicts, list):
                    for c in result.conflicts:
                        if isinstance(c, dict) and c.get("quality_reason"):
                            reason = c["quality_reason"]
                unreadable_docs.append(
                    {
                        "document_id": str(doc.id),
                        "document_type": doc.type,
                        "quality_reason": reason or "UNREADABLE",
                        "extractor": result.extractor,
                    }
                )
            # Don't aggregate internal meta keys
            for k, v in (result.fields or {}).items():
                if k.startswith("_"):
                    continue
                aggregated_fields[k] = v

        self.db.flush()

        threshold = float(
            (app.config_checks or {}).get(
                "extraction_confidence_min", self.settings.extraction_confidence_min
            )
        )

        if unreadable_docs:
            reasons = []
            missing = []
            for u in unreadable_docs:
                qr = u["quality_reason"]
                if qr == "BLURRED":
                    reasons.append(ReviewReason.DOC_BLURRED.value)
                    msg = "Document appears blurred — please re-upload a clear scan"
                elif qr == "BLACKED_OUT":
                    reasons.append(ReviewReason.DOC_BLACKED_OUT.value)
                    msg = "Key fields are blacked out / redacted — please re-upload unredacted document"
                else:
                    reasons.append(ReviewReason.DOC_UNREADABLE.value)
                    msg = f"Document unreadable ({qr}) — please re-upload"
                missing.append(
                    {
                        "field": u["document_type"],
                        "document_id": u["document_id"],
                        "message": msg,
                        "quality_reason": qr,
                        "action": "reupload",
                    }
                )
            reasons = list(dict.fromkeys(reasons))
            app.missing_info = missing

            self.sm.transition(
                application_id,
                ApplicationStatus.EXTRACTION_FAILED,
                actor="system",
                action="EXTRACTION_UNREADABLE",
                payload={"unreadable": unreadable_docs},
            )
            # Fixable by merchant → MISSING_INFO (request re-upload), not silent approve
            self.sm.transition(
                application_id,
                ApplicationStatus.MISSING_INFO,
                actor="system",
                action="REQUEST_DOC_REUPLOAD",
                payload={"reasons": reasons, "missing_info": missing},
            )
            self._open_review(app, reasons, status=ReviewCaseStatus.WAITING_MERCHANT.value)
            self.db.commit()
            return

        if all_conflicts:
            self.sm.transition(
                application_id,
                ApplicationStatus.IN_REVIEW,
                actor="system",
                action="EXTRACTION_CONFLICT",
                payload={"conflicts": all_conflicts},
            )
            self._open_review(app, [ReviewReason.DOC_CONFLICT.value])
            self.db.commit()
            return

        required = ["pan", "gstin"]
        missing = []
        for field_name in required:
            form_val = getattr(app, f"{field_name}_raw", None)
            extracted = aggregated_fields.get(field_name)
            conf = (extracted or {}).get("confidence", 1.0) if extracted else 0.0
            if not form_val and not extracted:
                missing.append(
                    {
                        "field": field_name,
                        "message": f"Required field {field_name} missing",
                        "action": "provide_field_or_doc",
                    }
                )
            elif extracted and conf < threshold:
                missing.append(
                    {
                        "field": field_name,
                        "message": f"Low OCR confidence ({conf})",
                        "confidence": conf,
                        "action": "reupload",
                    }
                )

        if missing and any(m.get("confidence") is not None for m in missing):
            self.sm.transition(
                application_id,
                ApplicationStatus.IN_REVIEW,
                actor="system",
                action="EXTRACTION_LOW_CONFIDENCE",
                payload={"missing": missing},
            )
            self._open_review(app, [ReviewReason.LOW_CONFIDENCE.value])
            self.db.commit()
            return

        if missing:
            app.missing_info = missing
            self.sm.transition(
                application_id,
                ApplicationStatus.MISSING_INFO,
                actor="system",
                action="EXTRACTION_MISSING_FIELDS",
                payload={"missing": missing},
            )
            self.db.commit()
            return

        # Form left blank → adopt OCR values so async verify uses extracted identity
        applied = self._apply_extracted_identity(app, aggregated_fields)

        self.sm.transition(
            application_id,
            ApplicationStatus.PENDING_VERIFICATION,
            actor="system",
            action="EXTRACTION_OK",
            payload={
                "fields": list(aggregated_fields.keys()),
                "min_confidence": min_confidence,
                "applied_from_ocr": applied,
            },
            outbox_commands=[{"command": "START_VERIFICATION", "payload": {}}],
        )
        self.db.commit()

    def _extract_document(self, app: Application, doc: Document) -> ExtractionResult:
        if doc.source == DocumentSource.STRUCTURED_PAYLOAD.value:
            return self._extract_structured(app, doc)

        # Real OCR path for uploaded image/PDF bytes
        storage_root = Path(self.settings.doc_storage_path)
        if not doc.storage_key:
            return ExtractionResult(
                document_id=doc.id,
                application_id=app.id,
                fields={},
                overall_confidence=0.0,
                conflicts=[{"quality_reason": "MISSING_FILE"}],
                status=ExtractionStatus.UNREADABLE.value,
                extractor=ExtractorType.TESSERACT_OCR.value,
            )

        path = storage_root / doc.storage_key
        if not path.exists():
            return ExtractionResult(
                document_id=doc.id,
                application_id=app.id,
                fields={},
                overall_confidence=0.0,
                conflicts=[{"quality_reason": "FILE_NOT_FOUND"}],
                status=ExtractionStatus.UNREADABLE.value,
                extractor=ExtractorType.TESSERACT_OCR.value,
            )

        ocr = extract_from_image(path, doc.type)
        if not ocr.quality.readable:
            return ExtractionResult(
                document_id=doc.id,
                application_id=app.id,
                fields={"_quality": ocr.quality.details},
                overall_confidence=ocr.overall_confidence,
                conflicts=[
                    {
                        "quality_reason": ocr.quality.reason or "UNREADABLE",
                        "details": ocr.quality.details,
                        "message": f"Document quality check failed: {ocr.quality.reason}",
                    }
                ],
                status=ExtractionStatus.UNREADABLE.value,
                extractor=ExtractorType.TESSERACT_OCR.value,
            )

        conflicts = self._detect_conflicts(app, ocr.fields)
        status = (
            ExtractionStatus.CONFLICT.value
            if conflicts
            else (
                ExtractionStatus.LOW_CONFIDENCE.value
                if ocr.overall_confidence < self.settings.extraction_confidence_min
                else ExtractionStatus.OK.value
            )
        )
        return ExtractionResult(
            document_id=doc.id,
            application_id=app.id,
            fields={**ocr.fields, "_quality": ocr.quality.details, "_ocr_preview": ocr.raw_text[:400]},
            overall_confidence=ocr.overall_confidence,
            conflicts=conflicts,
            status=status,
            extractor=ExtractorType.TESSERACT_OCR.value,
        )

    def _extract_structured(self, app: Application, doc: Document) -> ExtractionResult:
        payload = doc.structured_payload or {}
        if payload.get("unreadable"):
            return ExtractionResult(
                document_id=doc.id,
                application_id=app.id,
                fields={},
                overall_confidence=0.0,
                conflicts=[{"quality_reason": payload.get("quality_reason", "UNREADABLE")}],
                status=ExtractionStatus.UNREADABLE.value,
                extractor=ExtractorType.STRUCTURED.value,
            )

        raw_fields = payload.get("fields", {})
        confidence = float(payload.get("confidence", 1.0))
        fields: dict[str, Any] = {}
        for key, value in raw_fields.items():
            if isinstance(value, dict) and "value" in value:
                fields[key] = {
                    "value": value["value"],
                    "confidence": value.get("confidence", confidence),
                    "source": "STRUCTURED",
                }
            else:
                fields[key] = {
                    "value": value,
                    "confidence": confidence,
                    "source": "STRUCTURED",
                }

        conflicts = self._detect_conflicts(app, fields)
        status = (
            ExtractionStatus.CONFLICT.value
            if conflicts
            else (
                ExtractionStatus.LOW_CONFIDENCE.value
                if confidence < self.settings.extraction_confidence_min
                else ExtractionStatus.OK.value
            )
        )
        return ExtractionResult(
            document_id=doc.id,
            application_id=app.id,
            fields=fields,
            overall_confidence=confidence,
            conflicts=conflicts,
            status=status,
            extractor=ExtractorType.STRUCTURED.value,
        )

    def _apply_extracted_identity(
        self, app: Application, aggregated_fields: dict[str, Any]
    ) -> list[str]:
        """Copy OCR values onto the application when the form left them blank."""
        from shared.masking import (
            content_fingerprint,
            hash_sensitive,
            mask_bank_account,
            mask_gstin,
            mask_pan,
        )

        pepper = self.settings.app_pepper
        applied: list[str] = []

        pan_ext = aggregated_fields.get("pan") or {}
        if not app.pan_raw and pan_ext.get("value"):
            val = str(pan_ext["value"]).strip().upper()
            app.pan_raw = val
            app.pan_masked = mask_pan(val)
            app.pan_hash = hash_sensitive(val, pepper)
            applied.append("pan")

        gst_ext = aggregated_fields.get("gstin") or {}
        if not app.gstin_raw and gst_ext.get("value"):
            val = str(gst_ext["value"]).strip().upper()
            app.gstin_raw = val
            app.gstin_masked = mask_gstin(val)
            app.gstin_hash = hash_sensitive(val, pepper)
            applied.append("gstin")

        bank_ext = aggregated_fields.get("bank_account_number") or {}
        if not app.bank_account_raw and bank_ext.get("value"):
            val = str(bank_ext["value"]).strip()
            app.bank_account_raw = val
            app.bank_account_masked = mask_bank_account(val)
            app.bank_account_hash = hash_sensitive(val, pepper)
            applied.append("bank_account_number")

        ifsc_ext = aggregated_fields.get("ifsc") or {}
        if not app.ifsc and ifsc_ext.get("value"):
            val = str(ifsc_ext["value"]).strip().upper()
            app.ifsc = val
            applied.append("ifsc")

        if applied:
            app.content_fingerprint = content_fingerprint(
                app.pan_raw, app.gstin_raw, app.bank_account_raw, app.ifsc
            )
        return applied

    def _detect_conflicts(
        self, app: Application, fields: dict[str, Any]
    ) -> list[dict[str, Any]]:
        conflicts = []
        mapping = {
            "pan": app.pan_raw,
            "gstin": app.gstin_raw,
            "bank_account_number": app.bank_account_raw,
            "ifsc": app.ifsc,
        }
        for key, form_val in mapping.items():
            extracted = fields.get(key)
            if not form_val or not extracted:
                continue
            ext_val = str(extracted.get("value", "")).strip().upper()
            form_norm = str(form_val).strip().upper()
            if ext_val and form_norm and ext_val != form_norm:
                conflicts.append(
                    {
                        "field": key,
                        "form_value_masked": form_norm[-4:],
                        "extracted_value_masked": ext_val[-4:],
                        "message": f"{key} mismatch between form and document",
                    }
                )
        return conflicts

    def _open_review(
        self,
        app: Application,
        reasons: list[str],
        *,
        status: str = ReviewCaseStatus.OPEN.value,
    ) -> None:
        existing = (
            self.db.query(ReviewCase)
            .filter(
                ReviewCase.application_id == app.id,
                ReviewCase.status.in_(
                    [
                        ReviewCaseStatus.OPEN.value,
                        ReviewCaseStatus.IN_REVIEW.value,
                        ReviewCaseStatus.WAITING_MERCHANT.value,
                    ]
                ),
            )
            .first()
        )
        if existing:
            existing.reason_codes = list(dict.fromkeys((existing.reason_codes or []) + reasons))
            existing.status = status
            return

        priority = 75
        if any("BLACKED" in r or "BLURRED" in r or "UNREADABLE" in r for r in reasons):
            priority = 65
        if any("FAIL" in r for r in reasons):
            priority = 80

        self.db.add(
            ReviewCase(
                application_id=app.id,
                reason_codes=reasons,
                status=status,
                priority=priority,
            )
        )
