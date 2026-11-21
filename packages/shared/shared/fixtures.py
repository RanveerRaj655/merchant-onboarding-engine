"""Load synthetic merchant registry from fixtures/merchants.json."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
FIXTURES_PATH = ROOT / "fixtures" / "merchants.json"


@lru_cache
def load_registry() -> list[dict[str, Any]]:
    if not FIXTURES_PATH.exists():
        return []
    data = json.loads(FIXTURES_PATH.read_text())
    return list(data.get("registry", []))


def get_merchant(merchant_id: str) -> dict[str, Any] | None:
    for m in load_registry():
        if m["id"] == merchant_id:
            return m
    return None


def lookup_pan(pan: str) -> dict[str, Any] | None:
    key = (pan or "").strip().upper()
    for m in load_registry():
        if m["pan"].upper() == key:
            return m
    return None


def lookup_gstin(gstin: str) -> dict[str, Any] | None:
    key = (gstin or "").strip().upper()
    for m in load_registry():
        if m["gstin"].upper() == key:
            return m
    return None


def lookup_bank(account: str, ifsc: str) -> dict[str, Any] | None:
    acc = (account or "").strip()
    code = (ifsc or "").strip().upper()
    for m in load_registry():
        if m["bank_account_number"] == acc and m["ifsc"].upper() == code:
            return m
    return None


def fixture_doc_path(relative: str) -> Path:
    return ROOT / "fixtures" / relative
