import datetime
import logging

from .database import SessionLocal
from .gmail_poller import poll_gmail_once
from .models import STATUS_ACTIVE, STATUS_ARCHIVED, Promotion

logger = logging.getLogger("jobs")


def run_gmail_poll():
    db = SessionLocal()
    try:
        created = poll_gmail_once(db)
        if created:
            logger.info("%d nouvelle(s) promotion(s) en attente depuis Gmail", created)
    except Exception:
        logger.exception("Échec du polling Gmail")
    finally:
        db.close()


def run_auto_archive():
    db = SessionLocal()
    try:
        today = datetime.date.today()
        expired = (
            db.query(Promotion)
            .filter(Promotion.status == STATUS_ACTIVE, Promotion.valid_until.isnot(None), Promotion.valid_until < today)
            .all()
        )
        for promo in expired:
            promo.status = STATUS_ARCHIVED
            promo.archived_at = datetime.datetime.utcnow()
        if expired:
            db.commit()
            logger.info("%d promotion(s) archivée(s) automatiquement", len(expired))
    except Exception:
        logger.exception("Échec de l'archivage automatique")
    finally:
        db.close()
