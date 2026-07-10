import csv
import datetime
import io
import logging
import uuid
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import config, highco
from .auth import check_password, is_admin
from .database import get_db, init_db
from .gmail_poller import poll_gmail_once
from .jobs import run_auto_archive, run_daily_review, run_erpnext_pull, run_erpnext_sync, run_gmail_poll
from .monthly_preview import run_monthly_preview
from .logos import fetch_logo_url
from .models import (
    INTEGRATION_ERPNEXT,
    INTEGRATION_STANDALONE,
    STATUS_ACTIVE,
    STATUS_ARCHIVED,
    STATUS_PENDING,
    SOURCE_MANUAL,
    GeneratedCode,
    ProcessedEmail,
    Promotion,
    Store,
)
from .promotion_rules import find_conflicting_ids
from .qrcode_utils import extract_qr_payload

logging.basicConfig(level=logging.INFO)

app = FastAPI(title="Codes promo pharmacie")
app.add_middleware(SessionMiddleware, secret_key=config.SECRET_KEY)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")
app.mount("/media/logos", StaticFiles(directory=config.LOGO_DIR), name="logos")

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")

scheduler = BackgroundScheduler()


@app.on_event("startup")
def on_startup():
    init_db()
    if not config.DISABLE_GMAIL_POLLER:
        scheduler.add_job(run_gmail_poll, "interval", minutes=config.POLL_INTERVAL_MINUTES, id="gmail_poll")
    scheduler.add_job(run_auto_archive, "interval", minutes=config.ARCHIVE_CHECK_INTERVAL_MINUTES, id="auto_archive")
    scheduler.add_job(
        run_daily_review, CronTrigger(hour=config.DAILY_REVIEW_HOUR, minute=0), id="daily_review"
    )
    scheduler.add_job(
        run_monthly_preview,
        CronTrigger(day=config.MONTHLY_PREVIEW_DAY, hour=config.MONTHLY_PREVIEW_HOUR, minute=0),
        id="monthly_preview",
    )
    if config.ERPNEXT_SYNC_ENABLED:
        scheduler.add_job(
            run_erpnext_sync, "interval", minutes=config.ERPNEXT_SYNC_INTERVAL_MINUTES, id="erpnext_sync"
        )
    if config.ERPNEXT_SYNC_ENABLED:
        scheduler.add_job(
            run_erpnext_pull, "interval", minutes=config.ERPNEXT_PULL_INTERVAL_MINUTES, id="erpnext_pull"
        )
    scheduler.start()


@app.on_event("shutdown")
def on_shutdown():
    scheduler.shutdown(wait=False)


# ---------------------------------------------------------------------------
# Multi-magasins — résolution du point de vente à partir de l'URL.
#
# Un seul jeu de routes sert tous les points de vente, préfixées par le code
# à 3 lettres du point de vente (/{code}/...). Le point de vente historique
# (Artemare, integration=erpnext) reste en plus accessible via les anciennes
# adresses sans préfixe (/, /admin/*, ...) pour ne rien casser des favoris /
# de la documentation existante — ce sont de simples alias vers le même code
# métier, résolus vers le magasin par défaut (config.DEFAULT_STORE_CODE).
# ---------------------------------------------------------------------------


# En-tête posée uniquement par la passerelle nginx publique
# (atm.hellopharmacie.com/nifty/, voir déploiement du 2026-07-10) — jamais
# présente sur un accès direct via Tailscale (100.99.14.86:8010). Sert de
# double sécurité, EN PLUS des règles nginx qui bloquent déjà ces chemins :
# même si la config nginx était un jour mal réglée, l'appli refuse quand même
# de servir Artemare/admin/superadmin/legacy à travers cette passerelle.
_PUBLIC_GATEWAY_HEADER = "x-nifty-public-gateway"


def _is_public_gateway(request: Request) -> bool:
    return request.headers.get(_PUBLIC_GATEWAY_HEADER) == "1"


def _mount_prefix(request: Request) -> str:
    """Préfixe de chemin ajouté par le reverse proxy public (ex: "/nifty"),
    à répercuter sur les liens absolus vers /static et /media/logos. Vide sur
    un accès direct Tailscale (pas de reverse proxy entre les deux)."""
    return request.headers.get("x-forwarded-prefix", "").rstrip("/")


