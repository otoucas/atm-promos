"""Polls a Gmail mailbox over IMAP for HighCo Nifty promotion emails, decodes the
QR code from PDF attachments or inline images, and creates pending Promotion rows
for admin review/validation.

Setup: enable 2-Step Verification on the Gmail account, then create an "App
password" (Google Account > Security > App passwords) and set it as
GMAIL_APP_PASSWORD. See README for details.

GMAIL_MAILBOX can be any Gmail label, not just INBOX — useful when a mail
filter already auto-labels/archives the relevant emails (e.g. a "Commercial"
label) before they'd otherwise be seen sitting in the inbox.
"""

import email
import imaplib
import logging
import re
import uuid
from email.header import decode_header, make_header
from email.message import Message

from sqlalchemy.orm import Session

from . import config
from .date_extraction import extract_validity_dates
from .logos import fetch_logo_url
from .models import STATUS_PENDING, SOURCE_EMAIL, Promotion, ProcessedEmail
from .qrcode_utils import extract_best_product_image, extract_qr_payload

logger = logging.getLogger("gmail_poller")

_CANDIDATE_CONTENT_TYPES = ("application/pdf", "image/png", "image/jpeg", "image/jpg", "image/gif")
_CANDIDATE_EXTENSIONS = (".pdf", ".png", ".jpg", ".jpeg", ".gif")
# Some senders mislabel attachments generically — fall back to the filename
# extension rather than trusting Content-Type alone (seen in practice:
# HighCo Nifty PDFs sent as application/octet-stream).
_GENERIC_CONTENT_TYPES = ("application/octet-stream",)


def _iter_candidate_parts(msg: Message):
    for part in msg.walk():
        content_type = part.get_content_type()
        disposition = part.get("Content-Disposition", "")
        filename = part.get_filename() or ""
        is_candidate_type = content_type in _CANDIDATE_CONTENT_TYPES
        is_candidate_by_extension = content_type in _GENERIC_CONTENT_TYPES and filename.lower().endswith(
            _CANDIDATE_EXTENSIONS
        )
        if (is_candidate_type or is_candidate_by_extension) and (
            "attachment" in disposition or "inline" in disposition or content_type == "application/pdf"
        ):
            payload = part.get_payload(decode=True)
            if payload:
                yield filename, content_type, payload


def _extract_pdf_text(data: bytes) -> str:
    """The validity dates for a "launch" campaign are often only printed on
    the promotional PDF (shelf-talker), not mentioned in the email body."""
    import fitz  # PyMuPDF

    try:
        doc = fitz.open(stream=data, filetype="pdf")
        return "\n".join(page.get_text() for page in doc)
    except Exception:
        return ""


def _save_product_image(image_bytes: bytes, ext: str) -> str:
    filename = f"{uuid.uuid4().hex}.{ext}"
    (config.LOGO_DIR / filename).write_bytes(image_bytes)
    return filename


def _get_body_text(msg: Message) -> str:
    for part in msg.walk():
        if part.get_content_type() == "text/plain":
            payload = part.get_payload(decode=True)
            if payload:
                charset = part.get_content_charset() or "utf-8"
                try:
                    return payload.decode(charset, errors="replace")
                except LookupError:
                    return payload.decode("utf-8", errors="replace")
    for part in msg.walk():
        if part.get_content_type() == "text/html":
            payload = part.get_payload(decode=True)
            if payload:
                charset = part.get_content_charset() or "utf-8"
                try:
                    html = payload.decode(charset, errors="replace")
                except LookupError:
                    html = payload.decode("utf-8", errors="replace")
                return re.sub(r"<[^>]+>", " ", html)
    return ""


def _decode_subject(raw_subject: str) -> str:
    if not raw_subject:
        return ""
    try:
        return str(make_header(decode_header(raw_subject)))
    except (ValueError, LookupError):
        return raw_subject


# HighCo Nifty subjects consistently look like "PROMO [NIFTY] <BRAND> 5€ de
# remise ..." — the brand name sits between the fixed prefix and the first
# digit (the discount amount). Best-effort only: the admin reviews/corrects
# every promotion on the validation screen regardless.
_NIFTY_SUBJECT_PATTERN = re.compile(r"PROMO\s+(?:NIFTY\s+)?(.+?)\s+\d", re.IGNORECASE)
_AMOUNT_PATTERN = re.compile(r"\d+(?:[.,]\d+)?\s*[%€]")


def _guess_brand_name(subject: str) -> str:
    decoded = _decode_subject(subject)
    match = _NIFTY_SUBJECT_PATTERN.search(decoded)
    if match:
        return match.group(1).strip()[:200]
    return (decoded or "Promotion à nommer").strip()[:200]


