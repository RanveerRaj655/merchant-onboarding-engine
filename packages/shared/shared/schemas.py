from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from shared.enums import (
    ApplicationStatus,
    CallbackProcessingResult,
    DocumentSource,
    DocumentType,
    ExtractionStatus,
    ReviewCaseStatus,
    ReviewResolution,
    VerificationOutcome,
    VerificationProvider,
    VerificationStatus,
)


class ConfigChecks(BaseModel):
    required_verifications: list[str] = Field(default_factory=lambda: ["PAN", "GST"])
    require_bank_if_present: bool = True
    auto_provision_on_pass: bool = True
    extraction_confidence_min: float = 0.7
    verification_timeout_ms: int = 30000
    scenario: str | None = None  # demo fixture: happy | gst_fail | pan_fail | timeout


class ApplicationCreate(BaseModel):
    legal_name: str
    trade_name: str | None = None
    # Optional when PAN/GST images are uploaded — OCR fills them during extraction
    pan: str = ""
    gstin: str = ""
    bank_account_number: str | None = None
    ifsc: str | None = None
    contact_email: str
    contact_phone: str | None = None
    business_category: str | None = None
    config_checks: ConfigChecks = Field(default_factory=ConfigChecks)


class ApplicationUpdate(BaseModel):
    legal_name: str | None = None
    trade_name: str | None = None
    pan: str | None = None
    gstin: str | None = None
    bank_account_number: str | None = None
    ifsc: str | None = None
    contact_email: str | None = None
    contact_phone: str | None = None
    business_category: str | None = None
    version: int


class StructuredDocumentPayload(BaseModel):
    document_type: DocumentType
    fields: dict[str, Any]
    confidence: float | None = 1.0
    unreadable: bool = False


class ExtractedField(BaseModel):
    value: Any
    confidence: float
    source: str = "STRUCTURED"


class VerificationCallbackPayload(BaseModel):
    correlation_id: str
    provider_event_id: str
    outcome: VerificationOutcome
    details: dict[str, Any] = Field(default_factory=dict)
    scenario: str | None = None


class ReviewActionRequest(BaseModel):
    action: str  # approve | reject | request_info | reverify
    note: str
    missing_info: list[dict[str, Any]] | None = None
    version: int


class ApplicationSummary(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    reference: str
    status: ApplicationStatus
    legal_name: str
    trade_name: str | None
    pan_masked: str | None
    gstin_masked: str | None
    bank_account_masked: str | None
    ifsc: str | None
    contact_email: str
    missing_info: list[Any] | None
    config_checks: dict[str, Any]
    version: int
    submitted_at: datetime | None
    updated_at: datetime


class AuditEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    actor: str
    action: str
    from_status: str | None
    to_status: str | None
    payload: dict[str, Any]
    created_at: datetime


class VerificationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    provider: VerificationProvider
    status: VerificationStatus
    attempt: int
    correlation_id: str
    timeout_at: datetime | None
    outcome: VerificationOutcome | None = None


class ReviewCaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    application_id: UUID
    reason_codes: list[str]
    status: ReviewCaseStatus
    priority: int
    notes: str | None
    resolution: ReviewResolution | None
    created_at: datetime