def get_store_by_code(request: Request, code: str, db: Session = Depends(get_db)) -> Store:
    store = db.query(Store).filter(Store.code == code.upper(), Store.is_active.is_(True)).first()
    if not store:
        raise HTTPException(status_code=404, detail="Point de vente inconnu")
    if _is_public_gateway(request) and store.integration == INTEGRATION_ERPNEXT:
        # Ne devrait déjà plus arriver ici (bloqué par nginx) — filet de
        # sécurité applicatif si la config nginx changeait un jour.
        raise HTTPException(status_code=404, detail="Point de vente inconnu")
    return store


def get_default_store(request: Request, db: Session = Depends(get_db)) -> Store:
    if _is_public_gateway(request):
        raise HTTPException(status_code=404, detail="Introuvable")
    store = db.query(Store).filter(Store.code == config.DEFAULT_STORE_CODE).first()
    if not store:
        raise HTTPException(status_code=500, detail="Magasin par défaut introuvable")
    return store


def _require_store_admin(request: Request, store: Store):
    """Un point de vente "erpnext" (Artemare aujourd'hui) garde le mot de
    passe admin historique. Un point de vente "standalone" (format de
    dépannage) n'a volontairement aucune authentification sur ses propres
    pages de réglages — demande explicite : ces pages doivent rester
    utilisables sans code par l'équipe du point de vente, y compris depuis
    l'extérieur du réseau Tailscale."""
    if store.integration != INTEGRATION_ERPNEXT:
        return
    if not is_admin(request):
        raise HTTPException(status_code=307, headers={"Location": f"/{store.code}/admin/login"})


def _require_superadmin(request: Request):
    if _is_public_gateway(request):
        raise HTTPException(status_code=404, detail="Introuvable")
    if not is_admin(request):
        raise HTTPException(status_code=307, headers={"Location": "/superadmin/login"})


# ---------------------------------------------------------------------------
# Anti-abus sur la génération de code — voir config.CODE_GENERATION_RATE_LIMIT_*.
# Chaque appel réussi consomme un vrai code HighCo à usage unique ; sans
# limite, une page publique sans mot de passe permettrait de vider les codes
# d'une promotion en boucle.
# ---------------------------------------------------------------------------


def _rate_limited(db: Session, promotion_id: int) -> bool:
    window_start = datetime.datetime.utcnow() - datetime.timedelta(
        minutes=config.CODE_GENERATION_RATE_LIMIT_WINDOW_MINUTES
    )
    recent_count = (
        db.query(GeneratedCode)
        .filter(GeneratedCode.promotion_id == promotion_id, GeneratedCode.generated_at >= window_start)
        .count()
    )
    return recent_count >= config.CODE_GENERATION_RATE_LIMIT_COUNT


# ---------------------------------------------------------------------------
# Grille (opérateur/public) — pas d'authentification, c'est la page destinée
# au comptoir (et, pour les points de vente en dépannage, potentiellement
# exposée sur Internet).
# ---------------------------------------------------------------------------


def _active_promotions(db: Session, store_id: int):
    today = datetime.date.today()
    promotions = (
        db.query(Promotion)
        .filter(
            Promotion.store_id == store_id,
            Promotion.status == STATUS_ACTIVE,
            (Promotion.valid_until.is_(None)) | (Promotion.valid_until >= today),
            (Promotion.valid_from.is_(None)) | (Promotion.valid_from <= today),
        )
        .order_by(Promotion.brand_name)
        .all()
    )
    return promotions, find_conflicting_ids(promotions)


def _grid_response(request: Request, db: Session, store: Store):
    promotions, conflict_ids = _active_promotions(db, store.id)
    return templates.TemplateResponse(
        "operator_grid.html",
        {
            "request": request, "mount_prefix": _mount_prefix(request),
            "promotions": promotions,
            "conflict_ids": conflict_ids,
            "url_prefix": f"/{store.code}",
            "store": store,
        },
    )


@app.get("/{code}/", response_class=HTMLResponse)
def grid_for_store(request: Request, db: Session = Depends(get_db), store: Store = Depends(get_store_by_code)):
    return _grid_response(request, db, store)


@app.get("/", response_class=HTMLResponse)
def grid_legacy(request: Request, db: Session = Depends(get_db), store: Store = Depends(get_default_store)):
    return _grid_response(request, db, store)


@app.get("/promotions", response_class=HTMLResponse)
def grid_legacy_promotions_alias(request: Request, db: Session = Depends(get_db), store: Store = Depends(get_default_store)):
    """Ancienne adresse (avant l'authentification admin) — conservée pour ne pas casser
    un lien déjà en place (ex: accueil ERPNext)."""
    return _grid_response(request, db, store)


