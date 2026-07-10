"""Pont bidirectionnel avec ERPNext (module atm_nifty).

Sens service -> ERPNext (push) : pousse les promotions (upsert idempotent par
service_id). Best-effort, n'interrompt jamais le service.

Sens ERPNext -> service (pull) : relit l'état autoritatif depuis ERPNext.
ERPNext fait autorité sur la VALIDATION (pending -> active) et le REJET ; le
service adopte ce statut, et récupère les promotions créées dans le desk. Pour
éviter les boucles : l'archivage par date reste calculé des deux côtés (le pull
ne ré-active jamais une promotion expirée).
"""

import datetime
import logging

import httpx

from . import config
from .models import STATUS_ACTIVE, STATUS_ARCHIVED, STATUS_PENDING, Promotion, Store

logger = logging.getLogger("erpnext_sync")


def _erpnext_store(db) -> Store | None:
    """Le pont ERPNext ne concerne que le point de vente historique (Artemare)
    — les autres points de vente (format dépannage) n'y sont jamais poussés
    ni relus depuis ERPNext."""
    return db.query(Store).filter(Store.code == config.DEFAULT_STORE_CODE).first()

_UPSERT = "/api/method/atm_nifty.api.sync.upsert_promotion"
_LIST = "/api/method/atm_nifty.api.sync.list_promotions"
_SET_SERVICE_ID = "/api/method/atm_nifty.api.sync.set_service_id"


def _headers():
    return {"Authorization": f"token {config.ERPNEXT_API_KEY}:{config.ERPNEXT_API_SECRET}"}


def _base():
    return config.ERPNEXT_URL.rstrip("/")


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
    """Pousse une promotion vers ERPNext (best-effort)."""
    if not config.ERPNEXT_SYNC_ENABLED:
        return False
    try:
        resp = httpx.post(_base() + _UPSERT, json=_payload(promo), headers=_headers(), timeout=15.0)
        resp.raise_for_status()
        return True
    except Exception as exc:
        logger.warning("Push ERPNext échoué pour la promotion %s (%s) : %s", promo.id, promo.brand_name, exc)
        return False


def sync_all(db) -> tuple[int, int]:
    """Ré-pousse toutes les promotions actives/archivées/en attente (upsert idempotent)."""
    if not config.ERPNEXT_SYNC_ENABLED:
        return (0, 0)
    store = _erpnext_store(db)
    if not store:
        return (0, 0)
    promos = (
        db.query(Promotion)
        .filter(
            Promotion.store_id == store.id,
            Promotion.status.in_([STATUS_PENDING, STATUS_ACTIVE, STATUS_ARCHIVED]),
        )
        .all()
    )
    pushed = failed = 0
    for promo in promos:
        if push_promotion(promo):
            pushed += 1
        else:
            failed += 1
    return pushed, failed


def _parse_date(value):
    return datetime.date.fromisoformat(value) if value else None


def pull_from_erpnext(db) -> tuple[int, int, int]:
    """Relit l'état autoritatif depuis ERPNext.

    - Applique le statut ERPNext au local (validation/rejet décidés dans le desk),
      SAUF ne jamais ré-activer une promotion dont la validité est passée.
    - Adopte les promotions créées dans le desk (service_id vide) : création locale
      puis renvoi du service_id vers ERPNext.
    Retourne (statuts_mis_a_jour, adoptees, erreurs).
    """
    if not config.ERPNEXT_SYNC_ENABLED:
        return (0, 0, 0)
    store = _erpnext_store(db)
    if not store:
        return (0, 0, 0)
    try:
        resp = httpx.get(_base() + _LIST, headers=_headers(), timeout=20.0)
        resp.raise_for_status()
        rows = resp.json().get("message", []) or []
    except Exception as exc:
        logger.warning("Pull ERPNext (liste) échoué : %s", exc)
        return (0, 0, 1)

    today = datetime.date.today()
    updated = adopted = errors = 0

    for row in rows:
        try:
            sid = row.get("service_id")
            erp_status = row.get("status")

            if sid:
                promo = db.query(Promotion).filter(Promotion.id == sid, Promotion.store_id == store.id).first()
                if not promo:
                    continue
                changed = False

                if erp_status and erp_status != promo.status:
                    expired = promo.valid_until is not None and promo.valid_until < today
                    resurrecting = expired and erp_status in (STATUS_ACTIVE, STATUS_PENDING)
                    if not resurrecting:
                        promo.status = erp_status
                        if erp_status == STATUS_ACTIVE and not promo.validated_at:
                            promo.validated_at = datetime.datetime.utcnow()
                        if erp_status == STATUS_ARCHIVED and not promo.archived_at:
                            promo.archived_at = datetime.datetime.utcnow()
                        changed = True

                for field in ("brand_name", "operation_label", "product_codes"):
                    val = row.get(field)
                    if val is not None and getattr(promo, field) != val:
                        setattr(promo, field, val)
                        changed = True
                for field in ("valid_from", "valid_until"):
                    nv = _parse_date(row.get(field))
                    if getattr(promo, field) != nv:
                        setattr(promo, field, nv)
                        changed = True

                if changed:
                    updated += 1
            else:
                # Promotion créée dans le desk : on l'adopte côté service.
                if not row.get("highco_reference"):
                    continue
                promo = Promotion(
                    store_id=store.id,
                    brand_name=row.get("brand_name") or "",
                    operation_label=row.get("operation_label"),
                    highco_reference=row.get("highco_reference"),
                    product_codes=row.get("product_codes"),
                    valid_from=_parse_date(row.get("valid_from")),
                    valid_until=_parse_date(row.get("valid_until")),
                    status=erp_status or STATUS_ACTIVE,
                    source="manual",
                )
                db.add(promo)
                db.flush()  # obtient promo.id
                try:
                    r = httpx.post(
                        _base() + _SET_SERVICE_ID,
                        json={"name": row["name"], "service_id": promo.id},
                        headers=_headers(),
                        timeout=15.0,
                    )
                    r.raise_for_status()
                    adopted += 1
                except Exception as exc:
                    logger.warning("Adoption : renvoi service_id vers ERPNext échoué (%s) : %s", row.get("name"), exc)
                    db.rollback()
                    errors += 1
        except Exception:
            errors += 1
            logger.exception("Pull ERPNext : échec sur %s", row.get("name"))

    db.commit()
    return (updated, adopted, errors)
