"""initial schema

Revision ID: 001
Revises:
Create Date: 2026-11-21
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "applications",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("reference", sa.String(32), unique=True, nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("legal_name", sa.String(255), nullable=False),
        sa.Column("trade_name", sa.String(255)),
        sa.Column("pan_raw", sa.String(20)),
        sa.Column("pan_masked", sa.String(20)),
        sa.Column("pan_hash", sa.String(64)),
        sa.Column("gstin_raw", sa.String(20)),
        sa.Column("gstin_masked", sa.String(20)),
        sa.Column("gstin_hash", sa.String(64)),
        sa.Column("bank_account_raw", sa.String(40)),
        sa.Column("bank_account_masked", sa.String(40)),
        sa.Column("bank_account_hash", sa.String(64)),
        sa.Column("ifsc", sa.String(20)),
        sa.Column("contact_email", sa.String(255), nullable=False),
        sa.Column("contact_phone", sa.String(40)),
        sa.Column("business_category", sa.String(100)),
        sa.Column("missing_info", postgresql.JSONB()),
        sa.Column("config_checks", postgresql.JSONB(), nullable=False),
        sa.Column("content_fingerprint", sa.String(64)),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status_entered_at", sa.DateTime(timezone=True)),
        sa.Column("submitted_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "documents",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("application_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("applications.id"), nullable=False),
        sa.Column("type", sa.String(40), nullable=False),
        sa.Column("storage_key", sa.String(512)),
        sa.Column("content_type", sa.String(100)),
        sa.Column("byte_size", sa.Integer()),
        sa.Column("source", sa.String(40), nullable=False),
        sa.Column("sha256", sa.String(64)),
        sa.Column("structured_payload", postgresql.JSONB()),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "extraction_results",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("document_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("documents.id"), nullable=False),
        sa.Column("application_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("applications.id"), nullable=False),
        sa.Column("fields", postgresql.JSONB(), nullable=False),
        sa.Column("overall_confidence", sa.Float(), nullable=False),
        sa.Column("conflicts", postgresql.JSONB()),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("extractor", sa.String(40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "verification_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("application_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("applications.id"), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("request_payload", postgresql.JSONB(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("timeout_at", sa.DateTime(timezone=True)),
        sa.Column("correlation_id", sa.String(64), unique=True, nullable=False),
        sa.Column("outcome", sa.String(20)),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("idempotency_key", name="uq_verification_idempotency"),
    )
    op.create_table(
        "verification_callbacks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("verification_request_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("verification_requests.id")),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("provider_event_id", sa.String(128), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True)),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("outcome", sa.String(20), nullable=False),
        sa.Column("processing_result", sa.String(40), nullable=False),
        sa.UniqueConstraint("provider", "provider_event_id", name="uq_provider_event"),
    )
    op.create_table(
        "review_cases",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("application_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("applications.id"), nullable=False),
        sa.Column("reason_codes", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(40), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("assignee", sa.String(100)),
        sa.Column("notes", sa.Text()),
        sa.Column("resolution", sa.String(40)),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("updated_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "merchant_accounts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("application_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("applications.id"), unique=True, nullable=False),
        sa.Column("capabilities", postgresql.JSONB(), nullable=False),
        sa.Column("provisioning_status", sa.String(40), nullable=False),
        sa.Column("fail_once", sa.Boolean()),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("application_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("applications.id"), nullable=False),
        sa.Column("actor", sa.String(100), nullable=False),
        sa.Column("action", sa.String(80), nullable=False),
        sa.Column("from_status", sa.String(40)),
        sa.Column("to_status", sa.String(40)),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "outbox_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("application_id", postgresql.UUID(as_uuid=True)),
        sa.Column("command", sa.String(80), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True)),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True)),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
    )
    op.create_table(
        "failed_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("task_name", sa.String(120), nullable=False),
        sa.Column("payload", postgresql.JSONB(), nullable=False),
        sa.Column("error", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True)),
    )
    op.create_index("ix_applications_status", "applications", ["status"])
    op.create_index("ix_outbox_status_available", "outbox_messages", ["status", "available_at"])


def downgrade() -> None:
    op.drop_table("failed_jobs")
    op.drop_table("outbox_messages")
    op.drop_table("audit_events")
    op.drop_table("merchant_accounts")
    op.drop_table("review_cases")
    op.drop_table("verification_callbacks")
    op.drop_table("verification_requests")
    op.drop_table("extraction_results")
    op.drop_table("documents")
    op.drop_table("applications")