def _generate_code_response(promotion_id: int, request: Request, db: Session, store: Store):
    promo = db.query(Promotion).filter(Promotion.id == promotion_id, Promotion.store_id == store.id).first()
    if not promo or promo.status != STATUS_ACTIVE:
        raise HTTPException(status_code=404, detail="Promotion introuvable ou inactive")

    code = None
    error = None
    if _rate_limited(db, promo.id):
        error = "Trop de codes générés récemment pour cette promotion — réessayez dans quelques minutes."
    else:
        try:
            code = highco.generate_code(promo.highco_reference)
        except highco.HighCoResponseError as exc:
            error = str(exc)

    if code:
        db.add(GeneratedCode(promotion_id=promo.id, code=code))
        db.commit()

    context = {"request": request, "mount_prefix": _mount_prefix(request), "promotion": promo, "code": code, "error": error, "url_prefix": f"/{store.code}"}
    # La grille appelle cette route via fetch() pour afficher le code dans une
    # pop-up sans navigation — repli sur la page complète si JS est coupé
    # (navigation classique du <form>, sans cet en-tête).
    template_name = "code_modal.html" if request.headers.get("X-Requested-With") == "fetch" else "code_display.html"
    return templates.TemplateResponse(template_name, context)


@app.post("/{code}/generate/{promotion_id}", response_class=HTMLResponse)
def generate_code_for_store(
    promotion_id: int, request: Request, db: Session = Depends(get_db), store: Store = Depends(get_store_by_code)
):
    return _generate_code_response(promotion_id, request, db, store)


@app.post("/generate/{promotion_id}", response_class=HTMLResponse)
def generate_code_legacy(
    promotion_id: int, request: Request, db: Session = Depends(get_db), store: Store = Depends(get_default_store)
):
    return _generate_code_response(promotion_id, request, db, store)


# ---------------------------------------------------------------------------
# Admin par point de vente — protégé par mot de passe uniquement pour les
# points de vente "erpnext" (Artemare). Pour les points de vente "standalone"
# (format dépannage), ces mêmes pages sont ouvertes sans code, demande
# explicite pour que l'équipe du point de vente s'en serve seule.
# ---------------------------------------------------------------------------


def _admin_login_form_response(request: Request, store: Store):
    return templates.TemplateResponse(
        "admin_login.html", {"request": request, "mount_prefix": _mount_prefix(request), "error": None, "url_prefix": f"/{store.code}"}
    )


@app.get("/{code}/admin/login", response_class=HTMLResponse)
def admin_login_form_for_store(request: Request, store: Store = Depends(get_store_by_code)):
    return _admin_login_form_response(request, store)


@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_form_legacy(request: Request, store: Store = Depends(get_default_store)):
    return _admin_login_form_response(request, store)


def _admin_login_response(request: Request, store: Store, password: str):
    if check_password(password):
        request.session["is_admin"] = True
        return RedirectResponse(f"/{store.code}/admin/pending", status_code=303)
    return templates.TemplateResponse(
        "admin_login.html",
        {"request": request, "mount_prefix": _mount_prefix(request), "error": "Mot de passe incorrect", "url_prefix": f"/{store.code}"},
        status_code=401,
    )


@app.post("/{code}/admin/login", response_class=HTMLResponse)
def admin_login_for_store(request: Request, store: Store = Depends(get_store_by_code), password: str = Form(...)):
    return _admin_login_response(request, store, password)


@app.post("/admin/login", response_class=HTMLResponse)
def admin_login_legacy(request: Request, store: Store = Depends(get_default_store), password: str = Form(...)):
    return _admin_login_response(request, store, password)


@app.post("/{code}/admin/logout")
def admin_logout_for_store(request: Request, store: Store = Depends(get_store_by_code)):
    request.session.clear()
    return RedirectResponse(f"/{store.code}/", status_code=303)


@app.post("/admin/logout")
def admin_logout_legacy(request: Request, store: Store = Depends(get_default_store)):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


def _admin_pending_response(request: Request, db: Session, store: Store):
    _require_store_admin(request, store)
    pending = (
        db.query(Promotion)
        .filter(Promotion.store_id == store.id, Promotion.status == STATUS_PENDING)
        .order_by(Promotion.brand_name)
        .all()
    )
    complete = [p for p in pending if p.is_complete]
    incomplete = [p for p in pending if not p.is_complete]
    return templates.TemplateResponse(
        "admin_pending.html",
        {
            "request": request, "mount_prefix": _mount_prefix(request),
            "complete": complete,
            "incomplete": incomplete,
            "flash": request.query_params.get("flash"),
            "url_prefix": f"/{store.code}",
            "store": store,
        },
    )


