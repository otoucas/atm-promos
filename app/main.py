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
from .jobs import run_auto_archive, run_daily_review, run_gmail_poll
from .logos import fetch_logo_url
from .models import (
    STATUS_ACTIVE,
    STATUS_ARCHIVED,
    STATUS_PENDING,
    SOURCE_MANUAL,
    GeneratedCode,
    ProcessedEmail,
    Promotion,
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
    scheduler.start()


@app.on_event("shutdown")
def on_shutdown():
    scheduler.shutdown(wait=False)


def _require_admin(request: Request):
    if not is_admin(request):
        raise HTTPException(status_code=307, headers={"Location": "/admin/login"})


# ---------------------------------------------------------------------------
# Operator view — no auth, meant for the counter's local network only
# ---------------------------------------------------------------------------


def _active_promotions(db: Session):
    today = datetime.date.today()
    promotions = (
        db.query(Promotion)
        .filter(
            Promotion.status == STATUS_ACTIVE,
            (Promotion.valid_until.is_(None)) | (Promotion.valid_until >= today),
            (Promotion.valid_from.is_(None)) | (Promotion.valid_from <= today),
        )
        .order_by(Promotion.brand_name)
        .all()
    )
    return promotions, find_conflicting_ids(promotions)


@app.get("/", response_class=HTMLResponse)
def operator_grid(request: Request, db: Session = Depends(get_db)):
    promotions, conflict_ids = _active_promotions(db)
    return templates.TemplateResponse(
        "operator_grid.html", {"request": request, "promotions": promotions, "conflict_ids": conflict_ids}
    )


# ---------------------------------------------------------------------------
# Public view — same grid, no admin link. Pré-déploiement : le conteneur
# n'écoute que sur l'IP Tailscale (voir docker-compose.yml / BIND_ADDRESS),
# donc cette page n'est pas réellement exposée sur Internet pour l'instant
# malgré l'absence de lien vers /admin/*. Voir page Docmost "ATM Nifty —
# Prompt de reprise" pour les prérequis avant une vraie ouverture publique.
# ---------------------------------------------------------------------------


@app.get("/promotions", response_class=HTMLResponse)
def public_grid(request: Request, db: Session = Depends(get_db)):
    promotions, conflict_ids = _active_promotions(db)
    return templates.TemplateResponse(
        "public_grid.html", {"request": request, "promotions": promotions, "conflict_ids": conflict_ids}
    )


@app.post("/generate/{promotion_id}", response_class=HTMLResponse)
def generate_code(promotion_id: int, request: Request, db: Session = Depends(get_db)):
    promo = db.get(Promotion, promotion_id)
    if not promo or promo.status != STATUS_ACTIVE:
        raise HTTPException(status_code=404, detail="Promotion introuvable ou inactive")

    code = None
    error = None
    try:
        code = highco.generate_code(promo.highco_reference)
    except highco.HighCoResponseError as exc:
        error = str(exc)

    if code:
        db.add(GeneratedCode(promotion_id=promo.id, code=code))
        db.commit()

    return templates.TemplateResponse(
        "code_display.html", {"request": request, "promotion": promo, "code": code, "error": error}
    )


# ---------------------------------------------------------------------------
# Admin — password-protected, manages ingestion/validation/history
# ---------------------------------------------------------------------------


@app.get("/admin/login", response_class=HTMLResponse)
def admin_login_form(request: Request):
    return templates.TemplateResponse("admin_login.html", {"request": request, "error": None})


@app.post("/admin/login", response_class=HTMLResponse)
def admin_login(request: Request, password: str = Form(...)):
    if check_password(password):
        request.session["is_admin"] = True
        return RedirectResponse("/admin/pending", status_code=303)
    return templates.TemplateResponse(
        "admin_login.html", {"request": request, "error": "Mot de passe incorrect"}, status_code=401
    )


@app.post("/admin/logout")
def admin_logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)


