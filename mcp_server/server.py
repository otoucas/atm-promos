"""Serveur MCP d'ATM Nifty — permet à un point de vente de connecter SON
PROPRE Claude (Claude Desktop, Claude Code...) pour lire et proposer des
promotions. Volontairement un service séparé du site principal (app/) : ses
dépendances (mcp, dont une version récente de starlette) sont incompatibles
avec celles de FastAPI 0.115 utilisée par l'appli web — les deux tournent
dans des environnements/conteneurs isolés, en ne partageant que la base de
données (même fichier SQLite, même volume Docker).

Authentification : jeton par magasin (Store.mcp_token, généré dans
/{code}/admin/mcp), passé en en-tête HTTP "Authorization: Bearer <jeton>" —
volontairement distinct du mot de passe web humain (voir décision du
2026-07-10 : jeton d'API dédié, révocable indépendamment).
"""

import datetime
import logging
import os

from mcp.server.fastmcp import Context, FastMCP

from app.database import SessionLocal
from app.models import (
    MCP_ACTION_LIST,
    MCP_ACTION_SUBMIT,
    STATUS_ACTIVE,
    STATUS_PENDING,
    SOURCE_MCP,
    McpActivityLog,
    Promotion,
    Store,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp_server")

mcp = FastMCP(
    "ATM Nifty",
    instructions=(
        "Outils pour lire et proposer les promotions Nifty d'un point de vente. "
        "Authentification par jeton (en-tête Authorization: Bearer <jeton>), "
        "généré dans les réglages du point de vente (/{code}/admin/mcp)."
    ),
    stateless_http=True,
    json_response=True,
    streamable_http_path="/",
)


class McpAuthError(Exception):
    pass


def _authenticate(ctx: Context) -> Store:
    request = ctx.request_context.request
    auth_header = request.headers.get("authorization", "") if request else ""
    token = auth_header.removeprefix("Bearer ").strip()
    if not token:
        raise McpAuthError("En-tête Authorization: Bearer <jeton> manquant.")

    db = SessionLocal()
    try:
        store = db.query(Store).filter(Store.mcp_token == token, Store.is_active.is_(True)).first()
        if not store:
            raise McpAuthError("Jeton invalide, révoqué, ou point de vente désactivé.")
        return store
    finally:
        db.close()


def _log_activity(store_id: int, action: str, detail: str | None = None) -> None:
    db = SessionLocal()
    try:
        db.add(McpActivityLog(store_id=store_id, action=action, detail=detail))
        db.commit()
    finally:
        db.close()


@mcp.tool()
def list_promotions(ctx: Context) -> list[dict]:
    """Liste les promotions actuelles de votre point de vente (actives et en
    attente de validation), pour éviter de proposer un doublon."""
    store = _authenticate(ctx)
    db = SessionLocal()
    try:
        promotions = (
            db.query(Promotion)
            .filter(Promotion.store_id == store.id, Promotion.status.in_([STATUS_ACTIVE, STATUS_PENDING]))
            .order_by(Promotion.brand_name)
            .all()
        )
        result = [
            {
                "id": p.id,
                "brand_name": p.brand_name,
                "operation_label": p.operation_label,
                "concerned_products": p.concerned_products,
                "valid_from": p.valid_from.isoformat() if p.valid_from else None,
                "valid_until": p.valid_until.isoformat() if p.valid_until else None,
                "status": p.status,
                "source": p.source,
            }
            for p in promotions
        ]
    finally:
        db.close()

    _log_activity(store.id, MCP_ACTION_LIST, f"{len(result)} promotion(s) lues")
    return result


@mcp.tool()
def submit_promotion(
    ctx: Context,
    brand_name: str,
    highco_reference: str,
    operation_label: str = "",
    valid_from: str = "",
    valid_until: str = "",
    concerned_products: str = "",
) -> dict:
    """Propose une nouvelle promotion pour votre point de vente.

    brand_name: nom de la marque (obligatoire).
    highco_reference: lien ou identifiant HighCo extrait du QR code/coupon
        (obligatoire — sans lui, aucun code ne pourra être généré en caisse).
    operation_label: montant/pourcentage de la remise (ex: "2€", "20%").
    valid_from / valid_until: dates ISO (AAAA-MM-JJ), si connues.
    concerned_products: produits couverts par cette offre, texte libre.

    Selon les réglages du point de vente (/{code}/admin/mcp), la promotion
    part directement active (visible au comptoir) ou attend une validation
    manuelle — dans tous les cas, un email de contact reste disponible dans
    l'historique du point de vente pour vérifier ce que l'IA a soumis.
    """
    store = _authenticate(ctx)

    if not brand_name.strip():
        raise ValueError("brand_name est obligatoire.")
    if not highco_reference.strip():
        raise ValueError("highco_reference est obligatoire (lien ou identifiant du QR HighCo).")

    def _parse_date(value: str):
        value = value.strip()
        if not value:
            return None
        return datetime.date.fromisoformat(value)

    status = STATUS_ACTIVE if store.mcp_auto_publish else STATUS_PENDING

    db = SessionLocal()
    try:
        promo = Promotion(
            store_id=store.id,
            brand_name=brand_name.strip(),
            operation_label=operation_label.strip() or None,
            highco_reference=highco_reference.strip(),
            concerned_products=concerned_products.strip() or None,
            valid_from=_parse_date(valid_from),
            valid_until=_parse_date(valid_until),
            status=status,
            source=SOURCE_MCP,
            validated_at=datetime.datetime.utcnow() if status == STATUS_ACTIVE else None,
        )
        db.add(promo)
        db.commit()
        db.refresh(promo)
        promo_id = promo.id
    finally:
        db.close()

    _log_activity(
        store.id,
        MCP_ACTION_SUBMIT,
        f"{brand_name.strip()} — {'publiée directement' if status == STATUS_ACTIVE else 'en attente de validation'}",
    )

    return {
        "id": promo_id,
        "status": status,
        "message": (
            "Promotion créée et publiée directement (comptoir)."
            if status == STATUS_ACTIVE
            else "Promotion créée, en attente de validation manuelle par le point de vente."
        ),
    }


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("MCP_BIND_HOST", "0.0.0.0")
    port = int(os.environ.get("MCP_PORT", "8000"))
    uvicorn.run(mcp.streamable_http_app(), host=host, port=port)