@app.get("/{code}/admin/pending", response_class=HTMLResponse)
def admin_pending_for_store(request: Request, db: Session = Depends(get_db), store: Store = Depends(get_store_by_code)):
    return _admin_pending_response(request, db, store)


@app.get("/admin/pending", response_class=HTMLResponse)
def admin_pending_legacy(request: Request, db: Session = Depends(get_db), store: Store = Depends(get_default_store)):
    return _admin_pending_response(request, db, store)


async def _admin_pending_validate_response(request: Request, db: Session, store: Store):
    _require_store_admin(request, store)
    form = await request.form()
    selected_ids = {int(v) for v in form.getlist("selected")}

    pending = db.query(Promotion).filter(Promotion.store_id == store.id, Promotion.status == STATUS_PENDING).all()
    validated_count = 0
    for promo in pending:
        brand_key, op_key, products_key, from_key, until_key = (
            f"brand_name_{promo.id}",
            f"operation_label_{promo.id}",
            f"concerned_products_{promo.id}",
            f"valid_from_{promo.id}",
            f"valid_until_{promo.id}",
        )
        if brand_key in form:
            promo.brand_name = form[brand_key] or promo.brand_name
        if op_key in form:
            promo.operation_label = form[op_key] or None
        if products_key in form:
            promo.concerned_products = form[products_key] or None
        if from_key in form and form[from_key]:
            promo.valid_from = datetime.date.fromisoformat(form[from_key])
        if until_key in form and form[until_key]:
            promo.valid_until = datetime.date.fromisoformat(form[until_key])

        if promo.id in selected_ids:
            promo.status = STATUS_ACTIVE
            promo.validated_at = datetime.datetime.utcnow()
            validated_count += 1

    db.commit()
    return RedirectResponse(
        f"/{store.code}/admin/pending?flash={validated_count} promotion(s) validée(s)", status_code=303
    )


@app.post("/{code}/admin/pending/validate")
async def admin_pending_validate_for_store(
    request: Request, db: Session = Depends(get_db), store: Store = Depends(get_store_by_code)
):
    return await _admin_pending_validate_response(request, db, store)


@app.post("/admin/pending/validate")
async def admin_pending_validate_legacy(
    request: Request, db: Session = Depends(get_db), store: Store = Depends(get_default_store)
):
    return await _admin_pending_validate_response(request, db, store)


async def _admin_pending_reject_reprocess_response(request: Request, db: Session, store: Store):
    """Discards the selected pending promotions AND their processed-email
    marker, so the next Gmail poll re-reads the source email from scratch —
    for cases where the extracted data is wrong/incomplete and a fresh pass
    might do better (e.g. after fixing the parsing logic)."""
    _require_store_admin(request, store)
    form = await request.form()
    selected_ids = {int(v) for v in form.getlist("selected")}

    count = 0
    for promo in db.query(Promotion).filter(Promotion.id.in_(selected_ids), Promotion.store_id == store.id).all():
        if promo.source_message_id:
            db.query(ProcessedEmail).filter_by(message_id=promo.source_message_id).delete()
        db.delete(promo)
        count += 1

    db.commit()
    return RedirectResponse(
        f"/{store.code}/admin/pending?flash={count} promotion(s) rejetée(s) — relues au prochain relevé Gmail",
        status_code=303,
    )


@app.post("/{code}/admin/pending/reject-reprocess")
async def admin_pending_reject_reprocess_for_store(
    request: Request, db: Session = Depends(get_db), store: Store = Depends(get_store_by_code)
):
    return await _admin_pending_reject_reprocess_response(request, db, store)


@app.post("/admin/pending/reject-reprocess")
async def admin_pending_reject_reprocess_legacy(
    request: Request, db: Session = Depends(get_db), store: Store = Depends(get_default_store)
):
    return await _admin_pending_reject_reprocess_response(request, db, store)


async def _admin_pending_reject_archive_response(request: Request, db: Session, store: Store):
    """Discards the selected pending promotions for good — archived without
    ever appearing at the till, and not re-read from the source email."""
    _require_store_admin(request, store)
    form = await request.form()
    selected_ids = {int(v) for v in form.getlist("selected")}

    count = 0
    for promo in db.query(Promotion).filter(Promotion.id.in_(selected_ids), Promotion.store_id == store.id).all():
        promo.status = STATUS_ARCHIVED
        promo.archived_at = datetime.datetime.utcnow()
        count += 1

    db.commit()
    return RedirectResponse(
        f"/{store.code}/admin/pending?flash={count} promotion(s) rejetée(s) et archivée(s)", status_code=303
    )


