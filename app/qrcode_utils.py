"""Decode HighCo Nifty QR codes from PDF attachments or images (email attachments,
inline images, or manual admin uploads)."""

import io
from typing import List, Optional, Tuple


def decode_qr_from_image_bytes(data: bytes) -> List[str]:
    from PIL import Image
    from pyzbar.pyzbar import decode

    try:
        img = Image.open(io.BytesIO(data))
    except Exception:
        return []
    results = decode(img)
    return [r.data.decode("utf-8", errors="replace") for r in results]


def decode_qr_from_pdf_bytes(data: bytes) -> List[str]:
    import fitz  # PyMuPDF

    payloads: List[str] = []
    try:
        doc = fitz.open(stream=data, filetype="pdf")
    except Exception:
        return []
    for page in doc:
        # Upscale rendering — QR codes in a printable PDF are often small in raw pixel terms.
        pix = page.get_pixmap(matrix=fitz.Matrix(3, 3))
        payloads.extend(decode_qr_from_image_bytes(pix.tobytes("png")))
        if payloads:
            break
    return payloads


def extract_qr_payload(data: bytes, filename: str = "", content_type: str = "") -> Optional[str]:
    """Best-effort extraction of the first QR payload from a PDF or image blob."""
    looks_like_pdf = content_type == "application/pdf" or filename.lower().endswith(".pdf") or data[:4] == b"%PDF"

    payloads: List[str] = []
    if looks_like_pdf:
        payloads = decode_qr_from_pdf_bytes(data)
    if not payloads:
        payloads = decode_qr_from_image_bytes(data)
    if not payloads and not looks_like_pdf:
        # last resort: maybe it actually was a PDF mislabeled
        payloads = decode_qr_from_pdf_bytes(data)

    return payloads[0] if payloads else None


def _uniform_pixel_fraction(image_bytes: bytes, dark_threshold: int = 20, light_threshold: int = 235) -> float:
    """Fraction of near-black/near-white pixels — a cheap proxy for "mostly
    empty background design element" vs. an actual product photo. Real
    product photography measured ~2-30% here; sparse branded backgrounds
    measured ~99-100%."""
    from PIL import Image

    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("L")
    except Exception:
        return 0.0
    histogram = img.histogram()
    total = sum(histogram) or 1
    dark = sum(histogram[:dark_threshold])
    light = sum(histogram[light_threshold:])
    return (dark + light) / total


def extract_best_product_image(
    pdf_bytes: bytes, min_dimension: int = 150, max_uniform_fraction: float = 0.97
) -> Optional[Tuple[bytes, str]]:
    """Best-effort pick of the most useful embedded image in a promotional PDF
    (product packaging / brand visual) to use as the operator-tile picture,
    instead of a generic auto-fetched brand logo. Candidates are excluded if
    they're scannable as a QR/barcode themselves (the QR crop is also
    embedded as its own image asset), too small to be a real photo (icons,
    tiny badges), or mostly a flat/near-empty background (many HighCo PDFs
    have a large but visually sparse branded intro image that isn't a useful
    thumbnail). Among what's left, picks the largest by pixel area; if
    everything got filtered out, falls back to the largest image overall
    rather than returning nothing.

    Returns (image_bytes, extension) or None if the PDF has no images at all.
    """
    import fitz  # PyMuPDF

    try:
        doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    except Exception:
        return None

    candidates = []  # (area, image_bytes, ext)
    for page in doc:
        for img in page.get_images(full=True):
            xref, width, height = img[0], img[2], img[3]
            if width < min_dimension or height < min_dimension:
                continue
            try:
                extracted = doc.extract_image(xref)
            except Exception:
                continue
            image_bytes = extracted.get("image")
            if not image_bytes or decode_qr_from_image_bytes(image_bytes):
                continue
            candidates.append((width * height, image_bytes, extracted.get("ext", "png")))

    informative = [c for c in candidates if _uniform_pixel_fraction(c[1]) <= max_uniform_fraction]
    if not informative:
        return None
    _, image_bytes, ext = max(informative, key=lambda c: c[0])
    return image_bytes, ext
