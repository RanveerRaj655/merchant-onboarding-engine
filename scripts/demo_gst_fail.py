"""Fixture B: M-002 GST CANCELLED in registry → IN_REVIEW."""

from scripts.demo_lib import client, submit_fixture, wait_status


def main():
    print("=== Demo GST Fail (registry status CANCELLED) ===")
    with client() as c:
        app = submit_fixture(c, "M-002")
        detail = wait_status(
            c, app["id"], {"IN_REVIEW", "APPROVED", "MISSING_INFO", "FAILED"}, timeout=120
        )
        print(f"\nFinal status: {detail['status']}")
        for case in detail.get("review_cases") or []:
            print(f"Review: {case['reason_codes']} ({case['status']})")
        for v in detail.get("verifications") or []:
            print(f"  {v['provider']}: {v['status']} / {v.get('outcome')}")
        if detail["status"] != "IN_REVIEW":
            raise SystemExit(f"Expected IN_REVIEW, got {detail['status']}")

        open_case = next(
            (x for x in (detail.get("review_cases") or []) if x["status"] in ("OPEN", "IN_REVIEW")),
            None,
        )
        if open_case:
            r = c.post(
                f"/api/v1/ops/review-cases/{open_case['id']}/actions",
                json={
                    "action": "reject",
                    "note": "GST not active in registry",
                    "version": detail["version"],
                },
                headers={"Idempotency-Key": f"demo-reject-{app['id']}"},
            )
            r.raise_for_status()
            print(f"After reject: {r.json()['status']}")
        print("\n✓ GST fail path complete")


if __name__ == "__main__":
    main()