@app.post("/{code}/admin/pending/reject-archive")
async def admin_pending_reject_archive_for_store(
    request: Request, db: Session = Depends(get_db), store: Store = Depends(get_store_by_code)
):
    return await _admin_pending_reject_archive_response(request, db, store)


@app.post("/admin/pending/reject-archive")
async def admin_pending_reject_archive_legacy(
    request: Request, db: Session = Depends(get_db), store: Store = Depends(get_default_store)
):
    return await _admin_pending_reject_archive_response(request, db, store)


def _admin_delete_promotion_response(promotion_id: int, request: Request, db: Session, store: Store):
    _require_store_admin(request, store)
    promo = db.query(Promotion).filter(Promotion.id == promotion_id, Promotion.store_id == store.id).first()
    if promo:
        db.delete(promo)
        db.commit()
    referer = request.headers.get("referer", f"/{store.code}/admin/pending")
    return RedirectResponse(referer, status_code=303)


@app.post("/{code}/admin/promotions/{promotion_id}/delete")
def admin_delete_promotion_for_store(
    promotion_id: int, request: Request, db: Session = Depends(get_db), store: Store = Depends(get_store_by_code)
):
    return _admin_delete_promotion_response(promotion_id, request, db, store)


@app.post("/admin/promotions/{promotion_id}/delete")
def admin_delete_promotion_legacy(
    promotion_id: int, request: Request, db: Session = Depends(get_db), store: Store = Depends(get_default_store)
):
    return _admin_delete_promotion_response(promotion_id, request, db, store)


def _admin_promotions_response(request: Request, db: Session, store: Store):
    _require_store_admin(request, store)
    active = (
        db.query(Promotion)
        .filter(Promotion.store_id == store.id, Promotion.status == STATUS_ACTIVE)
        .order_by(Promotion.brand_name)
        .all()
    )
    archived = (
        db.query(Promotion)
        .filter(Promotion.store_id == store.id, Promotion.status == STATUS_ARCHIVED)
        .order_by(Promotion.archived_at.desc())
        .all()
    )
    conflict_ids = find_conflicting_ids(active)
    return templates.TemplateResponse(
        "admin_promotions.html",
        {
            "request": request, "mount_prefix": _mount_prefix(request),
            "active": active,
            "archived": archived,
            "conflict_ids": conflict_ids,
            "url_prefix": f"/{store.code}",
            "store": store,
        },
    )


@app.get("/{code}/admin/promotions", response_class=HTMLResponse)
def admin_promotions_for_store(request: Request, db: Session = Depends(get_db), store: Store = Depends(get_store_by_code)):
    return _admin_promotions_response(request, db, store)


@app.get("/admin/promotions", response_class=HTMLResponse)
def admin_promotions_legacy(request: Request, db: Session = Depends(get_db), store: Store = Depends(get_default_store)):
    return _admin_promotions_response(request, db, store)


def _admin_archive_promotion_response(promotion_id: int, request: Request, db: Session, store: Store):
    _require_store_admin(request, store)
    promo = db.query(Promotion).filter(Promotion.id == promotion_id, Promotion.store_id == store.id).first()
    if promo:
        promo.status = STATUS_ARCHIVED
        promo.archived_at = datetime.datetime.utcnow()
        db.commit()
    return RedirectResponse(f"/{store.code}/admin/promotions", status_code=303)


@app.post("/{code}/admin/promotions/{promotion_id}/archive")
def admin_archive_promotion_for_store(
    promotion_id: int, request: Request, db: Session = Depends(get_db), store: Store = Depends(get_store_by_code)
):
    return _admin_archive_promotion_response(promotion_id, request, db, store)


@app.post("/admin/promotions/{promotion_id}/archive")
def admin_archive_promotion_legacy(
    promotion_id: int, request: Request, db: Session = Depends(get_db), store: Store = Depends(get_default_store)
):
    return _admin_archive_promotion_response(promotion_id, request, db, store)


async def _admin_replace_logo_response(promotion_id: int, request: Request, db: Session, store: Store, logo: UploadFile):
    _require_store_admin(request, store)
    promo = db.query(Promotion).filter(Promotion.id == promotion_id, Promotion.store_id == store.id).first()
    if not promo:
        raise HTTPException(status_code=404)
    suffix = Path(logo.filename or "logo.png").suffix or ".png"
    filename = f"{uuid.uuid4().hex}{suffix}"
    dest = config.LOGO_DIR / filename
    dest.write_bytes(await logo.read())
    promo.logo_path = filename
    db.commit()
    return RedirectResponse(f"/{store.code}/admin/promotions", status_code=303)


