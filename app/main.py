import datetime
import logging
import uuid
from pathlib import Path

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from . import config, highco
from .auth import check_password, is_admin
from .database import get_db, init_db
from .gmail_poller import poll_gmail_once
from .jobs import run_auto_archive, run_gmail_poll
from .logos import fetch_logo_url
from .models import (
    STATUS_ACTIVE,
    STATUS_ARCHIVED,
    STATUS_PENDING,
    SOURCE_MANUAL,
    GeneratedCode,
    Promotion,
)
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


@app.get("/", response_class=HTMLResponse)
def operator_grid(request: Request, db: Session = Depends(get_db)):
    today = datetime.date.today()
    promotions = (
        db.query(Promotion)
        .filter(
            Promotion.status == STATUS_ACTIVE,
            (Promotion.valid_until.is_(None)) | (Promotion.valid_until >= today),
        )
        .order_by(Promotion.brand_name)
        .all()
    )
    return templates.TemplateResponse("operator_grid.html", {"request": request, "promotions": promotions})


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
    pending = db.query(Promotion).filter(Promotion.status == STATUS_PENDING).order_by(Promotion.created_at).all()
    return templates.TemplateResponse("admin_pending.html", {"request": request, "pending": pending, "flash": request.query_params.get("flash")})


@app.post("/admin/pending/validate")
async def admin_pending_validate(request: Request, db: Session = Depends(get_db)):
    _require_admin(request)
    form = await request.form()
    selected_ids = {int(v) for v in form.getlist("selected")}

    pending = db.query(Promotion).filter(Promotion.status == STATUS_PENDING).all()
    validated_count = 0
    for promo in pending:
        brand_key, op_key, from_key, until_key = (
            f"brand_name_{promo.id}",
            f"operation_label_{promo.id}",
            f"valid_from_{promo.id}",
            f"valid_until_{promo.id}",
        )
        if brand_key in form:
            promo.brand_name = form[brand_key] or promo.brand_name
        if op_key in form:
            promo.operation_label = form[op_key] or None
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
    return templates.TemplateResponse(
        "admin_promotions.html", {"request": request, "active": active, "archived": archived}
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
    created = poll_gmail_once(db)
    return RedirectResponse(f"/admin/pending?flash={created} nouvelle(s) promotion(s) importée(s)", status_code=303)
