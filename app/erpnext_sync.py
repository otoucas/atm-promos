"""Pont vers ERPNext (module atm_nifty).

Pousse les promotions validées/archivées vers l'endpoint idempotent
`atm_nifty.api.sync.upsert_promotion` du site ERPNext. La clé d'idempotence
est l'id local de la promotion (`service_id` côté ERPNext), donc re-pousser
la même promotion met simplement à jour l'enregistrement.

Best-effort : toute erreur réseau/ERP est journalisée mais n'interrompt
jamais le fonctionnement du service (le comptoir doit marcher même ERP down).
Actif uniquement si ERPNEXT_URL + clés sont renseignés dans le .env.
"""

import logging

import httpx

from . import config
from .models import STATUS_ACTIVE, STATUS_ARCHIVED, STATUS_PENDING, Promotion

logger = logging.getLogger("erpnext_sync")

_ENDPOINT = "/api/method/atm_nifty.api.sync.upsert_promotion"


def _fmt_dt(value):
    return value.isoformat(sep=" ") if value else None


def _payload(promo: Promotion) -> dict:
    return {
        "service_id": promo.id,
        "brand_name": promo.brand_name or "",
        "operation_label": promo.operation_label,
        "status": promo.status,
        "source": promo.source,
        "valid_from": promo.valid_from.isoformat() if promo.valid_from else None,
        "valid_until": promo.valid_until.isoformat() if promo.valid_until else None,
        "source_message_id": promo.source_message_id,
        "highco_reference": promo.highco_reference or "",
        "concerned_products": promo.concerned_products,
        "product_codes": promo.product_codes,
        "logo_url": promo.logo_url,
        "raw_email_subject": promo.raw_email_subject,
        "validated_at": _fmt_dt(promo.validated_at),
        "archived_at": _fmt_dt(promo.archived_at),
        "generated_codes": [
            {"code": c.code, "generated_at": _fmt_dt(c.generated_at)}
            for c in (promo.generated_codes or [])
        ],
    }


def push_promotion(promo: Promotion) -> bool:
    """Pousse une promotion vers ERPNext. Retourne True si acceptée."""
    if not config.ERPNEXT_SYNC_ENABLED:
        return False
    headers = {
        "Authorization": f"token {config.ERPNEXT_API_KEY}:{config.ERPNEXT_API_SECRET}"
    }
    try:
        resp = httpx.post(
            config.ERPNEXT_URL.rstrip("/") + _ENDPOINT,
            json=_payload(promo),
            headers=headers,
            timeout=15.0,
        )
        resp.raise_for_status()
        return True
    except Exception as exc:  # best-effort : on log et on continue
        logger.warning(
            "Push ERPNext échoué pour la promotion %s (%s) : %s",
            promo.id,
            promo.brand_name,
            exc,
        )
        return False


def sync_all(db) -> tuple[int, int]:
    """Ré-pousse toutes les promotions actives et archivées (upsert idempotent).
    Rattrape ainsi les envois qui auraient échoué et propage les changements
    de statut. Retourne (nb_poussées, nb_échecs)."""
    if not config.ERPNEXT_SYNC_ENABLED:
        return (0, 0)
    promos = (
        db.query(Promotion)
        .filter(Promotion.status.in_([STATUS_PENDING, STATUS_ACTIVE, STATUS_ARCHIVED]))
        .all()
    )
    pushed = failed = 0
    for promo in promos:
        if push_promotion(promo):
            pushed += 1
        else:
            failed += 1
    return pushed, failed