@app.post("/{code}/admin/promotions/{promotion_id}/logo")
async def admin_replace_logo_for_store(
    promotion_id: int,
    request: Request,
    db: Session = Depends(get_db),
    store: Store = Depends(get_store_by_code),
    logo: UploadFile = File(...),
):
    return await _admin_replace_logo_response(promotion_id, request, db, store, logo)


@app.post("/admin/promotions/{promotion_id}/logo")
async def admin_replace_logo_legacy(
    promotion_id: int,
    request: Request,
    db: Session = Depends(get_db),
    store: Store = Depends(get_default_store),
    logo: UploadFile = File(...),
):
    return await _admin_replace_logo_response(promotion_id, request, db, store, logo)


async def _admin_set_product_codes_response(promotion_id: int, request: Request, db: Session, store: Store):
    """Lets the pharmacist attach the Winpharma product code(s) (CodeProduit)
    that a promotion actually covers — the join key the future Winpharma
    export relies on, since concerned_products is only a free-text label
    mined from the promo email and can't be matched automatically."""
    _require_store_admin(request, store)
    form = await request.form()
    promo = db.query(Promotion).filter(Promotion.id == promotion_id, Promotion.store_id == store.id).first()
    if not promo:
        raise HTTPException(status_code=404)
    promo.product_codes = (form.get("product_codes") or "").strip() or None
    db.commit()
    return RedirectResponse(f"/{store.code}/admin/promotions", status_code=303)


@app.post("/{code}/admin/promotions/{promotion_id}/product-codes")
async def admin_set_product_codes_for_store(
    promotion_id: int, request: Request, db: Session = Depends(get_db), store: Store = Depends(get_store_by_code)
):
    return await _admin_set_product_codes_response(promotion_id, request, db, store)


@app.post("/admin/promotions/{promotion_id}/product-codes")
async def admin_set_product_codes_legacy(
    promotion_id: int, request: Request, db: Session = Depends(get_db), store: Store = Depends(get_default_store)
):
    return await _admin_set_product_codes_response(promotion_id, request, db, store)


def _admin_new_promotion_form_response(request: Request, store: Store):
    _require_store_admin(request, store)
    return templates.TemplateResponse(
        "admin_new_promotion.html", {"request": request, "mount_prefix": _mount_prefix(request), "error": None, "url_prefix": f"/{store.code}", "store": store}
    )


@app.get("/{code}/admin/promotions/new", response_class=HTMLResponse)
def admin_new_promotion_form_for_store(request: Request, store: Store = Depends(get_store_by_code)):
    return _admin_new_promotion_form_response(request, store)


@app.get("/admin/promotions/new", response_class=HTMLResponse)
def admin_new_promotion_form_legacy(request: Request, store: Store = Depends(get_default_store)):
    return _admin_new_promotion_form_response(request, store)


async def _admin_new_promotion_response(
    request: Request,
    db: Session,
    store: Store,
    brand_name: str,
    operation_label: str,
    valid_from: str,
    valid_until: str,
    highco_reference: str,
    qr_file: UploadFile | None,
):
    _require_store_admin(request, store)

    reference = (highco_reference or "").strip()
    if not reference and qr_file is not None and qr_file.filename:
        data = await qr_file.read()
        reference = extract_qr_payload(data, filename=qr_file.filename, content_type=qr_file.content_type or "") or ""

    if not reference:
        return templates.TemplateResponse(
            "admin_new_promotion.html",
            {
                "request": request, "mount_prefix": _mount_prefix(request),
                "error": "Impossible de déterminer la référence HighCo (QR illisible et aucun lien fourni).",
                "url_prefix": f"/{store.code}",
                "store": store,
            },
            status_code=400,
        )

    # Manual entries are reviewed by the admin at creation time, so they go
    # straight to "active" rather than through the pending queue.
    promo = Promotion(
        store_id=store.id,
        brand_name=brand_name.strip(),
        operation_label=operation_label.strip() or None,
        highco_reference=reference,
        valid_from=datetime.date.fromisoformat(valid_from) if valid_from else None,
        valid_until=datetime.date.fromisoformat(valid_until) if valid_until else None,
        status=STATUS_ACTIVE,
        source=SOURCE_MANUAL,
        validated_at=datetime.datetime.utcnow(),
        logo_url=fetch_logo_url(brand_name.strip()),
    )
    db.add(promo)
    db.commit()
    return RedirectResponse(f"/{store.code}/admin/promotions", status_code=303)


