from celery import Celery

from apps.api.config import get_settings

settings = get_settings()

celery_app = Celery(
    "amop",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["apps.worker.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_default_retry_delay=2,
    broker_connection_retry_on_startup=True,
)
