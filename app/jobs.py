import datetime
import logging

from .database import SessionLocal
from .gmail_poller import poll_gmail_once
from .models import STATUS_ACTIVE, STATUS_ARCHIVED, STATUS_PENDING, Promotion

logger = logging.getLogger("jobs")


def run_gmail_poll():
    db = SessionLocal()
    try:
        created, merged = poll_gmail_once(db)
        if created or merged:
            logger.info(
                "%d nouvelle(s) promotion(s) en attente, %d mail(s) fusionné(s) dans une promotion existante",
                created,
                merged,
            )
    except Exception:
        logger.exception("Échec du polling Gmail")
    finally:
        db.close()


def _archive_expired(db, today: datetime.date) -> int:
    """Archives active promotions past their end date. Also sweeps stale
    pending ones (never validated, and whose window already closed) —
    no point asking an admin to review a promotion that can't be used
    anymore."""
    expired = (
        db.query(Promotion)
        .filter(
            Promotion.status.in_([STATUS_ACTIVE, STATUS_PENDING]),
            Promotion.valid_until.isnot(None),
            Promotion.valid_until < today,
        )
        .all()
    )
    for promo in expired:
        promo.status = STATUS_ARCHIVED
        promo.archived_at = datetime.datetime.utcnow()
    return len(expired)


def run_auto_archive():
    """Hourly safety net — catches expirations promptly rather than waiting
    for the once-a-day review."""
    db = SessionLocal()
    try:
        today = datetime.date.today()
        count = _archive_expired(db, today)
        if count:
            db.commit()
            logger.info("%d promotion(s) archivée(s) automatiquement", count)
    except Exception:
        logger.exception("Échec de l'archivage automatique")
    finally:
        db.close()


def run_daily_review():
    """Once-a-day checkpoint: archive anything past its end date (including
    stale pending promotions never validated in time), and log a clear
    summary of what's now visible at the counter vs. what just got
    archived — a readable "what changed overnight" record in the app logs."""
    db = SessionLocal()
    try:
        today = datetime.date.today()
        archived_count = _archive_expired(db, today)
        db.commit()

        visible = (
            db.query(Promotion)
            .filter(
                Promotion.status == STATUS_ACTIVE,
                (Promotion.valid_until.is_(None)) | (Promotion.valid_until >= today),
                (Promotion.valid_from.is_(None)) | (Promotion.valid_from <= today),
            )
            .order_by(Promotion.brand_name)
            .all()
        )
        upcoming = (
            db.query(Promotion)
            .filter(Promotion.status == STATUS_ACTIVE, Promotion.valid_from > today)
            .order_by(Promotion.valid_from)
            .all()
        )
        pending_count = db.query(Promotion).filter(Promotion.status == STATUS_PENDING).count()

        logger.info(
            "Revue quotidienne (%s) : %d promotion(s) visible(s) au comptoir, %d à venir, "
            "%d archivée(s) aujourd'hui, %d en attente de validation",
            today.isoformat(),
            len(visible),
            len(upcoming),
            archived_count,
            pending_count,
        )
        if upcoming:
            logger.info(
                "À venir : %s",
                ", ".join(f"{p.display_name} (dès le {p.valid_from})" for p in upcoming),
            )
    except Exception:
        logger.exception("Échec de la revue quotidienne")
    finally:
        db.close()
