"""Poll transactional outbox and dispatch Celery tasks."""

import logging
import time
from datetime import datetime, timezone

from sqlalchemy import select

from apps.api.db.models import OutboxMessage
from apps.api.db.session import SessionLocal
from apps.worker.tasks import COMMAND_TASKS
from shared.enums import OutboxStatus

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("outbox_relay")


def process_batch(limit: int = 20) -> int:
    db = SessionLocal()
    processed = 0
    try:
        now = datetime.now(timezone.utc)
        rows = (
            db.execute(
                select(OutboxMessage)
                .where(
                    OutboxMessage.status == OutboxStatus.PENDING.value,
                    OutboxMessage.available_at <= now,
                )
                .order_by(OutboxMessage.created_at.asc())
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            .scalars()
            .all()
        )
        for msg in rows:
            handler = COMMAND_TASKS.get(msg.command)
            if handler is None:
                msg.status = OutboxStatus.FAILED.value
                msg.last_error = f"Unknown command {msg.command}"
                continue
            try:
                msg.status = OutboxStatus.PROCESSING.value
                msg.attempts += 1
                db.flush()
                payload = dict(msg.payload or {})
                if "application_id" not in payload and msg.application_id:
                    payload["application_id"] = str(msg.application_id)
                handler(payload)
                msg.status = OutboxStatus.PROCESSED.value
                msg.processed_at = datetime.now(timezone.utc)
                processed += 1
            except Exception as exc:
                logger.exception("outbox dispatch failed: %s", msg.command)
                msg.last_error = str(exc)
                if msg.attempts >= 5:
                    msg.status = OutboxStatus.FAILED.value
                else:
                    msg.status = OutboxStatus.PENDING.value
        db.commit()
    except Exception:
        logger.exception("outbox batch failed")
        db.rollback()
    finally:
        db.close()
    return processed


def main():
    logger.info("Outbox relay started")
    while True:
        n = process_batch()
        time.sleep(0.5 if n else 1.0)


if __name__ == "__main__":
    main()
