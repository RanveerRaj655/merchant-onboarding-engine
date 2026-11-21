import time
from typing import Any

import httpx

API = "http://localhost:8000"
SECRET = "demo-webhook-secret"
HEADERS = {"Authorization": "Bearer demo-operator-token"}


def client() -> httpx.Client:
    return httpx.Client(base_url=API, timeout=30.0, headers=HEADERS)


def wait_status(
    c: httpx.Client,
    app_id: str,
    targets: set[str],
    timeout: float = 90.0,
) -> dict[str, Any]:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        r = c.get(f"/api/v1/ops/applications/{app_id}")
        r.raise_for_status()
        last = r.json()
        status = last["status"]
        print(f"  … status={status}")
        if status in targets:
            return last
        time.sleep(1.5)
    raise TimeoutError(f"Timed out waiting for {targets}; last={last.get('status')}")


def submit_fixture(c: httpx.Client, merchant_id: str) -> dict[str, Any]:
    r = c.post(f"/api/v1/fixtures/merchants/{merchant_id}/submit")
    r.raise_for_status()
    app = r.json()
    print(f"Submitted fixture {merchant_id} → {app['reference']} ({app['id']})")
    return app


def submit_with_docs(
    c: httpx.Client,
    *,
    scenario: str | None,
    legal_name: str = "Demo Merchant Pvt Ltd",
    pan: str = "AABCA1234A",
    gstin: str = "27AABCA1234A1Z5",
    bank: str = "50100123456789",
    ifsc: str = "HDFC0001234",
) -> dict[str, Any]:
    """Legacy structured-payload path (still works). Prefer submit_fixture()."""
    cfg: dict[str, Any] = {
        "required_verifications": ["PAN", "GST"],
        "require_bank_if_present": True,
        "auto_provision_on_pass": True,
        "extraction_confidence_min": 0.7,
        "verification_timeout_ms": 30000,
    }
    if scenario:
        cfg["scenario"] = scenario
        if scenario == "timeout":
            cfg["verification_timeout_ms"] = 8000

    r = c.post(
        "/api/v1/applications",
        json={
            "legal_name": legal_name,
            "trade_name": "Demo Store",
            "pan": pan,
            "gstin": gstin,
            "bank_account_number": bank,
            "ifsc": ifsc,
            "contact_email": "demo@example.com",
            "contact_phone": "+919999999999",
            "business_category": "retail",
            "config_checks": cfg,
        },
    )
    r.raise_for_status()
    app = r.json()
    app_id = app["id"]
    print(f"Created {app['reference']} ({app_id}) scenario={scenario}")

    for dtype, fields in [
        ("PAN_CARD", {"pan": pan, "name": legal_name}),
        ("GST_CERT", {"gstin": gstin, "legal_name": legal_name}),
        ("CANCELLED_CHEQUE", {"bank_account_number": bank, "ifsc": ifsc}),
    ]:
        r = c.post(
            f"/api/v1/applications/{app_id}/documents/structured",
            json={"document_type": dtype, "fields": fields, "confidence": 1.0},
        )
        r.raise_for_status()

    r = c.post(f"/api/v1/applications/{app_id}/process")
    r.raise_for_status()
    print("  pipeline kicked off")
    return app