def _guess_operation_label(subject: str) -> str:
    """Harmonized "amount/percentage" part of the display name, e.g. "50%" or
    "0,50€" — pulled from the first discount value mentioned in the subject."""
    decoded = _decode_subject(subject)
    match = _AMOUNT_PATTERN.search(decoded)
    return match.group(0).replace(" ", "") if match else ""


def _connect():
    if not config.GMAIL_ADDRESS or not config.GMAIL_APP_PASSWORD:
        raise RuntimeError("GMAIL_ADDRESS / GMAIL_APP_PASSWORD non configurés")
    conn = imaplib.IMAP4_SSL(config.GMAIL_IMAP_HOST)
    conn.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
    conn.select(config.GMAIL_MAILBOX)
    return conn


def _build_search_criteria(sender_filter: str) -> str:
    """GMAIL_SENDER_FILTER may hold several comma-separated domains/addresses
    — combine them with IMAP's nested OR so any of them matches."""
    senders = [s.strip() for s in sender_filter.split(",") if s.strip()]
    if not senders:
        return "ALL"
    criteria = f'FROM "{senders[0]}"'
    for sender in senders[1:]:
        criteria = f'OR ({criteria}) (FROM "{sender}")'
    return f"({criteria})"


def _archive_from_inbox(conn: imaplib.IMAP4_SSL, uid: bytes) -> None:
    """Remove the \\Inbox label (Gmail IMAP extension) — a no-op if the
    message was already archived (e.g. auto-labelled out of the inbox by a
    mail filter)."""
    try:
        conn.uid("STORE", uid, "-X-GM-LABELS", "(\\Inbox)")
    except imaplib.IMAP4.error:
        logger.exception("Échec de l'archivage du mail (uid=%s)", uid)


def poll_gmail_once(db: Session) -> int:
    """Fetch new messages, extract QR payloads, create pending promotions.

    Returns the number of new pending promotions created.
    """
    conn = _connect()
    created = 0
    try:
        search_criteria = _build_search_criteria(config.GMAIL_SENDER_FILTER)
        status, data = conn.uid("SEARCH", None, search_criteria)
        if status != "OK":
            logger.warning("IMAP search failed: %s", status)
            return 0

        message_uids = data[0].split()
        for uid in message_uids:
            status, msg_data = conn.uid("FETCH", uid, "(RFC822)")
            if status != "OK" or not msg_data or msg_data[0] is None:
                continue
            raw_bytes = msg_data[0][1]
            msg = email.message_from_bytes(raw_bytes)
            message_id = msg.get("Message-ID") or f"no-id-{uid.decode()}"

            if db.query(ProcessedEmail).filter_by(message_id=message_id).first():
                continue

            subject = _decode_subject(msg.get("Subject", ""))
            qr_payload = None
            qr_source_filename = ""
            qr_source_payload = b""
            for filename, content_type, payload in _iter_candidate_parts(msg):
                qr_payload = extract_qr_payload(payload, filename=filename, content_type=content_type)
                if qr_payload:
                    qr_source_filename = filename
                    qr_source_payload = payload
                    break

            if qr_payload:
                brand_name = _guess_brand_name(subject)
                operation_label = _guess_operation_label(subject)
                is_pdf = qr_source_filename.lower().endswith(".pdf") or qr_source_payload[:4] == b"%PDF"

                date_text = _get_body_text(msg)
                if is_pdf:
                    date_text += "\n" + _extract_pdf_text(qr_source_payload)
                valid_from, valid_until = extract_validity_dates(date_text)

                logo_path = None
                if is_pdf:
                    product_image = extract_best_product_image(qr_source_payload)
                    if product_image:
                        logo_path = _save_product_image(*product_image)

                promo = Promotion(
                    brand_name=brand_name,
                    operation_label=operation_label or None,
                    highco_reference=qr_payload,
                    status=STATUS_PENDING,
                    source=SOURCE_EMAIL,
                    raw_email_subject=subject,
                    source_message_id=message_id,
                    logo_path=logo_path,
                    logo_url=None if logo_path else fetch_logo_url(brand_name),
                    valid_from=valid_from,
                    valid_until=valid_until,
                )
                db.add(promo)
                created += 1
                if config.GMAIL_ARCHIVE_AFTER_PROCESSING:
                    _archive_from_inbox(conn, uid)
            else:
                logger.info("Aucun QR trouvé dans le mail %r — ignoré", subject)

            db.add(ProcessedEmail(message_id=message_id))

        db.commit()
    finally:
        conn.logout()

    return created
