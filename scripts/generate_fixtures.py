"""Generate synthetic KYC-looking document images (clear / blurred / blacked-out).

No real PII — values come from fixtures/merchants.json only.
"""

from __future__ import annotations

import json
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "fixtures"
DOCS = FIXTURES / "docs"


def _font(size: int):
    for name in (
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    ):
        p = Path(name)
        if p.exists():
            return ImageFont.truetype(str(p), size=size)
    return ImageFont.load_default()


def _draw_pan(name: str, pan: str) -> Image.Image:
    img = Image.new("RGB", (900, 560), (245, 236, 210))
    d = ImageDraw.Draw(img)
    d.rectangle([20, 20, 880, 540], outline=(120, 80, 20), width=4)
    d.text((40, 40), "INCOME TAX DEPARTMENT", fill=(80, 40, 10), font=_font(28))
    d.text((40, 85), "GOVT. OF INDIA  —  DEMO PAN CARD", fill=(100, 60, 20), font=_font(18))
    d.text((40, 160), "Name", fill=(90, 90, 90), font=_font(16))
    d.text((40, 190), name.upper(), fill=(20, 20, 20), font=_font(28))
    d.text((40, 270), "Permanent Account Number", fill=(90, 90, 90), font=_font(16))
    d.text((40, 305), pan.upper(), fill=(10, 10, 10), font=_font(40))
    d.text((40, 420), "Father's Name: DEMO FATHER", fill=(40, 40, 40), font=_font(18))
    d.text((40, 460), "DOB: 01/01/1990  (FIXTURE)", fill=(40, 40, 40), font=_font(18))
    d.text((40, 500), "SYNTHETIC DOCUMENT — NOT AN OFFICIAL ID", fill=(150, 40, 40), font=_font(14))
    return img


def _draw_gst(name: str, gstin: str, pan: str) -> Image.Image:
    img = Image.new("RGB", (1000, 700), (250, 250, 255))
    d = ImageDraw.Draw(img)
    d.rectangle([16, 16, 984, 684], outline=(20, 60, 120), width=4)
    d.text((40, 40), "GOODS AND SERVICES TAX  —  REGISTRATION CERTIFICATE", fill=(10, 40, 90), font=_font(22))
    d.text((40, 90), "(SYNTHETIC DEMO FIXTURE)", fill=(120, 40, 40), font=_font(16))
    d.text((40, 160), "Legal Name", fill=(90, 90, 90), font=_font(16))
    d.text((40, 190), name.upper(), fill=(10, 10, 10), font=_font(26))
    d.text((40, 270), "GSTIN", fill=(90, 90, 90), font=_font(16))
    d.text((40, 305), gstin.upper(), fill=(10, 10, 10), font=_font(36))
    d.text((40, 390), f"PAN: {pan.upper()}", fill=(30, 30, 30), font=_font(22))
    d.text((40, 450), "Status: ACTIVE (DEMO)", fill=(20, 100, 40), font=_font(20))
    d.text((40, 520), "Address: 123 Demo Street, Demo City, India", fill=(40, 40, 40), font=_font(18))
    d.text((40, 620), "SYNTHETIC DOCUMENT — NOT AN OFFICIAL CERTIFICATE", fill=(150, 40, 40), font=_font(14))
    return img


def _draw_cheque(name: str, account: str, ifsc: str) -> Image.Image:
    img = Image.new("RGB", (1100, 500), (230, 245, 235))
    d = ImageDraw.Draw(img)
    d.rectangle([16, 16, 1084, 484], outline=(20, 90, 60), width=3)
    d.text((40, 40), "DEMO BANK  —  CANCELLED CHEQUE (FIXTURE)", fill=(10, 60, 40), font=_font(22))
    d.text((40, 120), name.upper(), fill=(10, 10, 10), font=_font(24))
    d.text((40, 200), f"A/C No: {account}", fill=(10, 10, 10), font=_font(28))
    d.text((40, 260), f"IFSC: {ifsc.upper()}", fill=(10, 10, 10), font=_font(26))
    d.text((40, 340), "XXXXXXXX  CANCELLED  XXXXXXXX", fill=(160, 30, 30), font=_font(22))
    d.text((40, 420), "SYNTHETIC DOCUMENT — NOT A REAL CHEQUE", fill=(150, 40, 40), font=_font(14))
    return img


def _blur(img: Image.Image, radius: int = 28) -> Image.Image:
    return img.filter(ImageFilter.GaussianBlur(radius=radius))


def _blackout(img: Image.Image, boxes: list[tuple[int, int, int, int]]) -> Image.Image:
    out = img.copy()
    d = ImageDraw.Draw(out)
    for box in boxes:
        d.rectangle(box, fill=(5, 5, 5))
    # Extra bottom redaction bar so dark_ratio stays clearly above the quality gate
    w, h = out.size
    d.rectangle([20, int(h * 0.55), w - 20, h - 20], fill=(0, 0, 0))
    d.text((30, 30), "REDACTED / BLACKED OUT REGIONS", fill=(200, 40, 40), font=_font(18))
    return out


def main() -> None:
    DOCS.mkdir(parents=True, exist_ok=True)
    data = json.loads((FIXTURES / "merchants.json").read_text())
    for m in data["registry"]:
        mid = m["id"]
        pan_clear = _draw_pan(m["legal_name"], m["pan"])
        gst_clear = _draw_gst(m["legal_name"], m["gstin"], m["pan"])
        cheque_clear = _draw_cheque(m["legal_name"], m["bank_account_number"], m["ifsc"])

        variants = {
            f"{mid}_pan_clear.png": pan_clear,
            f"{mid}_gst_clear.png": gst_clear,
            f"{mid}_cheque_clear.png": cheque_clear,
            f"{mid}_pan_blurred.png": _blur(pan_clear, 22),
            f"{mid}_gst_blurred.png": _blur(gst_clear, 22),
            f"{mid}_pan_blacked.png": _blackout(pan_clear, [(40, 290, 520, 360), (40, 180, 700, 230)]),
            f"{mid}_gst_blacked.png": _blackout(gst_clear, [(40, 290, 700, 360), (40, 180, 800, 230)]),
        }
        for name, img in variants.items():
            path = DOCS / name
            img.save(path, format="PNG")
            print(f"wrote {path.relative_to(ROOT)}")

    print("Fixture documents ready under fixtures/docs/")


if __name__ == "__main__":
    main()
