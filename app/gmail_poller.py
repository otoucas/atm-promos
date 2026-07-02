"""Polls a Gmail mailbox over IMAP for HighCo Nifty promotion emails, decodes the
QR code from PDF attachments or inline images, and creates pending Promotion rows
for admin review/validation.

Setup: enable 2-Step Verification on the Gmail account, then create an "App
password" (Google Account > Security > App passwords) and set it as
GMAIL_APP_PASSWORD. See README for details.
"""

import email
import imaplib
import logging
from email.message import Message

from sqlalchemy.orm import Session

from . import config
from .logos import fetch_logo_url
from .models import STATUS_PENDING, SOURCE_EMAIL, Promotion, ProcessedEmail
from .qrcode_utils import extract_qr_payload

logger = logging.getLogger("gmail_poller")

_CANDIDATE_CONTENT_TYPES = ("application/pdf", "image/png", "image/jpeg", "image/jpg", "image/gif")


def _iter_candidate_parts(msg: Message):
    for part in msg.walk():
        content_type = part.get_content_type()
        disposition = part.get("Content-Disposition", "")
        if content_type in _CANDIDATE_CONTENT_TYPES and (
            "attachment" in disposition or "inline" in disposition or content_type == "application/pdf"
        ):
            payload = part.get_payload(decode=True)
            if payload:
                yield part.get_filename() or "", content_type, payload


def _guess_brand_name(subject: str) -> str:
    # Best-effort placeholder — the admin corrects this during validation.
    return (subject or "Promotion à nommer").strip()[:200]


def _connect():
    if not config.GMAIL_ADDRESS or not config.GMAIL_APP_PASSWORD:
        raise RuntimeError("GMAIL_ADDRESS / GMAIL_APP_PASSWORD non configurés")
    conn = imaplib.IMAP4_SSL(config.GMAIL_IMAP_HOST)
    conn.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
    conn.select(config.GMAIL_MAILBOX)
    return conn


def poll_gmail_once(db: Session) -> int:
    """Fetch new messages, extract QR payloads, create pending promotions.

    Returns the number of new pending promotions created.
    """
    conn = _connect()
    created = 0
    try:
        search_criteria = "ALL"
        if config.GMAIL_SENDER_FILTER:
            search_criteria = f'(FROM "{config.GMAIL_SENDER_FILTER}")'
        status, data = conn.search(None, search_criteria)
        if status != "OK":
            logger.warning("IMAP search failed: %s", status)
            return 0

        message_numbers = data[0].split()
        for num in message_numbers:
            status, msg_data = conn.fetch(num, "(RFC822)")
            if status != "OK" or not msg_data or msg_data[0] is None:
                continue
            raw_bytes = msg_data[0][1]
            msg = email.message_from_bytes(raw_bytes)
            message_id = msg.get("Message-ID") or f"no-id-{num.decode()}"

            if db.query(ProcessedEmail).filter_by(message_id=message_id).first():
                continue

            subject = msg.get("Subject", "")
            qr_payload = None
            for filename, content_type, payload in _iter_candidate_parts(msg):
                qr_payload = extract_qr_payload(payload, filename=filename, content_type=content_type)
                if qr_payload:
                    break

            if qr_payload:
                brand_name = _guess_brand_name(subject)
                promo = Promotion(
                    brand_name=brand_name,
                    highco_reference=qr_payload,
                    status=STATUS_PENDING,
                    source=SOURCE_EMAIL,
                    raw_email_subject=subject,
                    logo_url=fetch_logo_url(brand_name),
                )
                db.add(promo)
                created += 1
            else:
                logger.info("Aucun QR trouvé dans le mail %r — ignoré", subject)

            db.add(ProcessedEmail(message_id=message_id))

        db.commit()
    finally:
        conn.logout()

    return created
