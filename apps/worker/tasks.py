import logging
from uuid import UUID

import httpx
from celery.utils.log import get_task_logger

from apps.api.config import get_settings
from apps.api.db.models import Application, FailedJob, VerificationRequest
from apps.api.db.session import SessionLocal
from apps.api.services.extraction import ExtractionService
from apps.api.services.provisioning import ProvisioningService
from apps.api.services.state_machine import StateMachineService, TransitionError
from apps.api.services.verification import VerificationOrchestrator
from apps.worker.celery_app import celery_app
from shared.enums import ApplicationStatus, VerificationStatus

logger = get_task_logger(__name__)
settings = get_settings()


@celery_app.task(name="amop.start_extraction", bind=True, max_retries=3)
def start_extraction(self, application_id: str):
    db = SessionLocal()
    try:
        ExtractionService(db).run_for_application(UUID(application_id))
    except Exception as exc:
        logger.exception("extraction failed")
        db.rollback()
        raise self.retry(exc=exc, countdown=2**self.request.retries)
    finally:
        db.close()


@celery_app.task(name="amop.start_verification", bind=True, max_retries=3)
def start_verification(self, application_id: str):
    db = SessionLocal()
    try:
        VerificationOrchestrator(db).start_verification(UUID(application_id))
    except Exception as exc:
        logger.exception("start_verification failed")
        db.rollback()
        raise self.retry(exc=exc, countdown=2**self.request.retries)
    finally:
        db.close()


@celery_app.task(name="amop.send_verification", bind=True, max_retries=3)
def send_verification(self, verification_request_id: str):
    db = SessionLocal()
    try:
        req = db.get(VerificationRequest, UUID(verification_request_id))
        if req is None:
            return
        if req.status not in (
            VerificationStatus.PENDING.value,
            VerificationStatus.SENT.value,
        ):
            return

        callback_url = (
            f"{settings.api_base_url}/webhooks/verifications/{req.provider.lower()}"
        )
        body = {
            "correlation_id": req.correlation_id,
            "idempotency_key": req.idempotency_key,
            "callback_url": callback_url,
            "payload": req.request_payload,
        }
        url = f"{settings.mock_provider_url}/verify/{req.provider.lower()}"
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                url,
                json=body,
                headers={"X-Webhook-Secret": settings.webhook_secret},
            )
            if resp.status_code == 409:
                req.status = VerificationStatus.SENT.value
                db.commit()
                return
            resp.raise_for_status()

        req.status = VerificationStatus.SENT.value
        StateMachineService(db).audit_only(
            req.application_id,
            actor="system",
            action="VERIFICATION_SENT",
            payload={
                "provider": req.provider,
                "correlation_id": req.correlation_id,
            },
        )
        db.commit()
    except Exception as exc:
        logger.exception("send_verification failed")
        db.rollback()
        if self.request.retries >= self.max_retries:
            _mark_send_exhausted(verification_request_id, str(exc))
            return
        raise self.retry(exc=exc, countdown=2**self.request.retries)
    finally:
        db.close()


def _mark_send_exhausted(verification_request_id: str, error: str) -> None:
    db = SessionLocal()
    try:
        req = db.get(VerificationRequest, UUID(verification_request_id))
        if not req:
            return
        req.status = VerificationStatus.FAILED.value
        app = db.get(Application, req.application_id)
        if app:
            orch = VerificationOrchestrator(db)
            orch._open_review(app, [f"{req.provider}_FAIL"])
            try:
                StateMachineService(db).transition(
                    req.application_id,
                    ApplicationStatus.IN_REVIEW,
                    actor="system",
                    action="VERIFICATION_SEND_EXHAUSTED",
                    payload={"error": error, "provider": req.provider},
                )
            except TransitionError:
                pass
        db.add(
            FailedJob(
                task_name="send_verification",
                payload={"verification_request_id": verification_request_id},
                error=error,
            )
        )
        db.commit()
    except Exception:
        logging.exception("failed job record error")
        db.rollback()
    finally:
        db.close()


@celery_app.task(name="amop.verification_timeout")
def verification_timeout(application_id: str):
    db = SessionLocal()
    try:
        VerificationOrchestrator(db).handle_timeout(UUID(application_id))
    finally:
        db.close()


@celery_app.task(name="amop.provision_merchant", bind=True, max_retries=3)
def provision_merchant(self, application_id: str):
    db = SessionLocal()
    try:
        ProvisioningService(db).provision(UUID(application_id))
    except Exception as exc:
        logger.exception("provision failed")
        db.rollback()
        raise self.retry(exc=exc, countdown=2**self.request.retries)
    finally:
        db.close()


COMMAND_TASKS = {
    "START_EXTRACTION": lambda p: start_extraction.delay(p["application_id"]),
    "START_VERIFICATION": lambda p: start_verification.delay(p["application_id"]),
    "SEND_VERIFICATION": lambda p: send_verification.delay(p["verification_request_id"]),
    "VERIFICATION_TIMEOUT": lambda p: verification_timeout.delay(p["application_id"]),
    "PROVISION_MERCHANT": lambda p: provision_merchant.delay(p["application_id"]),
}
