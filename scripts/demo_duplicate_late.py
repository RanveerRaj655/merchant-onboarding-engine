"""Demonstrate duplicate + late callback handling."""

import time
import uuid

from scripts.demo_lib import SECRET, client, submit_with_docs, wait_status


def main():
    print("=== Demo Duplicate + Late Callbacks ===")
    with client() as c:
        app = submit_with_docs(c, scenario=None, legal_name="Callback Edge Cases Pvt Ltd")
        detail = wait_status(c, app["id"], {"APPROVED", "IN_REVIEW"}, timeout=90)
        print(f"Reached {detail['status']}")

        # Pick a PAN verification correlation for simulate
        pan = next(v for v in detail["verifications"] if v["provider"] == "PAN")
        corr = pan["correlation_id"]

        print("Simulating duplicate callback…")
        r = c.post(
            "/webhooks/verifications/pan/simulate",
            json={
                "correlation_id": corr,
                "outcome": "PASS",
                "provider_event_id": f"dup-{uuid.uuid4()}",
                "mode": "duplicate",
            },
            headers={"X-Webhook-Secret": SECRET},
        )
        r.raise_for_status()
        print(r.json())

        print("Simulating late callback after forced timeout mark…")
        r = c.post(
            "/webhooks/verifications/gst/simulate",
            json={
                "correlation_id": next(
                    v["correlation_id"] for v in detail["verifications"] if v["provider"] == "GST"
                ),
                "outcome": "PASS",
                "mode": "late",
            },
            headers={"X-Webhook-Secret": SECRET},
        )
        r.raise_for_status()
        print(r.json())

        time.sleep(1)
        detail = c.get(f"/api/v1/ops/applications/{app['id']}").json()
        results = {cb["processing_result"] for cb in detail.get("callbacks") or []}
        print(f"Callback dispositions seen: {results}")
        print("✓ Duplicate/late fixtures exercised")


if __name__ == "__main__":
    main()
