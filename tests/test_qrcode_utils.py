import io
import random

import fitz
from PIL import Image

from app.qrcode_utils import _uniform_pixel_fraction, extract_best_product_image, extract_qr_payload


def _png_bytes(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _solid_background_image(size=(300, 300)) -> bytes:
    return _png_bytes(Image.new("L", size, color=250))


def _noisy_photo_like_image(size=(300, 300)) -> bytes:
    random.seed(42)
    img = Image.new("L", size)
    img.putdata([random.randint(60, 190) for _ in range(size[0] * size[1])])
    return _png_bytes(img)


def test_uniform_pixel_fraction_solid_background_is_high():
    assert _uniform_pixel_fraction(_solid_background_image()) > 0.97


def test_uniform_pixel_fraction_noisy_photo_is_low():
    assert _uniform_pixel_fraction(_noisy_photo_like_image()) < 0.97


def test_uniform_pixel_fraction_invalid_bytes_returns_zero():
    assert _uniform_pixel_fraction(b"not an image") == 0.0


def _make_pdf_with_images(images: list[bytes]) -> bytes:
    doc = fitz.open()
    page = doc.new_page()
    for i, img_bytes in enumerate(images):
        # spread rectangles so images don't overlap/get merged
        rect = fitz.Rect(10, 10 + i * 160, 160, 160 + i * 160)
        page.insert_image(rect, stream=img_bytes)
    return doc.tobytes()


def test_extract_best_product_image_picks_the_photo_over_background():
    background = _solid_background_image((400, 400))
    photo = _noisy_photo_like_image((200, 200))
    pdf_bytes = _make_pdf_with_images([background, photo])

    result = extract_best_product_image(pdf_bytes)
    assert result is not None
    image_bytes, _ext = result
    assert _uniform_pixel_fraction(image_bytes) < 0.97


def test_extract_best_product_image_excludes_too_small_images():
    tiny_photo = _noisy_photo_like_image((50, 50))
    pdf_bytes = _make_pdf_with_images([tiny_photo])

    assert extract_best_product_image(pdf_bytes, min_dimension=150) is None


def test_extract_best_product_image_no_images_returns_none():
    doc = fitz.open()
    doc.new_page()
    assert extract_best_product_image(doc.tobytes()) is None


def test_extract_best_product_image_invalid_pdf_returns_none():
    assert extract_best_product_image(b"not a pdf") is None


def test_extract_qr_payload_decodes_generated_qr_image():
    qrcode = __import__("qrcode") if _has_qrcode_lib() else None
    if qrcode is None:
        import pytest

        pytest.skip("qrcode lib not installed — only used to synthesize a test fixture")
    img = qrcode.make("https://opn.to/a/TESTPAYLOAD")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    payload = extract_qr_payload(buf.getvalue(), filename="test.png", content_type="image/png")
    assert payload == "https://opn.to/a/TESTPAYLOAD"


def _has_qrcode_lib() -> bool:
    try:
        import qrcode  # noqa: F401

        return True
    except ImportError:
        return False


def test_extract_qr_payload_no_qr_in_image_returns_none():
    payload = extract_qr_payload(_solid_background_image(), filename="test.png", content_type="image/png")
    assert payload is None
