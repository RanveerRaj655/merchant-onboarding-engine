"""Blacked-out PAN/GST images → quality gate → MISSING_INFO (re-upload)."""

from scripts.demo_lib import client, submit_fixture, wait_status


def main():
    print("=== Demo Blacked-Out Documents ===")
    with client() as c:
        app = submit_fixture(c, "M-005")
        detail = wait_status(
            c, app["id"], {"MISSING_INFO", "IN_REVIEW", "APPROVED", "FAILED"}, timeout=60
        )
        print(f"\nFinal status: {detail['status']}")
        print(f"missing_info: {detail.get('missing_info')}")
        for case in detail.get("review_cases") or []:
            print(f"Review: {case['reason_codes']} ({case['status']})")
        if detail["status"] != "MISSING_INFO":
            raise SystemExit(f"Expected MISSING_INFO, got {detail['status']}")
        print("\n✓ Blacked-out docs correctly blocked with re-upload request")


if __name__ == "__main__":
    main()
