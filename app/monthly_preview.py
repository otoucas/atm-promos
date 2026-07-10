"""E-mail mensuel de préparation : envoyé quelques jours avant le début du
mois suivant, pour qu'Olivier puisse traiter à l'avance ce qui demande une
décision humaine (validations en attente, démarrages/fins de campagne à
répercuter sur le commentaire produit Winpharma, conflits non résolus)."""

import calendar
import datetime
import logging
import smtplib
from email.mime.text import MIMEText

from sqlalchemy.orm import Session

from . import config
from .database import SessionLocal
from .models import STATUS_ACTIVE, STATUS_PENDING, Promotion, Store
from .promotion_rules import find_conflicting_ids

logger = logging.getLogger("monthly_preview")

_MOIS_FR = {
    1: "janvier",
    2: "février",
    3: "mars",
    4: "avril",
    5: "mai",
    6: "juin",
    7: "juillet",
    8: "août",
    9: "septembre",
    10: "octobre",
    11: "novembre",
    12: "décembre",
}


def _next_month_range(today: datetime.date) -> tuple:
    year, month = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
    start = datetime.date(year, month, 1)
    end = datetime.date(year, month, calendar.monthrange(year, month)[1])
    return start, end


def build_preview_email(db: Session, today: datetime.date = None) -> tuple:
    """Ce rappel ne couvre que le point de vente historique (Artemare) : le
    commentaire produit Winpharma (Ctrl+M) qu'il mentionne n'a de sens que
    pour ce point de vente-là."""
    today = today or datetime.date.today()
    start, end = _next_month_range(today)
    mois_label = f"{_MOIS_FR[start.month]} {start.year}"

    store = db.query(Store).filter(Store.code == config.DEFAULT_STORE_CODE).first()
    store_filter = (Promotion.store_id == store.id) if store else Promotion.store_id.is_(None)

    pending = (
        db.query(Promotion)
        .filter(store_filter, Promotion.status == STATUS_PENDING)
        .order_by(Promotion.brand_name)
        .all()
    )
    starting = (
        db.query(Promotion)
        .filter(store_filter, Promotion.status == STATUS_ACTIVE, Promotion.valid_from >= start, Promotion.valid_from <= end)
        .order_by(Promotion.valid_from)
        .all()
    )
    ending = (
        db.query(Promotion)
        .filter(store_filter, Promotion.status == STATUS_ACTIVE, Promotion.valid_until >= start, Promotion.valid_until <= end)
        .order_by(Promotion.valid_until)
        .all()
    )
    active = db.query(Promotion).filter(store_filter, Promotion.status == STATUS_ACTIVE).all()
    conflict_ids = find_conflicting_ids(active)
    conflicts = [p for p in active if p.id in conflict_ids]

    lines = [f"Préparation des promotions Nifty pour {mois_label}.", ""]

    if pending:
        lines.append(f"À valider ou rejeter sur /admin/pending ({len(pending)}) :")
        for p in pending:
            lines.append(f"  - {p.display_name} ({p.valid_from or '?'} → {p.valid_until or '?'})")
    else:
        lines.append("Rien en attente de validation.")
    lines.append("")

    if starting:
        lines.append(f"Démarrent en {mois_label} — penser au commentaire Winpharma (Ctrl+M) :")
        for p in starting:
            lines.append(f"  - {p.display_name}, dès le {p.valid_from}")
        lines.append("")

    if ending:
        lines.append(f"Se terminent en {mois_label} — penser à enlever le commentaire Winpharma :")
        for p in ending:
            lines.append(f"  - {p.display_name}, jusqu'au {p.valid_until}")
        lines.append("")

    if conflicts:
        lines.append("Conflits actifs non résolus :")
        for p in conflicts:
            lines.append(f"  - {p.display_name} ({p.valid_from} → {p.valid_until})")
        lines.append("")

    subject = f"[ATM Nifty] Promotions à préparer pour {mois_label}"
    body = "\n".join(lines).rstrip() + "\n"
    return subject, body


def send_email(subject: str, body: str) -> None:
    if not config.GMAIL_ADDRESS or not config.GMAIL_APP_PASSWORD:
        raise RuntimeError("GMAIL_ADDRESS / GMAIL_APP_PASSWORD non configurés")
    if not config.MONTHLY_PREVIEW_RECIPIENT:
        raise RuntimeError("MONTHLY_PREVIEW_RECIPIENT non configuré")

    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = subject
    msg["From"] = config.GMAIL_ADDRESS
    msg["To"] = config.MONTHLY_PREVIEW_RECIPIENT

    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as smtp:
        smtp.login(config.GMAIL_ADDRESS, config.GMAIL_APP_PASSWORD)
        smtp.sendmail(config.GMAIL_ADDRESS, [config.MONTHLY_PREVIEW_RECIPIENT], msg.as_string())


def run_monthly_preview() -> None:
    if not config.MONTHLY_PREVIEW_RECIPIENT:
        logger.info("Rappel mensuel désactivé (MONTHLY_PREVIEW_RECIPIENT non configuré)")
        return

    db = SessionLocal()
    try:
        subject, body = build_preview_email(db)
        send_email(subject, body)
        logger.info("E-mail de préparation mensuelle envoyé : %s", subject)
    except Exception:
        logger.exception("Échec de l'envoi de l'e-mail de préparation mensuelle")
    finally:
        db.close()