@app.post("/{code}/admin/promotions/new", response_class=HTMLResponse)
async def admin_new_promotion_for_store(
    request: Request,
    db: Session = Depends(get_db),
    store: Store = Depends(get_store_by_code),
    brand_name: str = Form(...),
    operation_label: str = Form(""),
    valid_from: str = Form(""),
    valid_until: str = Form(""),
    highco_reference: str = Form(""),
    qr_file: UploadFile = File(None),
):
    return await _admin_new_promotion_response(
        request, db, store, brand_name, operation_label, valid_from, valid_until, highco_reference, qr_file
    )


@app.post("/admin/promotions/new", response_class=HTMLResponse)
async def admin_new_promotion_legacy(
    request: Request,
    db: Session = Depends(get_db),
    store: Store = Depends(get_default_store),
    brand_name: str = Form(...),
    operation_label: str = Form(""),
    valid_from: str = Form(""),
    valid_until: str = Form(""),
    highco_reference: str = Form(""),
    qr_file: UploadFile = File(None),
):
    return await _admin_new_promotion_response(
        request, db, store, brand_name, operation_label, valid_from, valid_until, highco_reference, qr_file
    )


def _admin_history_response(request: Request, db: Session, store: Store):
    _require_store_admin(request, store)
    history = (
        db.query(GeneratedCode)
        .join(Promotion)
        .filter(Promotion.store_id == store.id)
        .order_by(GeneratedCode.generated_at.desc())
        .limit(500)
        .all()
    )
    return templates.TemplateResponse(
        "admin_history.html",
        {"request": request, "mount_prefix": _mount_prefix(request), "history": history, "url_prefix": f"/{store.code}", "store": store},
    )


@app.get("/{code}/admin/history", response_class=HTMLResponse)
def admin_history_for_store(request: Request, db: Session = Depends(get_db), store: Store = Depends(get_store_by_code)):
    return _admin_history_response(request, db, store)


@app.get("/admin/history", response_class=HTMLResponse)
def admin_history_legacy(request: Request, db: Session = Depends(get_db), store: Store = Depends(get_default_store)):
    return _admin_history_response(request, db, store)


def _admin_poll_now_response(request: Request, db: Session, store: Store):
    _require_store_admin(request, store)
    if store.integration != INTEGRATION_ERPNEXT:
        raise HTTPException(status_code=404, detail="Pas de relevé Gmail pour ce point de vente")
    created, merged = poll_gmail_once(db)
    return RedirectResponse(
        f"/{store.code}/admin/pending?flash={created} nouvelle(s) promotion(s), {merged} fusionnée(s) avec une promotion existante",
        status_code=303,
    )


@app.post("/{code}/admin/poll-now")
def admin_poll_now_for_store(request: Request, db: Session = Depends(get_db), store: Store = Depends(get_store_by_code)):
    return _admin_poll_now_response(request, db, store)


@app.post("/admin/poll-now")
def admin_poll_now_legacy(request: Request, db: Session = Depends(get_db), store: Store = Depends(get_default_store)):
    return _admin_poll_now_response(request, db, store)


