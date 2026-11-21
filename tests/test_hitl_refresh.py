from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from uuid import uuid4

import pytest
from apps.api.routes.ui import _detail_context, templates


@pytest.mark.parametrize("status,poll", [
    ("IN_REVIEW", False), ("MISSING_INFO", False), ("EXTRACTION_FAILED", False),
    ("APPROVED", False), ("REJECTED", False), ("FAILED", False),
    ("CHECKS_PASSED", False), ("AWAITING_CALLBACKS", True),
    ("VERIFICATION_PARTIAL", True), ("PROVISIONING", True), ("EXTRACTING", True),
])
def test_refresh_only_during_background_work(status, poll):
    db = MagicMock()
    db.get.return_value = SimpleNamespace(status=status, legal_name="Example",
        pan_raw="AABCO5678B", gstin_raw="29AABCO5678B1Z2",
        bank_account_raw="", ifsc="")
    db.query.return_value.filter.return_value.order_by.return_value.first.return_value = SimpleNamespace(reason_codes=["GST_FAIL"])
    with patch("apps.api.routes.ui._application_detail", return_value={"status": status}):
        ctx = _detail_context(db, uuid4())
    assert ctx["poll_live"] is poll
    if status in {"IN_REVIEW", "MISSING_INFO", "EXTRACTION_FAILED"}:
        assert ctx["hitl_defaults"]["gstin"] == "29AABCO5678B1Z2"
        assert ctx["hitl_defaults"]["pan"] == "AABCO5678B"
