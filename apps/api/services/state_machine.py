from datetime import datetime, timezone
from typing import Any
from uuid import UUID

from sqlalchemy.orm import Session

from apps.api.db.models import Application, AuditEvent, OutboxMessage
from shared.enums import ApplicationStatus, OutboxStatus
from shared.transitions import can_transition


class TransitionError(Exception):
    def __init__(self, message: str):
        self.message = message
        super().__init__(message)


class StateMachineService:
    """Sole writer of application.status — validates transitions, audit + outbox."""

    def __init__(self, db: Session):
        self.db = db

    def transition(
        self,
        application_id: UUID,
        to_status: ApplicationStatus | str,
        *,
        actor: str = "system",
        action: str = "STATUS_TRANSITION",
        payload: dict[str, Any] | None = None,
        outbox_commands: list[dict[str, Any]] | None = None,
        expected_version: int | None = None,
    ) -> Application:
        app = self.db.get(Application, application_id)
        if app is None:
            raise TransitionError(f"Application {application_id} not found")

        if expected_version is not None and app.version != expected_version:
            raise TransitionError(
                f"Version conflict: expected {expected_version}, got {app.version}"
            )

        to_status = ApplicationStatus(to_status)
        from_status = ApplicationStatus(app.status)

        if from_status == to_status:
            # Allow no-op only when explicitly same (e.g. VERIFICATION_PARTIAL refresh)
            if to_status != ApplicationStatus.VERIFICATION_PARTIAL:
                return app

        if not can_transition(from_status, to_status) and from_status != to_status:
            raise TransitionError(f"Illegal transition {from_status} → {to_status}")

        now = datetime.now(timezone.utc)
        app.status = to_status.value
        app.status_entered_at = now
        app.updated_at = now
        app.version += 1

        audit = AuditEvent(
            application_id=app.id,
            actor=actor,
            action=action,
            from_status=from_status.value,
            to_status=to_status.value,
            payload=payload or {},
        )
        self.db.add(audit)

        for cmd in outbox_commands or []:
            self.db.add(
                OutboxMessage(
                    application_id=app.id,
                    command=cmd["command"],
                    payload={**cmd.get("payload", {}), "application_id": str(app.id)},
                    status=OutboxStatus.PENDING.value,
                    available_at=cmd.get("available_at", now),
                )
            )

        self.db.flush()
        return app

    def audit_only(
        self,
        application_id: UUID,
        *,
        actor: str,
        action: str,
        payload: dict[str, Any] | None = None,
        from_status: str | None = None,
        to_status: str | None = None,
    ) -> AuditEvent:
        event = AuditEvent(
            application_id=application_id,
            actor=actor,
            action=action,
            from_status=from_status,
            to_status=to_status,
            payload=payload or {},
        )
        self.db.add(event)
        self.db.flush()
        return event
