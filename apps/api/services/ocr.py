"""Document quality checks + Tesseract OCR field extraction.

Quality gate runs before OCR so blurred / blacked-out images request re-upload.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from PIL import Image, ImageFilter, ImageStat

logger = logging.getLogger("amop.ocr")

PAN_RE = re.compile(r"\b([A-Z]{5}[0-9]{4}[A-Z])\b")
GSTIN_RE = re.compile(r"\b([0-9]{2}[A-Z]{5}[0-9]{4}[A-Z][0-9A-Z]{3})\b")
IFSC_RE = re.compile(r"\b([A-Z]{4}0[A-Z0-9]{6})\b")
ACCOUNT_RE = re.compile(r"(?:A/?C(?:\s*No)?[:\s]*)([0-9]{9,18})", re.IGNORECASE)

# Tuned against fixtures/docs clear (~2400) vs blurred (~280) sharpness
BLUR_SCORE_MAX = 500.0
DARK_RATIO_MIN = 0.10
LOW_CONTRAST_MAX = 15.0


@dataclass
class QualityReport:
    readable: bool
    reason: str | None = None  # BLURRED | BLACKED_OUT | EMPTY | LOW_CONTRAST | OCR_ERROR
    blur_score: float = 0.0
    dark_ratio: float = 0.0
    details: dict[str, Any] = field(default_factory=dict)


@dataclass
class OcrResult:
    fields: dict[str, Any]
    overall_confidence: float
    raw_text: str
    quality: QualityReport
    extractor: str = "TESSERACT_OCR"


def _laplacian_var(img: Image.Image) -> float:
    gray = img.convert("L")
    edges = gray.filter(ImageFilter.FIND_EDGES)
    return float(ImageStat.Stat(edges).var[0])


def assess_quality(path: Path) -> QualityReport:
    img = Image.open(path).convert("RGB")
    img.thumbnail((1200, 1200))
    gray = img.convert("L")
    blur_score = _laplacian_var(img)
    # Pillow 10+: get_flattened_data; fall back for older
    if hasattr(gray, "get_flattened_data"):
        pixels = list(gray.get_flattened_data())
    else:
        pixels = list(gray.getdata())
    n = max(len(pixels), 1)
    dark = sum(1 for p in pixels if p < 30) / n
    mean = sum(pixels) / n
    contrast = float(ImageStat.Stat(gray).stddev[0])

    details = {
        "blur_score": round(blur_score, 2),
        "dark_ratio": round(dark, 3),
        "mean_luminance": round(mean, 1),
        "contrast": round(contrast, 2),
        "size": list(img.size),
    }

    # Blacked-out / redaction bars: significant near-black pixel share
    if dark >= DARK_RATIO_MIN:
        return QualityReport(
            readable=False,
            reason="BLACKED_OUT",
            blur_score=blur_score,
            dark_ratio=dark,
            details=details,
        )

    # Blurred: soft edges vs clear fixture scans
    if blur_score < BLUR_SCORE_MAX:
        return QualityReport(
            readable=False,
            reason="BLURRED",
            blur_score=blur_score,
            dark_ratio=dark,
            details=details,
        )

    if contrast < LOW_CONTRAST_MAX:
        return QualityReport(
            readable=False,
            reason="LOW_CONTRAST",
            blur_score=blur_score,
            dark_ratio=dark,
            details=details,
        )

    return QualityReport(
        readable=True,
        blur_score=blur_score,
        dark_ratio=dark,
        details=details,
    )


def _run_tesseract(path: Path) -> tuple[str, float]:
    try:
        import pytesseract
    except ImportError as exc:
        raise RuntimeError("pytesseract not installed") from exc

    img = Image.open(path)
    data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)
    texts = []
    confs = []
    for text, conf in zip(data.get("text", []), data.get("conf", []), strict=False):
        try:
            c = float(conf)
        except (TypeError, ValueError):
            continue
        if text and text.strip() and c >= 0:
            texts.append(text.strip())
            confs.append(c)
    raw = " ".join(texts)
    page = pytesseract.image_to_string(img) or ""
    combined = f"{page}\n{raw}"
    avg_conf = (sum(confs) / len(confs) / 100.0) if confs else 0.0
    return combined, avg_conf


def extract_fields_from_text(text: str, doc_type: str) -> dict[str, Any]:
    upper = text.upper()
    fields: dict[str, Any] = {}

    pan_m = PAN_RE.search(upper)
    if not pan_m:
        pan_m = PAN_RE.search(re.sub(r"[\s\-_]+", "", upper))

    gst_m = GSTIN_RE.search(upper)
    if not gst_m:
        gst_m = GSTIN_RE.search(re.sub(r"[\s\-_]+", "", upper))

    # Context-aware fallback: On GST certificates, search for state code + PAN + suffix
    if not gst_m and pan_m:
        pan_val = pan_m.group(1)
        embedded_m = re.search(rf"([0-9IOl|L]{{2,4}})\s*{pan_val}\s*([0-9A-Z]{{3,5}})", upper)
        if embedded_m:
            prefix = (
                embedded_m.group(1)
                .replace("L", "")
                .replace("I", "1")
                .replace("l", "1")
                .replace("|", "1")
                .replace("O", "0")
            )
            prefix = prefix[-2:]
            suffix = embedded_m.group(2)
            if "2Z" in suffix:
                suffix = suffix.replace("2Z", "Z")
            suffix = suffix[:3]
            candidate = f"{prefix}{pan_val}{suffix}"
            match = GSTIN_RE.match(candidate)
            if match:
                gst_m = match

    ifsc_m = IFSC_RE.search(upper)
    if not ifsc_m:
        ifsc_m = IFSC_RE.search(re.sub(r"[\s\-_]+", "", upper))

    acct_m = ACCOUNT_RE.search(text)
    if not acct_m:
        acct_m = ACCOUNT_RE.search(re.sub(r"[\s\-]+", "", text))

    if doc_type == "PAN_CARD" or pan_m:
        if pan_m:
            fields["pan"] = {
                "value": pan_m.group(1),
                "confidence": 0.92,
                "source": "OCR",
            }
    if doc_type == "GST_CERT" or gst_m:
        if gst_m:
            fields["gstin"] = {
                "value": gst_m.group(1),
                "confidence": 0.9,
                "source": "OCR",
            }
        if pan_m and "pan" not in fields:
            fields["pan"] = {
                "value": pan_m.group(1),
                "confidence": 0.85,
                "source": "OCR",
            }
    if doc_type == "CANCELLED_CHEQUE":
        if acct_m:
            fields["bank_account_number"] = {
                "value": acct_m.group(1),
                "confidence": 0.88,
                "source": "OCR",
            }
        if ifsc_m:
            fields["ifsc"] = {
                "value": ifsc_m.group(1),
                "confidence": 0.9,
                "source": "OCR",
            }
    else:
        if acct_m:
            fields["bank_account_number"] = {
                "value": acct_m.group(1),
                "confidence": 0.75,
                "source": "OCR",
            }
        if ifsc_m:
            fields["ifsc"] = {
                "value": ifsc_m.group(1),
                "confidence": 0.75,
                "source": "OCR",
            }

    return fields


def extract_from_image(path: Path, doc_type: str) -> OcrResult:
    try:
        quality = assess_quality(path)
    except (OSError, ValueError, Image.DecompressionBombError):
        return OcrResult(fields={}, overall_confidence=0.0, raw_text="",
                         quality=QualityReport(readable=False, reason="INVALID_IMAGE"))
    if not quality.readable:
        return OcrResult(
            fields={},
            overall_confidence=0.05,
            raw_text="",
            quality=quality,
            extractor="TESSERACT_OCR",
        )

    try:
        raw_text, avg_conf = _run_tesseract(path)
    except Exception as exc:
        logger.exception("OCR failed for %s", path)
        return OcrResult(
            fields={},
            overall_confidence=0.0,
            raw_text="",
            quality=QualityReport(
                readable=False,
                reason="OCR_ERROR",
                blur_score=quality.blur_score,
                dark_ratio=quality.dark_ratio,
                details={**quality.details, "error": str(exc)},
            ),
            extractor="TESSERACT_OCR",
        )

    fields = extract_fields_from_text(raw_text, doc_type)
    required_field = {"PAN_CARD": "pan", "GST_CERT": "gstin",
                      "CANCELLED_CHEQUE": "bank_account_number"}.get(doc_type)
    if not fields or (required_field and required_field not in fields):
        return OcrResult(
            fields={},
            overall_confidence=max(avg_conf, 0.1),
            raw_text=raw_text[:2000],
            quality=QualityReport(
                readable=False,
                reason="NO_FIELDS_EXTRACTED",
                blur_score=quality.blur_score,
                dark_ratio=quality.dark_ratio,
                details=quality.details,
            ),
            extractor="TESSERACT_OCR",
        )

    field_conf = min(float(v.get("confidence", 0.5)) for v in fields.values())
    overall = min(0.98, max(0.4, (avg_conf + field_conf) / 2))
    for v in fields.values():
        v["confidence"] = round(min(float(v["confidence"]), overall + 0.05), 3)

    return OcrResult(
        fields=fields,
        overall_confidence=round(overall, 3),
        raw_text=raw_text[:2000],
        quality=quality,
        extractor="TESSERACT_OCR",
    )
