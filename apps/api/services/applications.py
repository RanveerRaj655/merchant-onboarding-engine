import hashlib
import secrets
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from apps.api.config import get_settings
from apps.api.db.models import Application, Document, ReviewCase
from apps.api.services.state_machine import StateMachineService
from apps.api.services.verification import VerificationOrchestrator
from shared.enums import (
    ApplicationStatus,
    DocumentSource,
    DocumentType,
    ReviewCaseStatus,
    ReviewReason,
    ReviewResolution,
)
from shared.masking import (
    content_fingerprint,
    hash_sensitive,
    mask_bank_account,
    mask_gstin,
    mask_pan,
)
from shared.schemas import ApplicationCreate, ApplicationUpdate, ConfigChecks, StructuredDocumentPayload


def next_reference(db: Session) -> str:
    n = db.query(Application).count() + 1
    return f"APP-{n:06d}"


class ApplicationService:
    def __init__(self, db: Session):
        self.db = db
        self.settings = get_settings()
        self.sm = StateMachineService(db)

    def create(self, data: ApplicationCreate) -> Application:
        pepper = self.settings.app_pepper
        cfg = data.config_checks.model_dump()
        pan = (data.pan or "").strip().upper() or None
        gstin = (data.gstin or "").strip().upper() or None
        bank = (data.bank_account_number or "").strip() or None
        ifsc = (data.ifsc or "").strip().upper() or None
        app = Application(
            reference=next_reference(self.db),
            status=ApplicationStatus.SUBMITTED.value,
            legal_name=data.legal_name,
            trade_name=data.trade_name,
            pan_raw=pan,
            pan_masked=mask_pan(pan),
            pan_hash=hash_sensitive(pan, pepper),
            gstin_raw=gstin,
            gstin_masked=mask_gstin(gstin),
            gstin_hash=hash_sensitive(gstin, pepper),
            bank_account_raw=bank,
            bank_account_masked=mask_bank_account(bank),
            bank_account_hash=hash_sensitive(bank, pepper),
            ifsc=ifsc,
            contact_email=data.contact_email,
            contact_phone=data.contact_phone,
            business_category=data.business_category,
            missing_info=[],
            config_checks=cfg,
            content_fingerprint=content_fingerprint(pan, gstin, bank, ifsc),
            version=1,
        )
        self.db.add(app)
        self.db.flush()

        self.sm.audit_only(
            app.id,
            actor="merchant",
            action="APPLICATION_SUBMITTED",
            to_status=ApplicationStatus.SUBMITTED.value,
            payload={"reference": app.reference},
        )
        # Extraction starts via kickoff() after documents are attached
        self.db.commit()
        self.db.refresh(app)
        return app

    def kickoff(self, application_id: UUID) -> Application:
        """Enqueue START_EXTRACTION once docs are present (submit or re-upload)."""
        from apps.api.db.models import OutboxMessage
        from shared.enums import OutboxStatus

        app = self.db.get(Application, application_id)
        if app is None:
            raise ValueError("Application not found")
        if app.status not in (
            ApplicationStatus.SUBMITTED.value,
            ApplicationStatus.MISSING_INFO.value,
        ):
            return app

        pending = (
            self.db.query(OutboxMessage)
            .filter(
                OutboxMessage.application_id == application_id,
                OutboxMessage.command == "START_EXTRACTION",
                OutboxMessage.status.in_(
                    [OutboxStatus.PENDING.value, OutboxStatus.PROCESSING.value]
                ),
            )
            .first()
        )
        if pending:
            return app

        self.db.add(
            OutboxMessage(
                application_id=app.id,
                command="START_EXTRACTION",
                payload={"application_id": str(app.id)},
                status=OutboxStatus.PENDING.value,
            )
        )
        self.sm.audit_only(
            application_id,
            actor="system",
            action="EXTRACTION_ENQUEUED",
            payload={"from_status": app.status},
        )
        self.db.commit()
        self.db.refresh(app)
        return app

    def create_from_fixture(self, merchant_id: str) -> Application:
        """Create application + upload generated KYC images from fixtures/."""
        from shared.fixtures import fixture_doc_path, get_merchant
        from shared.schemas import ApplicationCreate, ConfigChecks

        merchant = get_merchant(merchant_id)
        if merchant is None:
            raise ValueError(f"Unknown fixture merchant {merchant_id}")

        form_pan = merchant.get("form_overrides", {}).get("pan", merchant["pan"])
        form_gstin = merchant.get("form_overrides", {}).get("gstin", merchant["gstin"])
        scenario = merchant.get("scenario")
        # Registry pan_status/gst_status already encode gst_fail/pan_fail.
        # Only force scenario for timeout / provision_fail / bank_fail overrides.
        verify_scenario = scenario if scenario in ("timeout", "provision_fail", "bank_fail") else None

        body = ApplicationCreate(
            legal_name=merchant["legal_name"],
            trade_name=merchant.get("trade_name"),
            pan=form_pan,
            gstin=form_gstin,
            bank_account_number=merchant.get("bank_account_number"),
            ifsc=merchant.get("ifsc"),
            contact_email=merchant["contact_email"],
            contact_phone=merchant.get("contact_phone"),
            business_category=merchant.get("business_category"),
            config_checks=ConfigChecks(scenario=verify_scenario),
        )
        app = self.create(body)

        docs = merchant.get("documents") or {}
        mapping = [
            ("pan_card", DocumentType.PAN_CARD),
            ("gst_cert", DocumentType.GST_CERT),
            ("cheque", DocumentType.CANCELLED_CHEQUE),
        ]
        for key, dtype in mapping:
            rel = docs.get(key)
            if not rel:
                continue
            path = fixture_doc_path(rel)
            if not path.exists():
                raise ValueError(f"Fixture file missing: {path}")
            self.add_upload(
                app.id,
                doc_type=dtype,
                filename=path.name,
                content_type="image/png",
                data=path.read_bytes(),
            )

        self.kickoff(app.id)
        self.db.refresh(app)
        return app

    def add_structured_document(
        self, application_id: UUID, payload: StructuredDocumentPayload
    ) -> Document:
        app = self.db.get(Application, application_id)
        if app is None:
            raise ValueError("Application not found")

        doc = Document(
            application_id=application_id,
            type=payload.document_type.value,
            source=DocumentSource.STRUCTURED_PAYLOAD.value,
            structured_payload=payload.model_dump(),
            content_type="application/json",
            byte_size=len(str(payload.model_dump())),
            sha256=hashlib.sha256(str(payload.model_dump()).encode()).hexdigest(),
        )
        self.db.add(doc)
        self.sm.audit_only(
            application_id,
            actor="merchant",
            action="DOCUMENT_STRUCTURED_ADDED",
            payload={"type": payload.document_type.value},
        )
        self.db.commit()
        self.db.refresh(doc)
        return doc

    def add_upload(
        self,
        application_id: UUID,
        *,
        doc_type: DocumentType,
        filename: str,
        content_type: str,
        data: bytes,
        commit: bool = True,
        actor: str = "merchant",
    ) -> Document:
        app = self.db.get(Application, application_id)
        if app is None:
            raise ValueError("Application not found")

        storage_root = Path(self.settings.doc_storage_path)
        storage_root.mkdir(parents=True, exist_ok=True)
        key = f"{application_id}/{secrets.token_hex(8)}_{Path(filename).name}"
        path = storage_root / key
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

        doc = Document(
            application_id=application_id,
            type=doc_type.value,
            source=DocumentSource.UPLOAD.value,
            storage_key=key,
            content_type=content_type,
            byte_size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
        )
        self.db.add(doc)
        self.sm.audit_only(
            application_id,
            actor=actor,
            action="DOCUMENT_UPLOADED",
            payload={"type": doc_type.value, "filename": filename, "bytes": len(data)},
        )
        if commit:
            self.db.commit()
            self.db.refresh(doc)
        else:
            self.db.flush()
        return doc

    def update(self, application_id: UUID, data: ApplicationUpdate) -> Application:
        app = self.db.get(Application, application_id)
        if app is None:
            raise ValueError("Application not found")
        if app.version != data.version:
            raise ValueError(f"Version conflict: expected {data.version}, got {app.version}")

        pepper = self.settings.app_pepper
        bank_changed = False
        fields_updated = []

        if data.legal_name is not None:
            app.legal_name = data.legal_name
            fields_updated.append("legal_name")
        if data.trade_name is not None:
            app.trade_name = data.trade_name
            fields_updated.append("trade_name")
        if data.pan is not None:
            app.pan_raw = data.pan.upper()
            app.pan_masked = mask_pan(data.pan)
            app.pan_hash = hash_sensitive(data.pan, pepper)
            fields_updated.append("pan")
        if data.gstin is not None:
            app.gstin_raw = data.gstin.upper()
            app.gstin_masked = mask_gstin(data.gstin)
            app.gstin_hash = hash_sensitive(data.gstin, pepper)
            fields_updated.append("gstin")
        if data.bank_account_number is not None:
            if data.bank_account_number != app.bank_account_raw:
                bank_changed = True
            app.bank_account_raw = data.bank_account_number
            app.bank_account_masked = mask_bank_account(data.bank_account_number)
            app.bank_account_hash = hash_sensitive(data.bank_account_number, pepper)
            fields_updated.append("bank_account_number")
        if data.ifsc is not None:
            if data.ifsc.upper() != (app.ifsc or ""):
                bank_changed = True
            app.ifsc = data.ifsc.upper()
            fields_updated.append("ifsc")
        if data.contact_email is not None:
            app.contact_email = data.contact_email
            fields_updated.append("contact_email")
        if data.contact_phone is not None:
            app.contact_phone = data.contact_phone
            fields_updated.append("contact_phone")
        if data.business_category is not None:
            app.business_category = data.business_category
            fields_updated.append("business_category")

        app.version += 1
        app.content_fingerprint = content_fingerprint(
            app.pan_raw, app.gstin_raw, app.bank_account_raw, app.ifsc
        )
        app.missing_info = []

        self.sm.audit_only(
            application_id,
            actor="merchant",
            action="APPLICATION_UPDATED",
            payload={"fields": fields_updated, "bank_changed": bank_changed},
        )

        if bank_changed and app.status in (
            ApplicationStatus.CHECKS_PASSED.value,
            ApplicationStatus.PROVISIONING.value,
            ApplicationStatus.IN_REVIEW.value,
            ApplicationStatus.VERIFICATION_PARTIAL.value,
            ApplicationStatus.AWAITING_CALLBACKS.value,
            ApplicationStatus.PENDING_VERIFICATION.value,
        ):
            VerificationOrchestrator(self.db).supersede_bank_and_reverify(application_id)
        elif app.status == ApplicationStatus.MISSING_INFO.value:
            from apps.api.db.models import OutboxMessage
            from shared.enums import OutboxStatus

            self.sm.transition(
                application_id,
                ApplicationStatus.EXTRACTING,
                actor="merchant",
                action="MERCHANT_RESUBMIT",
                expected_version=None,
            )
            self.db.add(
                OutboxMessage(
                    application_id=app.id,
                    command="START_EXTRACTION",
                    payload={"application_id": str(app.id)},
                    status=OutboxStatus.PENDING.value,
                )
            )

        self.db.commit()
        self.db.refresh(app)
        return app

    def to_public_dict(self, app: Application, *, reveal: bool = False) -> dict[str, Any]:
        data = {
            "id": str(app.id),
            "reference": app.reference,
            "status": app.status,
            "legal_name": app.legal_name,
            "trade_name": app.trade_name,
            "pan_masked": app.pan_masked,
            "gstin_masked": app.gstin_masked,
            "bank_account_masked": app.bank_account_masked,
            "ifsc": app.ifsc,
            "contact_email": app.contact_email,
            "contact_phone": app.contact_phone,
            "business_category": app.business_category,
            "missing_info": app.missing_info,
            "config_checks": app.config_checks,
            "version": app.version,
            "submitted_at": app.submitted_at.isoformat() if app.submitted_at else None,
            "updated_at": app.updated_at.isoformat() if app.updated_at else None,
            "status_entered_at": app.status_entered_at.isoformat()
            if app.status_entered_at
            else None,
        }
        if reveal:
            data["pan"] = app.pan_raw
            data["gstin"] = app.gstin_raw
            data["bank_account_number"] = app.bank_account_raw
        return data

    def operator_action(
        self,
        review_case_id: UUID,
        *,
        action: str,
        note: str,
        version: int,
        missing_info: list[dict] | None = None,
        actor: str = "operator:demo",
    ) -> Application:
        case = self.db.get(ReviewCase, review_case_id)
        if case is None:
            raise ValueError("Review case not found")
        app = self.db.get(Application, case.application_id)
        if app is None:
            raise ValueError("Application not found")
        if app.version != version:
            raise ValueError(f"Version conflict: expected {version}, got {app.version}")

        from apps.api.db.models import OutboxMessage
        from shared.enums import OutboxStatus

        if action == "approve":
            if not VerificationOrchestrator(self.db).checks_passed(app):
                raise ValueError("Required checks must pass for current details before approval")
            case.status = ReviewCaseStatus.RESOLVED.value
            case.resolution = ReviewResolution.APPROVED.value
            case.notes = note
            self.sm.transition(
                app.id,
                ApplicationStatus.CHECKS_PASSED,
                actor=actor,
                action="OPERATOR_APPROVE_OVERRIDE",
                payload={"note": note, "review_case_id": str(case.id)},
                expected_version=version,
                outbox_commands=[{"command": "PROVISION_MERCHANT", "payload": {}}],
            )
            self.sm.transition(
                app.id,
                ApplicationStatus.PROVISIONING,
                actor=actor,
                action="START_PROVISION",
            )
        elif action == "reject":
            case.status = ReviewCaseStatus.RESOLVED.value
            case.resolution = ReviewResolution.REJECTED.value
            case.notes = note
            self.sm.transition(
                app.id,
                ApplicationStatus.REJECTED,
                actor=actor,
                action="OPERATOR_REJECT",
                payload={"note": note},
                expected_version=version,
            )
        elif action == "request_info":
            case.status = ReviewCaseStatus.WAITING_MERCHANT.value
            case.resolution = ReviewResolution.INFO_REQUESTED.value
            case.notes = note
            app.missing_info = missing_info or [
                {"field": "additional_docs", "message": note}
            ]
            self.sm.transition(
                app.id,
                ApplicationStatus.MISSING_INFO,
                actor=actor,
                action="OPERATOR_REQUEST_INFO",
                payload={"note": note, "missing_info": app.missing_info},
                expected_version=version,
            )
        elif action == "reverify":
            case.status = ReviewCaseStatus.IN_REVIEW.value
            case.resolution = ReviewResolution.REVERIFY.value
            case.notes = note
            # Clear forced fail scenarios so corrected registry data can pass
            cfg = dict(app.config_checks or {})
            cfg.pop("scenario", None)
            app.config_checks = cfg
            from apps.api.db.models import VerificationRequest
            from shared.enums import VerificationStatus

            for req in (
                self.db.query(VerificationRequest)
                .filter(VerificationRequest.application_id == app.id)
                .all()
            ):
                if req.status != VerificationStatus.SUPERSEDED.value:
                    req.status = VerificationStatus.SUPERSEDED.value
            self.sm.transition(
                app.id,
                ApplicationStatus.PENDING_VERIFICATION,
                actor=actor,
                action="OPERATOR_REVERIFY",
                payload={"note": note},
                expected_version=version,
                outbox_commands=[{"command": "START_VERIFICATION", "payload": {}}],
            )
        elif action == "retry_provision":
            if not VerificationOrchestrator(self.db).checks_passed(app):
                raise ValueError("Re-verify current details before retrying provisioning")
            case.status = ReviewCaseStatus.IN_REVIEW.value
            case.notes = note
            cfg = dict(app.config_checks or {})
            if cfg.get("scenario") == "provision_fail":
                cfg.pop("scenario", None)
                app.config_checks = cfg
            self.sm.transition(
                app.id,
                ApplicationStatus.CHECKS_PASSED,
                actor=actor,
                action="OPERATOR_RETRY_PROVISION",
                payload={"note": note},
                expected_version=version,
                outbox_commands=[{"command": "PROVISION_MERCHANT", "payload": {}}],
            )
            self.sm.transition(
                app.id,
                ApplicationStatus.PROVISIONING,
                actor=actor,
                action="START_PROVISION",
            )
        else:
            raise ValueError(f"Unknown action: {action}")

        self.db.commit()
        self.db.refresh(app)
        return app

    def hitl_correct(
        self,
        application_id: UUID,
        *,
        pan: str | None = None,
        gstin: str | None = None,
        bank_account_number: str | None = None,
        ifsc: str | None = None,
        legal_name: str | None = None,
        note: str = "",
        docs: list[tuple[DocumentType, str, str, bytes]] | None = None,
        actor: str = "operator:demo",
        mode: str = "auto",  # auto | extract | verify
    ) -> Application:
        """HITL correction: update fields/docs, clear fail scenario, re-extract and/or async re-verify."""
        from apps.api.db.models import OutboxMessage, VerificationRequest
        from shared.enums import OutboxStatus, VerificationStatus

        app = self.db.get(Application, application_id)
        if app is None:
            raise ValueError("Application not found")
        if app.status not in (
            ApplicationStatus.MISSING_INFO.value,
            ApplicationStatus.IN_REVIEW.value,
            ApplicationStatus.EXTRACTION_FAILED.value,
        ):
            raise ValueError(f"HITL correct not allowed from status {app.status}")

        pepper = self.settings.app_pepper
        identity_changed = False
        fields_updated: list[str] = []

        if legal_name is not None and legal_name.strip():
            app.legal_name = legal_name.strip()
            fields_updated.append("legal_name")
        if pan is not None and pan.strip():
            new_pan = pan.strip().upper()
            if new_pan != (app.pan_raw or ""):
                identity_changed = True
            app.pan_raw = new_pan
            app.pan_masked = mask_pan(new_pan)
            app.pan_hash = hash_sensitive(new_pan, pepper)
            fields_updated.append("pan")
        if gstin is not None and gstin.strip():
            new_gst = gstin.strip().upper()
            if new_gst != (app.gstin_raw or ""):
                identity_changed = True
            app.gstin_raw = new_gst
            app.gstin_masked = mask_gstin(new_gst)
            app.gstin_hash = hash_sensitive(new_gst, pepper)
            fields_updated.append("gstin")
        if bank_account_number is not None and bank_account_number.strip():
            if bank_account_number.strip() != (app.bank_account_raw or ""):
                identity_changed = True
            app.bank_account_raw = bank_account_number.strip()
            app.bank_account_masked = mask_bank_account(bank_account_number)
            app.bank_account_hash = hash_sensitive(bank_account_number, pepper)
            fields_updated.append("bank_account_number")
        if ifsc is not None and ifsc.strip():
            new_ifsc = ifsc.strip().upper()
            if new_ifsc != (app.ifsc or ""):
                identity_changed = True
            app.ifsc = new_ifsc
            fields_updated.append("ifsc")

        # Clear forced failure scenarios so corrected data can pass async verify
        from sqlalchemy.orm.attributes import flag_modified

        cfg = dict(app.config_checks or {})
        cleared_scenario = cfg.pop("scenario", None)
        app.config_checks = cfg
        flag_modified(app, "config_checks")
        app.content_fingerprint = content_fingerprint(
            app.pan_raw, app.gstin_raw, app.bank_account_raw, app.ifsc
        )
        app.missing_info = []
        flag_modified(app, "missing_info")
        app.version += 1

        uploaded_types: list[str] = []
        for dtype, filename, content_type, data in docs or []:
            self.add_upload(
                application_id,
                doc_type=dtype,
                filename=filename,
                content_type=content_type,
                data=data,
                commit=False,
                actor=actor,
            )
            uploaded_types.append(dtype.value)

        # Resolve open review cases to IN_REVIEW / WAITING while reprocessing
        for case in (
            self.db.query(ReviewCase)
            .filter(
                ReviewCase.application_id == application_id,
                ReviewCase.status.in_(
                    [
                        ReviewCaseStatus.OPEN.value,
                        ReviewCaseStatus.IN_REVIEW.value,
                        ReviewCaseStatus.WAITING_MERCHANT.value,
                    ]
                ),
            )
            .all()
        ):
            case.notes = (case.notes or "") + f"\nHITL correct: {note or 'updated'}"
            case.status = ReviewCaseStatus.IN_REVIEW.value

        self.sm.audit_only(
            application_id,
            actor=actor,
            action="HITL_CORRECTION",
            payload={
                "fields": fields_updated,
                "docs": uploaded_types,
                "cleared_scenario": cleared_scenario,
                "note": note,
                "mode": mode,
            },
        )

        # Decide extract vs verify restart
        reason_codes: list[str] = []
        open_case = (
            self.db.query(ReviewCase)
            .filter(
                ReviewCase.application_id == application_id,
                ReviewCase.status.in_(
                    [
                        ReviewCaseStatus.OPEN.value,
                        ReviewCaseStatus.IN_REVIEW.value,
                        ReviewCaseStatus.WAITING_MERCHANT.value,
                    ]
                ),
            )
            .order_by(ReviewCase.created_at.desc())
            .first()
        )
        if open_case:
            reason_codes = list(open_case.reason_codes or [])

        unread_reasons = {
            ReviewReason.DOC_BLURRED.value,
            ReviewReason.DOC_BLACKED_OUT.value,
            ReviewReason.DOC_UNREADABLE.value,
            ReviewReason.MISSING_DOCS.value,
            ReviewReason.EXTRACTION_FAILED.value,
        }
        conflict_or_low = {
            ReviewReason.DOC_CONFLICT.value,
            ReviewReason.LOW_CONFIDENCE.value,
        }
        verify_fail_reasons = {
            ReviewReason.GST_FAIL.value,
            ReviewReason.PAN_FAIL.value,
            ReviewReason.BANK_FAIL.value,
            ReviewReason.VERIFICATION_TIMEOUT.value,
            ReviewReason.PROVISION_FAIL.value,
        }

        if mode != "verify" and any(r in unread_reasons for r in reason_codes) and not uploaded_types:
            raise ValueError(
                "This case needs clear document re-uploads before re-processing."
            )

        needs_extract = bool(uploaded_types) or mode == "extract"
        if mode == "auto":
            if any(r in conflict_or_low for r in reason_codes):
                needs_extract = True
            elif any(r in verify_fail_reasons for r in reason_codes) and not uploaded_types:
                needs_extract = False
            elif app.status == ApplicationStatus.MISSING_INFO.value and uploaded_types:
                needs_extract = True
            elif app.status == ApplicationStatus.MISSING_INFO.value and not uploaded_types:
                needs_extract = False

        if mode == "verify" and not uploaded_types:
            if any(r in conflict_or_low for r in reason_codes):
                raise ValueError("Document conflicts and low confidence require re-extraction; use Auto.")
            needs_extract = False

        if needs_extract:
            if app.status == ApplicationStatus.MISSING_INFO.value:
                self.sm.transition(
                    application_id,
                    ApplicationStatus.EXTRACTING,
                    actor=actor,
                    action="HITL_REEXTRACT",
                    payload={"note": note},
                )
            elif app.status == ApplicationStatus.IN_REVIEW.value:
                # IN_REVIEW → MISSING_INFO → EXTRACTING not ideal; go via PENDING after extract
                # Allow IN_REVIEW → PENDING_VERIFICATION only; for extract use MISSING_INFO path
                # Transition IN_REVIEW → MISSING_INFO → EXTRACTING is allowed chain
                self.sm.transition(
                    application_id,
                    ApplicationStatus.MISSING_INFO,
                    actor=actor,
                    action="HITL_PREPARE_REEXTRACT",
                    payload={"note": note},
                )
                self.sm.transition(
                    application_id,
                    ApplicationStatus.EXTRACTING,
                    actor=actor,
                    action="HITL_REEXTRACT",
                )
            self.db.add(
                OutboxMessage(
                    application_id=app.id,
                    command="START_EXTRACTION",
                    payload={"application_id": str(app.id)},
                    status=OutboxStatus.PENDING.value,
                )
            )
        else:
            # Verification failure correction → async re-verify
            for req in (
                self.db.query(VerificationRequest)
                .filter(VerificationRequest.application_id == application_id)
                .all()
            ):
                if req.status != VerificationStatus.SUPERSEDED.value:
                    req.status = VerificationStatus.SUPERSEDED.value
            if app.status != ApplicationStatus.PENDING_VERIFICATION.value:
                self.sm.transition(
                    application_id,
                    ApplicationStatus.PENDING_VERIFICATION,
                    actor=actor,
                    action="HITL_REVERIFY",
                    payload={
                        "note": note,
                        "identity_changed": identity_changed,
                        "fields": fields_updated,
                    },
                    outbox_commands=[{"command": "START_VERIFICATION", "payload": {}}],
                )
            else:
                self.db.add(
                    OutboxMessage(
                        application_id=app.id,
                        command="START_VERIFICATION",
                        payload={"application_id": str(app.id)},
                        status=OutboxStatus.PENDING.value,
                    )
                )

        self.db.commit()
        self.db.refresh(app)
        return app
