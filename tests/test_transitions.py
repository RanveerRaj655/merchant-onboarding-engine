from shared.enums import ApplicationStatus as S
from shared.transitions import can_transition


def test_happy_path_transitions():
    assert can_transition(S.SUBMITTED, S.EXTRACTING)
    assert can_transition(S.EXTRACTING, S.PENDING_VERIFICATION)
    assert can_transition(S.PENDING_VERIFICATION, S.AWAITING_CALLBACKS)
    assert can_transition(S.AWAITING_CALLBACKS, S.VERIFICATION_PARTIAL)
    assert can_transition(S.VERIFICATION_PARTIAL, S.CHECKS_PASSED)
    assert can_transition(S.CHECKS_PASSED, S.PROVISIONING)
    assert can_transition(S.PROVISIONING, S.APPROVED)


def test_illegal_transition():
    assert not can_transition(S.APPROVED, S.SUBMITTED)
    assert not can_transition(S.REJECTED, S.APPROVED)


def test_hitl_paths():
    assert can_transition(S.EXTRACTING, S.IN_REVIEW)
    assert can_transition(S.VERIFICATION_PARTIAL, S.IN_REVIEW)
    assert can_transition(S.IN_REVIEW, S.REJECTED)
    assert can_transition(S.IN_REVIEW, S.CHECKS_PASSED)
    assert can_transition(S.IN_REVIEW, S.MISSING_INFO)


def test_masking():
    from shared.masking import mask_bank_account, mask_gstin, mask_pan

    assert mask_pan("ABCDE1234F").endswith("1234F")
    assert "*" in mask_pan("ABCDE1234F")
    assert mask_gstin("22ABCDE1234F1Z5").startswith("22")
    assert mask_bank_account("123456789012").endswith("9012")
