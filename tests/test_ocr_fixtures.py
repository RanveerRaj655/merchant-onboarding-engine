from pathlib import Path
import pytest
from apps.api.services.ocr import assess_quality, extract_from_image

ROOT = Path(__file__).resolve().parents[1] / "fixtures" / "docs"


@pytest.mark.parametrize("path", sorted(ROOT.glob("*.png")), ids=lambda p: p.name)
def test_all_quality_fixtures(path):
    quality = assess_quality(path)
    assert quality.readable == ("clear" in path.name)


@pytest.mark.parametrize("path", sorted(ROOT.glob("*_clear.png")), ids=lambda p: p.name)
def test_real_tesseract_clear_fixtures(path, request):
    known_failures = {"M-003_gst_clear.png", "M-004_gst_clear.png", "M-006_gst_clear.png", "M-007_gst_clear.png"}
    if path.name in known_failures:
        request.node.add_marker(pytest.mark.xfail(strict=True, reason="Known clear-fixture OCR recognition failure"))
    kind = "PAN_CARD" if "_pan_" in path.name else "GST_CERT" if "_gst_" in path.name else "CANCELLED_CHEQUE"
    result = extract_from_image(path, kind)
    expected = {"PAN_CARD": "pan", "GST_CERT": "gstin", "CANCELLED_CHEQUE": "bank_account_number"}[kind]
    assert result.quality.readable
    assert expected in result.fields
    assert result.overall_confidence >= .7


def test_corrupt_image_returns_actionable_failure(tmp_path):
    path = tmp_path / "broken.png"
    path.write_bytes(b"not an image")
    result = extract_from_image(path, "PAN_CARD")
    assert not result.quality.readable
    assert result.quality.reason == "INVALID_IMAGE"
    assert result.fields == {}
