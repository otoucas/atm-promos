import datetime
import logging

from . import config
from .database import SessionLocal
from .gmail_poller import poll_gmail_once
from .models import STATUS_ACTIVE, STATUS_ARCHIVED, STATUS_PENDING, Promotion, Store

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


def _due(target_date: datetime.date | None, today: datetime.date, days_before: int) -> bool:
    """Vrai si target_date tombe dans la fenêtre de rappel [aujourd'hui ;
    aujourd'hui + days_before] — la borne basse évite de rappeler une échéance
    déjà passée après une coupure prolongée du service."""
    if target_date is None:
        return False
    delta = (target_date - today).days
    return 0 <= delta <= days_before


def _reminder_body(promo: Promotion, store: Store, event: str, event_date: datetime.date) -> str:
    verb = "démarre" if event == "start" else "se termine"
    body = f"La promotion « {promo.display_name} » de {store.name} {verb} le {event_date.strftime('%d/%m/%Y')}."
    if config.PUBLIC_BASE_URL:
        body += f"\n\nRetrouvez-la dans vos promotions : {config.PUBLIC_BASE_URL}/{store.code}/admin/promotions"
    return body


def _send_reminder(send_email, store: Store, promo: Promotion, event: str, event_date: datetime.date) -> bool:
    verb = "démarre" if event == "start" else "se termine"
    subject = f"[Nifty] « {promo.display_name} » {verb} bientôt"
    try:
        send_email(subject, _reminder_body(promo, store, event, event_date), store.notification_email)
        return True
    except Exception:
        logger.exception("Échec du rappel de %s pour %s / promo %s", event, store.code, promo.id)
        return False


def run_promo_notifications():
    """Rappels par email paramétrables par point de vente (toggle + délais,
    voir Store.notifications_enabled et /{code}/admin/notifications) : un
    envoi J-X avant le début et J-X avant la fin d'une campagne active. Chaque
    échéance n'est signalée qu'une seule fois, grâce aux horodatages
    start_reminder_sent_at / end_reminder_sent_at sur la promotion elle-même
    (demande Olivier du 2026-07-14)."""
    from .store_requests import _send_email

    db = SessionLocal()
    try:
        today = datetime.date.today()
        stores = db.query(Store).filter(Store.notifications_enabled.is_(True)).all()
        sent = 0
        for store in stores:
            if not store.notification_email:
                continue
            promos = (
                db.query(Promotion)
                .filter(Promotion.store_id == store.id, Promotion.status == STATUS_ACTIVE)
                .all()
            )
            for promo in promos:
                if not promo.start_reminder_sent_at and _due(promo.valid_from, today, store.notify_days_before_start):
                    if _send_reminder(_send_email, store, promo, "start", promo.valid_from):
                        promo.start_reminder_sent_at = datetime.datetime.utcnow()
                        sent += 1
                if not promo.end_reminder_sent_at and _due(promo.valid_until, today, store.notify_days_before_end):
                    if _send_reminder(_send_email, store, promo, "end", promo.valid_until):
                        promo.end_reminder_sent_at = datetime.datetime.utcnow()
                        sent += 1
        if sent:
            db.commit()
            logger.info("%d email(s) de rappel de campagne envoyé(s)", sent)
    except Exception:
        logger.exception("Échec des notifications de campagne")
    finally:
        db.close()


def run_erpnext_sync():
    """Pousse (upsert idempotent) les promotions actives/archivées vers le
    module ERPNext atm_nifty. Best-effort : n'interrompt jamais le service."""
    from .erpnext_sync import sync_all

    db = SessionLocal()
    try:
        pushed, failed = sync_all(db)
        if pushed or failed:
            logger.info("Synchro ERPNext : %d poussée(s), %d échec(s)", pushed, failed)
    except Exception:
        logger.exception("Échec de la synchronisation ERPNext")
    finally:
        db.close()


def run_erpnext_pull():
    """Relit l'état autoritatif depuis ERPNext (validation/rejet décidés dans le
    desk, adoption des promos créées côté ERPNext). Best-effort."""
    from .erpnext_sync import pull_from_erpnext

    db = SessionLocal()
    try:
        updated, adopted, errors = pull_from_erpnext(db)
        if updated or adopted or errors:
            logger.info("Relecture ERPNext : %d statut(s) mis à jour, %d adoptée(s), %d erreur(s)",
                        updated, adopted, errors)
    except Exception:
        logger.exception("Échec de la relecture ERPNext")
    finally:
        db.close()
