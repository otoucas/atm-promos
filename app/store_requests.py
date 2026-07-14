"""Circuit de demande d'ouverture d'un point de vente : un email de contact
(@hellopharmacie.com) est collecté à la création (via /superadmin/stores/new,
typiquement à partir des réponses d'un formulaire externe rempli par les
autres pharmacies du groupement), doit être confirmé par un lien avant que le
sigle ne devienne actif, et une alerte est envoyée si un sigle déjà pris (actif
ou en attente de confirmation) est redemandé.
"""

import logging
import secrets
import smtplib
from email.mime.text import MIMEText

from . import config

logger = logging.getLogger("store_requests")


def generate_verification_token() -> str:
    return secrets.token_urlsafe(32)


def build_contact_email(local_part: str) -> str:
    return f"{local_part.strip().lower()}@{config.STORE_CONTACT_EMAIL_DOMAIN}"


def _send_email(subject: str, body: str, to: str) -> None:
    """Envoie depuis l'adresse @hellopharmacie.com dédiée (config.STORE_EMAIL_*)
    — jamais depuis la boîte Gmail personnelle (config.GMAIL_ADDRESS), qui ne
    sert qu'au relevé des promos Nifty. Voir CLAUDE.md."""
    if not config.STORE_EMAIL_SMTP_HOST or not config.STORE_EMAIL_ADDRESS or not config.STORE_EMAIL_PASSWORD:
        raise RuntimeError("STORE_EMAIL_SMTP_HOST / STORE_EMAIL_ADDRESS / STORE_EMAIL_PASSWORD non configurés")

    msg = MIMEText(body, _charset="utf-8")
    msg["Subject"] = subject
    msg["From"] = config.STORE_EMAIL_ADDRESS
    msg["To"] = to

    with smtplib.SMTP_SSL(config.STORE_EMAIL_SMTP_HOST, config.STORE_EMAIL_SMTP_PORT) as smtp:
        smtp.login(config.STORE_EMAIL_ADDRESS, config.STORE_EMAIL_PASSWORD)
        smtp.sendmail(config.STORE_EMAIL_ADDRESS, [to], msg.as_string())


def send_verification_email(store) -> bool:
    """Envoie le lien de confirmation au contact du point de vente. Best-effort :
    le magasin reste créé (mais inactif) même si l'envoi échoue — Olivier peut
    toujours renvoyer le lien à la main (il est journalisé ci-dessous)."""
    link = f"{config.PUBLIC_BASE_URL}/verify/{store.verification_token}"
    subject = f"Confirmez l'ouverture du point de vente « {store.name} » ({store.code})"
    body = (
        f"Bonjour{' ' + store.contact_name if store.contact_name else ''},\n\n"
        f"Une demande d'ouverture de page Nifty a été enregistrée pour « {store.name} » "
        f"(sigle {store.code}).\n\n"
        f"Pour l'activer, cliquez sur ce lien :\n{link}\n\n"
        "Si vous n'êtes pas à l'origine de cette demande, ignorez simplement cet e-mail."
    )
    try:
        _send_email(subject, body, store.contact_email)
        return True
    except Exception:
        logger.exception(
            "Échec de l'envoi de l'email de confirmation pour le magasin %s (%s) — lien : %s",
            store.code, store.contact_email, link,
        )
        return False


def send_password_reset_email(store) -> bool:
    link = f"{config.PUBLIC_BASE_URL}/{store.code}/admin/reset-password/{store.password_reset_token}"
    subject = f"Réinitialisation du mot de passe — {store.name} ({store.code})"
    body = (
        f"Bonjour{' ' + store.contact_name if store.contact_name else ''},\n\n"
        f"Une demande de réinitialisation du mot de passe a été faite pour « {store.name} » (sigle {store.code}).\n\n"
        f"Pour choisir un nouveau mot de passe, cliquez sur ce lien "
        f"(valable {config.PASSWORD_RESET_TOKEN_VALIDITY_DAYS} jours) :\n{link}\n\n"
        "Si vous n'êtes pas à l'origine de cette demande, ignorez simplement cet e-mail."
    )
    try:
        _send_email(subject, body, store.contact_email)
        return True
    except Exception:
        logger.exception(
            "Échec de l'envoi de l'email de réinitialisation pour le magasin %s (%s) — lien : %s",
            store.code, store.contact_email, link,
        )
        return False


