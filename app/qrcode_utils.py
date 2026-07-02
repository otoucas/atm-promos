"""Decode HighCo Nifty QR codes from PDF attachments or images (email attachments,
inline images, or manual admin uploads)."""

import io
from typing import List, Optional


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
