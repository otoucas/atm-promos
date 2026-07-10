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


def _ensure_store_id_column():
    """Base.metadata.create_all() ne modifie jamais une table déjà existante
    — sur une base de prod créée avant l'introduction du multi-magasins, la
    table promotions n'a pas encore la colonne store_id. On l'ajoute à la main
    si besoin, comme pour les précédents changements de schéma de ce projet
    (pas de framework de migration ici)."""
    with engine.begin() as conn:
        cols = [row[1] for row in conn.exec_driver_sql("PRAGMA table_info(promotions)").fetchall()]
        if cols and "store_id" not in cols:
            conn.exec_driver_sql("ALTER TABLE promotions ADD COLUMN store_id INTEGER REFERENCES stores(id)")


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
    _ensure_default_store_and_backfill()
