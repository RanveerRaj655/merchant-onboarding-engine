"""GST fail → HITL correct identity → async re-verify → APPROVED."""

from scripts.demo_lib import client, submit_fixture, wait_status


def main():
    print("=== Demo HITL recover after GST fail ===")
    with client() as c:
        app = submit_fixture(c, "M-002")
        detail = wait_status(c, app["id"], {"IN_REVIEW", "APPROVED", "FAILED"}, timeout=120)
        print(f"After verify: {detail['status']}")
        if detail["status"] != "IN_REVIEW":
            raise SystemExit(f"Expected IN_REVIEW, got {detail['status']}")

        r = c.post(
            f"/ops/applications/{app['id']}/hitl-correct",
            data={
                "note": "Corrected GSTIN/PAN to ACTIVE registry merchant",
                "mode": "verify",
                "legal_name": "Orbit Traders Private Limited",
                "pan": "AABCA1234A",
                "gstin": "27AABCA1234A1Z5",
                "bank_account_number": "50100123456789",
                "ifsc": "HDFC0001234",
            },
            follow_redirects=False,
        )
        print(f"HITL correct HTTP {r.status_code}")
        if r.status_code not in (302, 303, 200):
            print(r.text[:500])
            raise SystemExit("HITL correct failed")

        detail = wait_status(
            c, app["id"], {"APPROVED", "IN_REVIEW", "FAILED", "MISSING_INFO"}, timeout=120
        )
        print(f"After HITL re-verify: {detail['status']}")
        for v in detail.get("verifications") or []:
            print(f"  {v['provider']}: {v['status']} / {v.get('outcome')} attempt={v['attempt']}")
        if detail["status"] != "APPROVED":
            raise SystemExit(f"Expected APPROVED after HITL, got {detail['status']}")
        print("✓ HITL correction → async re-verify → APPROVED")


if __name__ == "__main__":
    main()
