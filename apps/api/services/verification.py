import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from apps.api.config import get_settings
from apps.api.db.models import (
    Application,
    ReviewCase,
    VerificationCallback,
    VerificationRequest,
)
from apps.api.services.state_machine import StateMachineService
from shared.enums import (
    ApplicationStatus,
    CallbackProcessingResult,
    ReviewCaseStatus,
    ReviewReason,
    VerificationOutcome,
    VerificationProvider,
    VerificationStatus,
)
from shared.masking import content_fingerprint, mask_bank_account, mask_gstin, mask_pan


class VerificationOrchestrator:
    def __init__(self, db: Session):
        self.db = db
        self.settings = get_settings()
        self.sm = StateMachineService(db)

    def required_providers(self, app: Application) -> list[str]:
        cfg = app.config_checks or {}
        required = list(cfg.get("required_verifications", ["PAN", "GST"]))
        if cfg.get("require_bank_if_present", True) and app.bank_account_raw and app.ifsc:
            if VerificationProvider.BANK.value not in required:
                required.append(VerificationProvider.BANK.value)
        return required

    def checks_passed(self, app: Application) -> bool:
        required = self.required_providers(app)
        requests = self.db.query(VerificationRequest).filter(
            VerificationRequest.application_id == app.id
        ).order_by(VerificationRequest.created_at.desc()).all()
        latest = {}
        for req in requests:
            if req.status != VerificationStatus.SUPERSEDED.value:
                latest.setdefault(req.provider, req)
        fingerprint = content_fingerprint(
            app.pan_raw, app.gstin_raw, app.bank_account_raw, app.ifsc)
        return bool(required) and app.content_fingerprint == fingerprint and all(
            provider in latest
            and latest[provider].status == VerificationStatus.SUCCEEDED.value
            and latest[provider].outcome == VerificationOutcome.PASS.value
            for provider in required
        )

    def start_verification(self, application_id: UUID) -> None:
        app = self.db.get(Application, application_id)
        if app is None:
            return

        if app.status != ApplicationStatus.PENDING_VERIFICATION.value:
            return

        fingerprint = content_fingerprint(
            app.pan_raw, app.gstin_raw, app.bank_account_raw, app.ifsc
        )
        app.content_fingerprint = fingerprint
        timeout_ms = int(
            (app.config_checks or {}).get(
                "verification_timeout_ms", self.settings.verification_timeout_ms
            )
        )
        timeout_at = datetime.now(timezone.utc) + timedelta(milliseconds=timeout_ms)
        scenario = (app.config_checks or {}).get("scenario")

        created = []
        for provider in self.required_providers(app):
            base_idem = hashlib.sha256(
                f"{app.id}:{provider}:{fingerprint}".encode()
            ).hexdigest()
            # Supersede any prior non-terminal / prior attempts for this provider
            priors = (
                self.db.query(VerificationRequest)
                .filter(
                    VerificationRequest.application_id == app.id,
                    VerificationRequest.provider == provider,
                )
                .all()
            )
            next_attempt = max([prior.attempt for prior in priors], default=0) + 1
            for prior in priors:
                if prior.status != VerificationStatus.SUPERSEDED.value:
                    prior.status = VerificationStatus.SUPERSEDED.value

            correlation_id = str(uuid.uuid4())
            payload = self._build_payload(app, provider, scenario)
            # Unique key per attempt so HITL corrections always dispatch fresh sends
            idem = f"{base_idem}:{next_attempt}"
            req = VerificationRequest(
                application_id=app.id,
                provider=provider,
                idempotency_key=idem,
                status=VerificationStatus.PENDING.value,
                request_payload=payload,
                attempt=next_attempt,
                timeout_at=timeout_at,
                correlation_id=correlation_id,
            )
            self.db.add(req)
            created.append(req)

        self.db.flush()

        outbox = [
            {
                "command": "SEND_VERIFICATION",
                "payload": {"verification_request_id": str(r.id)},
            }
            for r in created
            if r.status == VerificationStatus.PENDING.value
        ]
        outbox.append(
            {
                "command": "VERIFICATION_TIMEOUT",
                "payload": {"application_id": str(app.id)},
                "available_at": timeout_at,
            }
        )

        self.sm.transition(
            application_id,
            ApplicationStatus.AWAITING_CALLBACKS,
            actor="system",
            action="VERIFICATIONS_DISPATCHED",
            payload={
                "providers": [r.provider for r in created],
                "fingerprint": fingerprint,
                "gstin_masked": mask_gstin(app.gstin_raw),
                "pan_masked": mask_pan(app.pan_raw),
            },
            outbox_commands=outbox,
        )
        self.db.commit()

    def _build_payload(
        self, app: Application, provider: str, scenario: str | None
    ) -> dict[str, Any]:
        base = {"scenario": scenario, "legal_name": app.legal_name}
        if provider == VerificationProvider.PAN.value:
            base.update({"pan_masked": mask_pan(app.pan_raw), "pan": app.pan_raw})
        elif provider == VerificationProvider.GST.value:
            base.update({"gstin_masked": mask_gstin(app.gstin_raw), "gstin": app.gstin_raw})
        elif provider == VerificationProvider.BANK.value:
            base.update(
                {
                    "account_masked": mask_bank_account(app.bank_account_raw),
                    "account": app.bank_account_raw,
                    "ifsc": app.ifsc,
                }
            )
        return base

    def apply_callback(
        self,
        provider: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        provider_event_id = payload["provider_event_id"]
        correlation_id = payload["correlation_id"]
        outcome = VerificationOutcome(payload["outcome"])

        existing_cb = (
            self.db.query(VerificationCallback)
            .filter(
                VerificationCallback.provider == provider,
                VerificationCallback.provider_event_id == provider_event_id,
            )
            .first()
        )
        if existing_cb:
            if not existing_cb.verification_request:
                return {"processing_result": CallbackProcessingResult.DUPLICATE.value}
            app_id = existing_cb.verification_request.application_id
            # Audit-only duplicate — unique constraint prevents a second row
            self.sm.audit_only(
                app_id,
                actor=f"provider:{provider}",
                action="CALLBACK_DUPLICATE",
                payload={
                    "provider_event_id": provider_event_id,
                    "processing_result": CallbackProcessingResult.DUPLICATE.value,
                    "original_callback_id": str(existing_cb.id),
                },
            )
            self.db.commit()
            return {
                "processing_result": CallbackProcessingResult.DUPLICATE.value,
                "callback_id": str(existing_cb.id),
            }

        req = (
            self.db.query(VerificationRequest)
            .filter(VerificationRequest.correlation_id == correlation_id, VerificationRequest.provider == provider)
            .first()
        )
        if req is None:
            cb = VerificationCallback(
                verification_request_id=None,
                provider=provider,
                provider_event_id=provider_event_id,
                payload=self._redact(payload),
                outcome=outcome.value,
                processing_result=CallbackProcessingResult.LATE_IGNORED.value,
            )
            self.db.add(cb)
            self.db.commit()
            return {"processing_result": CallbackProcessingResult.LATE_IGNORED.value}

        if req.status in (VerificationStatus.SUCCEEDED.value, VerificationStatus.FAILED.value):
            return {"processing_result": CallbackProcessingResult.DUPLICATE.value}

        if req.status == VerificationStatus.TIMED_OUT.value or (
            req.timeout_at and req.timeout_at <= datetime.now(timezone.utc)
        ):
            cb = VerificationCallback(
                verification_request_id=req.id,
                provider=provider,
                provider_event_id=provider_event_id,
                payload=self._redact(payload),
                outcome=outcome.value,
                processing_result=CallbackProcessingResult.LATE_IGNORED.value,
            )
            self.db.add(cb)
            self.sm.audit_only(
                req.application_id,
                actor=f"provider:{provider}",
                action="CALLBACK_LATE_IGNORED",
                payload={"correlation_id": correlation_id, "outcome": outcome.value},
            )
            self.db.commit()
            return {"processing_result": CallbackProcessingResult.LATE_IGNORED.value}

        if req.status == VerificationStatus.SUPERSEDED.value:
            cb = VerificationCallback(
                verification_request_id=req.id,
                provider=provider,
                provider_event_id=provider_event_id,
                payload=self._redact(payload),
                outcome=outcome.value,
                processing_result=CallbackProcessingResult.SUPERSEDED_IGNORED.value,
            )
            self.db.add(cb)
            self.sm.audit_only(
                req.application_id,
                actor=f"provider:{provider}",
                action="CALLBACK_SUPERSEDED_IGNORED",
                payload={"correlation_id": correlation_id},
            )
            self.db.commit()
            return {"processing_result": CallbackProcessingResult.SUPERSEDED_IGNORED.value}

        cb = VerificationCallback(
            verification_request_id=req.id,
            provider=provider,
            provider_event_id=provider_event_id,
            payload=self._redact(payload),
            outcome=outcome.value,
            processing_result=CallbackProcessingResult.APPLIED.value,
        )
        self.db.add(cb)

        if outcome == VerificationOutcome.PASS:
            req.status = VerificationStatus.SUCCEEDED.value
            req.outcome = VerificationOutcome.PASS.value
        elif outcome == VerificationOutcome.FAIL:
            req.status = VerificationStatus.FAILED.value
            req.outcome = VerificationOutcome.FAIL.value
        else:
            req.status = VerificationStatus.FAILED.value
            req.outcome = VerificationOutcome.INCONCLUSIVE.value

        self.sm.audit_only(
            req.application_id,
            actor=f"provider:{provider}",
            action="CALLBACK_RECEIVED",
            payload={
                "provider": provider,
                "outcome": outcome.value,
                "correlation_id": correlation_id,
            },
        )

        self.db.flush()
        self._evaluate_aggregate(req.application_id)
        self.db.commit()
        return {
            "processing_result": CallbackProcessingResult.APPLIED.value,
            "callback_id": str(cb.id),
        }

    def _app_id_by_correlation(self, correlation_id: str) -> UUID:
        req = (
            self.db.query(VerificationRequest)
            .filter(VerificationRequest.correlation_id == correlation_id)
            .first()
        )
        return req.application_id if req else uuid.UUID(int=0)

    def _redact(self, payload: dict[str, Any]) -> dict[str, Any]:
        redacted = dict(payload)
        for key in ("pan", "gstin", "account", "bank_account_number"):
            if key in redacted:
                redacted[key] = "***REDACTED***"
        return redacted

    def _evaluate_aggregate(self, application_id: UUID) -> None:
        app = self.db.get(Application, application_id)
        if app is None:
            return
        if app.status in (
            ApplicationStatus.APPROVED.value,
            ApplicationStatus.REJECTED.value,
            ApplicationStatus.FAILED.value,
            ApplicationStatus.PROVISIONING.value,
            ApplicationStatus.CHECKS_PASSED.value,
        ):
            return

        required = self.required_providers(app)
        # Latest non-superseded request per provider
        latest: dict[str, VerificationRequest] = {}
        reqs = (
            self.db.query(VerificationRequest)
            .filter(VerificationRequest.application_id == application_id)
            .order_by(VerificationRequest.created_at.desc())
            .all()
        )
        for r in reqs:
            if r.provider in latest:
                continue
            if r.status == VerificationStatus.SUPERSEDED.value:
                continue
            latest[r.provider] = r

        relevant = [latest[p] for p in required if p in latest]
        if not relevant:
            return

        done = [
            r
            for r in relevant
            if r.status
            in (
                VerificationStatus.SUCCEEDED.value,
                VerificationStatus.FAILED.value,
                VerificationStatus.TIMED_OUT.value,
            )
        ]
        pending = [
            r
            for r in relevant
            if r.status
            in (VerificationStatus.PENDING.value, VerificationStatus.SENT.value)
        ]

        if done and app.status == ApplicationStatus.AWAITING_CALLBACKS.value:
            self.sm.transition(
                application_id,
                ApplicationStatus.VERIFICATION_PARTIAL,
                actor="system",
                action="FIRST_CALLBACK_APPLIED",
                payload={"done": [r.provider for r in done]},
            )

        failed = [
            r
            for r in done
            if r.status == VerificationStatus.FAILED.value
            or r.status == VerificationStatus.TIMED_OUT.value
        ]
        if failed and not pending:
            reasons = []
            for r in failed:
                if r.provider == "GST":
                    reasons.append(ReviewReason.GST_FAIL.value)
                elif r.provider == "PAN":
                    reasons.append(ReviewReason.PAN_FAIL.value)
                elif r.provider == "BANK":
                    reasons.append(ReviewReason.BANK_FAIL.value)
                if r.status == VerificationStatus.TIMED_OUT.value:
                    reasons.append(ReviewReason.VERIFICATION_TIMEOUT.value)
            reasons = list(dict.fromkeys(reasons))
            self.sm.transition(
                application_id,
                ApplicationStatus.IN_REVIEW,
                actor="system",
                action="VERIFICATION_FAILED_ESCALATE",
                payload={"reasons": reasons},
            )
            self._open_review(app, reasons)
            return

        if failed and pending:
            # Wait for remaining but already know we'll escalate — stay partial
            if app.status != ApplicationStatus.VERIFICATION_PARTIAL.value:
                self.sm.transition(
                    application_id,
                    ApplicationStatus.VERIFICATION_PARTIAL,
                    actor="system",
                    action="VERIFICATION_PARTIAL_WITH_FAIL",
                )
            return

        all_pass = len(done) == len(required) and all(
            r.outcome == VerificationOutcome.PASS.value
            and r.status == VerificationStatus.SUCCEEDED.value
            for r in done
        )
        if all_pass and not pending:
            auto = (app.config_checks or {}).get("auto_provision_on_pass", True)
            cmds = [{"command": "PROVISION_MERCHANT", "payload": {}}] if auto else []
            self.sm.transition(
                application_id,
                ApplicationStatus.CHECKS_PASSED,
                actor="system",
                action="ALL_CHECKS_PASSED",
                payload={"providers": required},
                outbox_commands=cmds
                if auto
                else [],
            )
            if auto:
                # Immediately move to provisioning via next outbox; also transition now
                self.sm.transition(
                    application_id,
                    ApplicationStatus.PROVISIONING,
                    actor="system",
                    action="START_PROVISION",
                )
            return

        if pending and done:
            if app.status not in (
                ApplicationStatus.VERIFICATION_PARTIAL.value,
                ApplicationStatus.AWAITING_CALLBACKS.value,
            ):
                return
            if app.status == ApplicationStatus.AWAITING_CALLBACKS.value:
                self.sm.transition(
                    application_id,
                    ApplicationStatus.VERIFICATION_PARTIAL,
                    actor="system",
                    action="VERIFICATION_PARTIAL",
                )

    def handle_timeout(self, application_id: UUID) -> None:
        app = self.db.get(Application, application_id)
        if app is None:
            return
        if app.status not in (
            ApplicationStatus.AWAITING_CALLBACKS.value,
            ApplicationStatus.VERIFICATION_PARTIAL.value,
            ApplicationStatus.PENDING_VERIFICATION.value,
        ):
            return

        reqs = (
            self.db.query(VerificationRequest)
            .filter(
                VerificationRequest.application_id == application_id,
                VerificationRequest.status.in_(
                    [VerificationStatus.PENDING.value, VerificationStatus.SENT.value]
                ),
            )
            .all()
        )
        now = datetime.now(timezone.utc)
        timed = False
        for r in reqs:
            if r.timeout_at and r.timeout_at <= now:
                r.status = VerificationStatus.TIMED_OUT.value
                r.outcome = VerificationOutcome.INCONCLUSIVE.value
                timed = True

        if not timed and not reqs:
            return

        if timed:
            self.sm.transition(
                application_id,
                ApplicationStatus.IN_REVIEW,
                actor="system",
                action="VERIFICATION_TIMEOUT",
                payload={"reason": ReviewReason.VERIFICATION_TIMEOUT.value},
            )
            self._open_review(app, [ReviewReason.VERIFICATION_TIMEOUT.value])
            self.db.commit()

    def supersede_bank_and_reverify(self, application_id: UUID) -> None:
        app = self.db.get(Application, application_id)
        if app is None:
            return
        bank_reqs = (
            self.db.query(VerificationRequest)
            .filter(
                VerificationRequest.application_id == application_id,
                VerificationRequest.provider == VerificationProvider.BANK.value,
            )
            .all()
        )
        for r in bank_reqs:
            if r.status not in (
                VerificationStatus.SUPERSEDED.value,
                VerificationStatus.TIMED_OUT.value,
            ):
                r.status = VerificationStatus.SUPERSEDED.value

        target = ApplicationStatus.PENDING_VERIFICATION
        if app.status in (
            ApplicationStatus.CHECKS_PASSED.value,
            ApplicationStatus.PROVISIONING.value,
            ApplicationStatus.APPROVED.value,
        ):
            # APPROVED is terminal — shouldn't happen; for CHECKS_PASSED/PROVISIONING
            if app.status != ApplicationStatus.APPROVED.value:
                self.sm.transition(
                    application_id,
                    target,
                    actor="system",
                    action="BANK_CHANGED_REVERIFY",
                    outbox_commands=[{"command": "START_VERIFICATION", "payload": {}}],
                )
        elif app.status == ApplicationStatus.IN_REVIEW.value:
            self.sm.transition(
                application_id,
                target,
                actor="system",
                action="BANK_CHANGED_REVERIFY",
                outbox_commands=[{"command": "START_VERIFICATION", "payload": {}}],
            )
        self.db.commit()

    def _open_review(self, app: Application, reasons: list[str]) -> None:
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
            existing.status = ReviewCaseStatus.OPEN.value
            return
        self.db.add(
            ReviewCase(
                application_id=app.id,
                reason_codes=reasons,
                status=ReviewCaseStatus.OPEN.value,
                priority=80,
            )
        )
