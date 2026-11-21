from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

from apps.api.services.verification import VerificationOrchestrator


def orchestrator(status="AWAITING_CALLBACKS"):
    db = MagicMock()
    app = SimpleNamespace(id=uuid4(), status=status, config_checks={},
                          bank_account_raw=None, ifsc=None)
    db.get.return_value = app
    service = VerificationOrchestrator(db)
    service.sm = MagicMock()
    return service, db, app


def test_duplicate_dispatch_does_not_supersede_requests():
    service, db, app = orchestrator()
    service.start_verification(app.id)
    db.add.assert_not_called()
    service.sm.transition.assert_not_called()


def test_old_timeout_does_not_expire_new_attempt():
    service, db, app = orchestrator()
    req = SimpleNamespace(status="SENT", timeout_at=datetime.now(timezone.utc) + timedelta(hours=1))
    db.query.return_value.filter.return_value.all.return_value = [req]
    service.handle_timeout(app.id)
    assert req.status == "SENT"
    service.sm.transition.assert_not_called()


def test_inconclusive_escalates():
    service, db, app = orchestrator("VERIFICATION_PARTIAL")
    requests = [
        SimpleNamespace(provider="PAN", status="SUCCEEDED", outcome="PASS"),
        SimpleNamespace(provider="GST", status="FAILED", outcome="INCONCLUSIVE"),
    ]
    db.query.return_value.filter.return_value.order_by.return_value.all.return_value = requests
    service._open_review = MagicMock()
    service._evaluate_aggregate(app.id)
    service._open_review.assert_called_once()
    assert service.sm.transition.call_args.args[1].value == "IN_REVIEW"


def test_unknown_duplicate_has_no_invalid_audit_foreign_key():
    service, db, app = orchestrator()
    db.query.return_value.filter.return_value.first.return_value = SimpleNamespace(
        verification_request=None)
    result = service.apply_callback("PAN", {
        "provider_event_id": "unknown", "correlation_id": "unknown", "outcome": "PASS"})
    assert result["processing_result"] == "DUPLICATE"
    service.sm.audit_only.assert_not_called()