def send_suspicious_request_alert(code: str, name: str, contact_email: str, reasons: list) -> bool:
    """La demande a bien été créée, mais ne correspond pas au fichier maître
    du groupement (voir app/contacts_directory.py) — alerte Olivier pour
    vérification a posteriori, sans jamais bloquer la création (les points
    de vente "dépannage" hors réseau sont légitimement absents de ce fichier)."""
    if not config.STORE_ALERT_RECIPIENT:
        logger.warning("STORE_ALERT_RECIPIENT non configuré — alerte de demande étonnante (%s) non envoyée", code)
        return False
    subject = f"[ATM Nifty] Demande d'ouverture à vérifier — sigle « {code} »"
    body = (
        f"Une demande d'ouverture de point de vente a été créée pour « {name} » (sigle {code}), "
        f"contact soumis : {contact_email}.\n\n"
        "Elle ne correspond pas exactement au fichier maître du groupement :\n\n"
        + "\n".join(f"- {r}" for r in reasons)
        + "\n\nÀ vérifier manuellement si besoin (rien n'a été bloqué)."
    )
    try:
        _send_email(subject, body, config.STORE_ALERT_RECIPIENT)
        return True
    except Exception:
        logger.exception("Échec de l'envoi de l'alerte de demande étonnante pour %s", code)
        return False


def send_dev_suggestion(store, message: str) -> bool:
    """Envoie une suggestion de développement laissée par un point de vente
    depuis la page d'aide (/{code}/admin/help) — même adresse que les autres
    alertes (config.STORE_ALERT_RECIPIENT), pas de nouvelle boîte dédiée."""
    if not config.STORE_ALERT_RECIPIENT:
        logger.warning("STORE_ALERT_RECIPIENT non configuré — suggestion de %s non envoyée", store.code)
        return False
    subject = f"[ATM Nifty] Suggestion de « {store.name} » ({store.code})"
    body = (
        f"Suggestion envoyée depuis la page d'aide par « {store.name} » (sigle {store.code}, "
        f"contact : {store.contact_email or '(aucun)'}) :\n\n{message}"
    )
    try:
        _send_email(subject, body, config.STORE_ALERT_RECIPIENT)
        return True
    except Exception:
        logger.exception("Échec de l'envoi de la suggestion de développement pour %s", store.code)
        return False


def send_duplicate_code_alert(code: str, existing_store, requested_contact_email: str) -> bool:
    """Le sigle demandé existe déjà (actif ou en attente de confirmation),
    éventuellement avec un contact différent — alerte Olivier plutôt que de
    laisser passer silencieusement."""
    if not config.STORE_ALERT_RECIPIENT:
        logger.warning("STORE_ALERT_RECIPIENT non configuré — alerte de sigle en doublon (%s) non envoyée", code)
        return False
    status = "confirmé" if existing_store.email_verified_at else "en attente de confirmation"
    subject = f"[ATM Nifty] Sigle « {code} » redemandé"
    body = (
        f"Le sigle « {code} » a été redemandé (contact soumis : {requested_contact_email}).\n\n"
        f"Il existe déjà pour « {existing_store.name} », statut {status}, "
        f"contact enregistré : {existing_store.contact_email or '(aucun)'}.\n\n"
        "À vérifier manuellement — la nouvelle demande n'a pas été créée."
    )
    try:
        _send_email(subject, body, config.STORE_ALERT_RECIPIENT)
        return True
    except Exception:
        logger.exception("Échec de l'envoi de l'alerte de sigle en doublon pour %s", code)
        return False
