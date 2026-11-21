"""Blurred PAN/GST images → quality gate → MISSING_INFO (re-upload)."""

from scripts.demo_lib import client, submit_fixture, wait_status


def main():
    print("=== Demo Blurred Documents ===")
    with client() as c:
        app = submit_fixture(c, "M-004")
        detail = wait_status(
            c, app["id"], {"MISSING_INFO", "IN_REVIEW", "APPROVED", "FAILED"}, timeout=60
        )
        print(f"\nFinal status: {detail['status']}")
        print(f"missing_info: {detail.get('missing_info')}")
        for e in detail.get("extractions") or []:
            print(f"  extraction: {e['status']} conflicts={e.get('conflicts')}")
        for case in detail.get("review_cases") or []:
            print(f"Review: {case['reason_codes']} ({case['status']})")
        if detail["status"] != "MISSING_INFO":
            raise SystemExit(f"Expected MISSING_INFO, got {detail['status']}")
        reasons = []
        for case in detail.get("review_cases") or []:
            reasons.extend(case.get("reason_codes") or [])
        if "DOC_BLURRED" not in reasons and not any(
            (m or {}).get("quality_reason") == "BLURRED"
            for m in (detail.get("missing_info") or [])
        ):
            raise SystemExit(f"Expected DOC_BLURRED signal, got {reasons}")
        print("\n✓ Blurred docs correctly blocked with re-upload request")


if __name__ == "__main__":
    main()
