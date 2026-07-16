from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from . import config

connect_args = {"check_same_thread": False} if config.DATABASE_URL.startswith("sqlite") else {}
engine = create_engine(config.DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _ensure_columns(table: str, column_defs: dict):
    """Base.metadata.create_all() ne modifie jamais une table déjà existante —
    sur une base de prod créée avant l'ajout d'une colonne, elle manquerait.
    On l'ajoute à la main si besoin, comme pour les précédents changements de
    schéma de ce projet (pas de framework de migration ici). column_defs :
    {nom_colonne: fragment SQL de définition (type + contraintes)}."""
    with engine.begin() as conn:
        cols = [row[1] for row in conn.exec_driver_sql(f"PRAGMA table_info({table})").fetchall()]
        if not cols:
            return  # table pas encore créée (base toute neuve) — create_all() s'en charge
        for name, definition in column_defs.items():
            if name not in cols:
                conn.exec_driver_sql(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")


def _ensure_store_id_column():
    _ensure_columns("promotions", {"store_id": "INTEGER REFERENCES stores(id)"})


def _ensure_store_contact_columns():
    _ensure_columns(
        "stores",
        {
            "contact_name": "VARCHAR(200)",
            "contact_email": "VARCHAR(255)",
            "verification_token": "VARCHAR(64)",
            "email_verified_at": "DATETIME",
            "password_hash": "VARCHAR(255)",
            "password_reset_token": "VARCHAR(64)",
            "password_reset_requested_at": "DATETIME",
            "mcp_token": "VARCHAR(64)",
            "mcp_auto_publish": "BOOLEAN DEFAULT 0",
            "notifications_enabled": "BOOLEAN DEFAULT 0",
            "notification_email": "VARCHAR(255)",
            "notify_days_before_start": "INTEGER DEFAULT 3",
            "notify_days_before_end": "INTEGER DEFAULT 3",
            "mcp_import_mode": "VARCHAR(10)",
        },
    )


def _ensure_promotion_reminder_columns():
    _ensure_columns(
        "promotions",
        {
            "start_reminder_sent_at": "DATETIME",
            "end_reminder_sent_at": "DATETIME",
        },
    )


def _ensure_default_store_and_backfill():
    from .models import INTEGRATION_ERPNEXT, Promotion, Store

    db = SessionLocal()
    try:
        store = db.query(Store).filter(Store.code == config.DEFAULT_STORE_CODE).first()
        if not store:
            store = Store(
                code=config.DEFAULT_STORE_CODE,
                name=config.DEFAULT_STORE_NAME,
                integration=INTEGRATION_ERPNEXT,
            )
            db.add(store)
            db.commit()
            db.refresh(store)
        db.query(Promotion).filter(Promotion.store_id.is_(None)).update({"store_id": store.id})
        db.commit()
    finally:
        db.close()


def init_db():
    from . import models  # noqa: F401  (ensure models are registered before create_all)

    Base.metadata.create_all(bind=engine)
    _ensure_store_id_column()
    _ensure_store_contact_columns()
    _ensure_promotion_reminder_columns()
    _ensure_default_store_and_backfill()