@app.get("/admin/pending", response_class=HTMLResponse)
def admin_pending(request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    pending = db.query(Promotion).filter(Promotion.status == STATUS_PENDING).order_by(Promotion.brand_name).all()
    complete = [p for p in pending if p.is_complete]
    incomplete = [p for p in pending if not p.is_complete]
    return templates.TemplateResponse(
        "admin_pending.html",
        {
            "request": request,
            "complete": complete,
            "incomplete": incomplete,
            "flash": request.query_params.get("flash"),
        },
    )


@app.post("/admin/pending/validate")
async def admin_pending_validate(request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    form = await request.form()
    selected_ids = {int(v) for v in form.getlist("selected")}

    pending = db.query(Promotion).filter(Promotion.status == STATUS_PENDING).all()
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
    return RedirectResponse(f"/admin/pending?flash={validated_count} promotion(s) validée(s)", status_code=303)


@app.post("/admin/pending/reject-reprocess")
async def admin_pending_reject_reprocess(request: Request, db: Session = Depends(get_db)):
    """Discards the selected pending promotions AND their processed-email
    marker, so the next Gmail poll re-reads the source email from scratch —
    for cases where the extracted data is wrong/incomplete and a fresh pass
    might do better (e.g. after fixing the parsing logic)."""
    _require_admin(request)
    form = await request.form()
    selected_ids = {int(v) for v in form.getlist("selected")}

    count = 0
    for promo in db.query(Promotion).filter(Promotion.id.in_(selected_ids)).all():
        if promo.source_message_id:
            db.query(ProcessedEmail).filter_by(message_id=promo.source_message_id).delete()
        db.delete(promo)
        count += 1

    db.commit()
    return RedirectResponse(
        f"/admin/pending?flash={count} promotion(s) rejetée(s) — relues au prochain relevé Gmail", status_code=303
    )


@app.post("/admin/pending/reject-archive")
async def admin_pending_reject_archive(request: Request, db: Session = Depends(get_db)):
    """Discards the selected pending promotions for good — archived without
    ever appearing at the till, and not re-read from the source email."""
    _require_admin(request)
    form = await request.form()
    selected_ids = {int(v) for v in form.getlist("selected")}

    count = 0
    for promo in db.query(Promotion).filter(Promotion.id.in_(selected_ids)).all():
        promo.status = STATUS_ARCHIVED
        promo.archived_at = datetime.datetime.utcnow()
        count += 1

    db.commit()
    return RedirectResponse(f"/admin/pending?flash={count} promotion(s) rejetée(s) et archivée(s)", status_code=303)


@app.post("/admin/promotions/{promotion_id}/delete")
def admin_delete_promotion(promotion_id: int, request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    promo = db.get(Promotion, promotion_id)
    if promo:
        db.delete(promo)
        db.commit()
    referer = request.headers.get("referer", "/admin/pending")
    return RedirectResponse(referer, status_code=303)


@app.get("/admin/promotions", response_class=HTMLResponse)
def admin_promotions(request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    active = db.query(Promotion).filter(Promotion.status == STATUS_ACTIVE).order_by(Promotion.brand_name).all()
    archived = (
        db.query(Promotion).filter(Promotion.status == STATUS_ARCHIVED).order_by(Promotion.archived_at.desc()).all()
    )
    conflict_ids = find_conflicting_ids(active)
    return templates.TemplateResponse(
        "admin_promotions.html",
        {"request": request, "active": active, "archived": archived, "conflict_ids": conflict_ids},
    )


@app.post("/admin/promotions/{promotion_id}/archive")
def admin_archive_promotion(promotion_id: int, request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    promo = db.get(Promotion, promotion_id)
    if promo:
        promo.status = STATUS_ARCHIVED
        promo.archived_at = datetime.datetime.utcnow()
        db.commit()
    return RedirectResponse("/admin/promotions", status_code=303)


@app.post("/admin/promotions/{promotion_id}/logo")
async def admin_replace_logo(promotion_id: int, request: Request, db: Session = Depends(get_db), logo: UploadFile = File(...)):
    _require_admin(request)
    promo = db.get(Promotion, promotion_id)
    if not promo:
        raise HTTPException(status_code=404)
    suffix = Path(logo.filename or "logo.png").suffix or ".png"
    filename = f"{uuid.uuid4().hex}{suffix}"
    dest = config.LOGO_DIR / filename
    dest.write_bytes(await logo.read())
    promo.logo_path = filename
    db.commit()
    return RedirectResponse("/admin/promotions", status_code=303)


@app.post("/admin/promotions/{promotion_id}/product-codes")
async def admin_set_product_codes(promotion_id: int, request: Request, db: Session = Depends(get_db)):
    """Lets the pharmacist attach the Winpharma product code(s) (CodeProduit)
    that a promotion actually covers — the join key the future Winpharma
    export relies on, since concerned_products is only a free-text label
    mined from the promo email and can't be matched automatically."""
    _require_admin(request)
    form = await request.form()
    promo = db.get(Promotion, promotion_id)
    if not promo:
        raise HTTPException(status_code=404)
    promo.product_codes = (form.get("product_codes") or "").strip() or None
    db.commit()
    return RedirectResponse("/admin/promotions", status_code=303)


@app.get("/admin/promotions/new", response_class=HTMLResponse)
def admin_new_promotion_form(request: Request):
    _require_admin(request)
    return templates.TemplateResponse("admin_new_promotion.html", {"request": request, "error": None})


@app.post("/admin/promotions/new", response_class=HTMLResponse)
async def admin_new_promotion(
    request: Request,
    db: Session = Depends(get_db),
    brand_name: str = Form(...),
    operation_label: str = Form(""),
    valid_from: str = Form(""),
    valid_until: str = Form(""),
    highco_reference: str = Form(""),
    qr_file: UploadFile = File(None),
):
    _require_admin(request)

    reference = (highco_reference or "").strip()
    if not reference and qr_file is not None and qr_file.filename:
        data = await qr_file.read()
        reference = extract_qr_payload(data, filename=qr_file.filename, content_type=qr_file.content_type or "") or ""

    if not reference:
        return templates.TemplateResponse(
            "admin_new_promotion.html",
            {"request": request, "error": "Impossible de déterminer la référence HighCo (QR illisible et aucun lien fourni)."},
            status_code=400,
        )

    # Manual entries are reviewed by the admin at creation time, so they go
    # straight to "active" rather than through the pending queue.
    promo = Promotion(
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
    return RedirectResponse("/admin/promotions", status_code=303)


@app.get("/admin/history", response_class=HTMLResponse)
def admin_history(request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    history = db.query(GeneratedCode).order_by(GeneratedCode.generated_at.desc()).limit(500).all()
    return templates.TemplateResponse("admin_history.html", {"request": request, "history": history})


@app.post("/admin/poll-now")
def admin_poll_now(request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    created, merged = poll_gmail_once(db)
    return RedirectResponse(
        f"/admin/pending?flash={created} nouvelle(s) promotion(s), {merged} fusionnée(s) avec une promotion existante",
        status_code=303,
    )


@app.get("/admin/export/promotions.csv")
def admin_export_promotions_csv(request: Request, db: Session = Depends(get_db)):
    """Structured hand-off point for the future Winpharma sync: one row per
    currently-active, currently-in-window promotion, with the Winpharma
    product codes attached by hand (see product-codes route above). Until an
    automated writer exists, this is also directly usable by a human at the
    till to key promotions into WinPromo."""
    _require_admin(request)
    today = datetime.date.today()
    promotions = (
        db.query(Promotion)
        .filter(
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
    filename = f"promotions_winpharma_{today.isoformat()}.csv"
    return StreamingResponse(
        buffer,
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
