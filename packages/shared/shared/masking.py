import hashlib
import re


def mask_pan(pan: str | None) -> str | None:
    if not pan:
        return None
    pan = pan.strip().upper()
    if len(pan) < 5:
        return "*****"
    return f"{'*' * (len(pan) - 5)}{pan[-5:]}"


def mask_gstin(gstin: str | None) -> str | None:
    if not gstin:
        return None
    gstin = gstin.strip().upper()
    if len(gstin) < 4:
        return "****"
    return f"{gstin[:2]}{'*' * (len(gstin) - 4)}{gstin[-2:]}"


def mask_bank_account(account: str | None) -> str | None:
    if not account:
        return None
    account = re.sub(r"\s+", "", account)
    if len(account) < 4:
        return "****"
    return f"{'*' * (len(account) - 4)}{account[-4:]}"


def hash_sensitive(value: str | None, pepper: str) -> str | None:
    if not value:
        return None
    normalized = value.strip().upper()
    return hashlib.sha256(f"{pepper}:{normalized}".encode()).hexdigest()


def content_fingerprint(*parts: str | None) -> str:
    joined = "|".join((p or "").strip().upper() for p in parts)
    return hashlib.sha256(joined.encode()).hexdigest()[:32]
