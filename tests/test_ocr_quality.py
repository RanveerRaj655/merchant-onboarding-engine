from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from apps.api.services.ocr import assess_quality, extract_fields_from_text


def _font():
    for name in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    ):
        if Path(name).exists():
            return ImageFont.truetype(name, 36)
    return ImageFont.load_default()


def test_extract_fields_from_text():
    text = "Name ACME  Permanent Account Number AABCA1234A  GSTIN 27AABCA1234A1Z5"
    fields = extract_fields_from_text(text, "PAN_CARD")
    assert fields["pan"]["value"] == "AABCA1234A"


def test_blur_detected(tmp_path: Path):
    img = Image.new("RGB", (800, 400), (240, 240, 240))
    d = ImageDraw.Draw(img)
    d.text((40, 150), "AABCA1234A", fill=(0, 0, 0), font=_font())
    clear = tmp_path / "clear.png"
    blur = tmp_path / "blur.png"
    img.save(clear)
    img.filter(ImageFilter.GaussianBlur(24)).save(blur)

    assert assess_quality(clear).readable is True
    q = assess_quality(blur)
    assert q.readable is False
    assert q.reason in {"BLURRED", "LOW_CONTRAST"}


def test_blackout_detected(tmp_path: Path):
    img = Image.new("RGB", (800, 400), (240, 240, 240))
    d = ImageDraw.Draw(img)
    d.text((40, 80), "PAN AABCA1234A", fill=(0, 0, 0), font=_font())
    d.rectangle([20, 50, 780, 360], fill=(5, 5, 5))
    path = tmp_path / "blacked.png"
    img.save(path)
    q = assess_quality(path)
    assert q.readable is False
    assert q.reason == "BLACKED_OUT"


def test_fixture_images_classified():
    root = Path(__file__).resolve().parents[1] / "fixtures" / "docs"
    if not (root / "M-001_pan_clear.png").exists():
        return
    assert assess_quality(root / "M-001_pan_clear.png").readable is True
    assert assess_quality(root / "M-004_pan_blurred.png").readable is False
    assert assess_quality(root / "M-005_pan_blacked.png").reason == "BLACKED_OUT"
