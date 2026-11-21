"""Fixture A: M-001 clear docs → OCR → registry verify → APPROVED."""

from scripts.demo_lib import client, submit_fixture, wait_status


def main():
    print("=== Demo Happy Path (OCR + fixture registry) ===")
    with client() as c:
        app = submit_fixture(c, "M-001")
        detail = wait_status(
            c, app["id"], {"APPROVED", "IN_REVIEW", "MISSING_INFO", "FAILED"}, timeout=120
        )
        print(f"\nFinal status: {detail['status']}")
        for e in detail.get("extractions") or []:
            print(f"  extraction: {e['extractor']} {e['status']} conf={e['overall_confidence']}")
        if detail.get("merchant"):
            print(f"Merchant ID: {detail['merchant']['id']}")
        if detail["status"] != "APPROVED":
            raise SystemExit(f"Expected APPROVED, got {detail['status']}")
        print("\n✓ Happy path complete")


if __name__ == "__main__":
    main()