def _admin_export_csv_response(request: Request, db: Session, store: Store):
    """Structured hand-off point for the future Winpharma sync: one row per
    currently-active, currently-in-window promotion, with the Winpharma
    product codes attached by hand (see product-codes route above). Until an
    automated writer exists, this is also directly usable by a human at the
    till to key promotions into WinPromo."""
    _require_store_admin(request, store)
    today = datetime.date.today()
    promotions = (
        db.query(Promotion)
        .filter(
            Promotion.store_id == store.id,
            Promotion.status == STATUS_ACTIVE,
            (Promotion.valid_until.is_(None)) | (Promotion.valid_until >= today),
            (Promotion.valid_from.is_(None)) | (Promotion.valid_from <= today),
        )
        .order_by(Promotion.brand_name)
        .all()
    )

    buffer = io.StringIO()
    writer = csv.writer(buffer, delimiter=";")
    writer.writerow(
        ["marque", "operation", "produits_concernes", "codes_produits_winpharma", "valide_du", "valide_au", "reference_highco"]
    )
    for promo in promotions:
        writer.writerow(
            [
                promo.brand_name,
                promo.operation_label or "",
                promo.concerned_products or "",
                ", ".join(promo.product_codes_list),
                promo.valid_from.isoformat() if promo.valid_from else "",
                promo.valid_until.isoformat() if promo.valid_until else "",
                promo.highco_reference,
            ]
        )
    buffer.seek(0)
    filename = f"promotions_winpharma_{store.code.lower()}_{today.isoformat()}.csv"
    return StreamingResponse(
        buffer,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/{code}/admin/export/promotions.csv")
def admin_export_promotions_csv_for_store(
    request: Request, db: Session = Depends(get_db), store: Store = Depends(get_store_by_code)
):
    return _admin_export_csv_response(request, db, store)


@app.get("/admin/export/promotions.csv")
def admin_export_promotions_csv_legacy(
    request: Request, db: Session = Depends(get_db), store: Store = Depends(get_default_store)
):
    return _admin_export_csv_response(request, db, store)


# ---------------------------------------------------------------------------
# Super-admin — création et supervision des points de vente. Même mot de
# passe/session que l'admin d'Artemare (une seule personne gère les deux).
# ---------------------------------------------------------------------------


@app.get("/superadmin/login", response_class=HTMLResponse)
def superadmin_login_form(request: Request):
    return templates.TemplateResponse("superadmin_login.html", {"request": request, "mount_prefix": _mount_prefix(request), "error": None})


@app.post("/superadmin/login", response_class=HTMLResponse)
def superadmin_login(request: Request, password: str = Form(...)):
    if check_password(password):
        request.session["is_admin"] = True
        return RedirectResponse("/superadmin", status_code=303)
    return templates.TemplateResponse(
        "superadmin_login.html", {"request": request, "mount_prefix": _mount_prefix(request), "error": "Mot de passe incorrect"}, status_code=401
    )


@app.get("/superadmin", response_class=HTMLResponse)
def superadmin_dashboard(request: Request, db: Session = Depends(get_db)):
    _require_superadmin(request)
    today = datetime.date.today()
    stores = db.query(Store).order_by(Store.code).all()
    rows = []
    for store in stores:
        active_count = (
            db.query(Promotion)
            .filter(
                Promotion.store_id == store.id,
                Promotion.status == STATUS_ACTIVE,
                (Promotion.valid_until.is_(None)) | (Promotion.valid_until >= today),
            )
            .count()
        )
        last_code = (
            db.query(GeneratedCode)
            .join(Promotion)
            .filter(Promotion.store_id == store.id)
            .order_by(GeneratedCode.generated_at.desc())
            .first()
        )
        rows.append(
            {
                "store": store,
                "active_count": active_count,
                "last_activity": last_code.generated_at if last_code else None,
            }
        )
    return templates.TemplateResponse(
        "superadmin_dashboard.html",
        {"request": request, "mount_prefix": _mount_prefix(request), "rows": rows, "flash": request.query_params.get("flash")},
    )


@app.get("/superadmin/stores/new", response_class=HTMLResponse)
def superadmin_new_store_form(request: Request):
    _require_superadmin(request)
    return templates.TemplateResponse("superadmin_new_store.html", {"request": request, "mount_prefix": _mount_prefix(request), "error": None})


@app.post("/superadmin/stores/new", response_class=HTMLResponse)
def superadmin_new_store(
    request: Request,
    db: Session = Depends(get_db),
    name: str = Form(...),
    code: str = Form(...),
):
    _require_superadmin(request)
    normalized = code.strip().upper()

    error = None
    if len(normalized) != 3 or not normalized.isalpha():
        error = "Le code doit comporter exactement 3 lettres."
    elif db.query(Store).filter(Store.code == normalized).first():
        error = f"Le code « {normalized} » est déjà utilisé."

    if error:
        return templates.TemplateResponse(
            "superadmin_new_store.html", {"request": request, "mount_prefix": _mount_prefix(request), "error": error}, status_code=400
        )

    store = Store(code=normalized, name=name.strip(), integration=INTEGRATION_STANDALONE)
    db.add(store)
    db.commit()
    return RedirectResponse(f"/superadmin?flash=Point de vente « {store.name} » créé ({store.code})", status_code=303)


@app.post("/superadmin/stores/{store_id}/disable")
def superadmin_disable_store(store_id: int, request: Request, db: Session = Depends(get_db)):
    _require_superadmin(request)
    store = db.get(Store, store_id)
    if store and store.integration != INTEGRATION_ERPNEXT:
        store.is_active = False
        db.commit()
    return RedirectResponse("/superadmin", status_code=303)


@app.post("/superadmin/stores/{store_id}/enable")
def superadmin_enable_store(store_id: int, request: Request, db: Session = Depends(get_db)):
    _require_superadmin(request)
    store = db.get(Store, store_id)
    if store:
        store.is_active = True
        db.commit()
    return RedirectResponse("/superadmin", status_code=303)
