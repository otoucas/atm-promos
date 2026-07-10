"""Covers the multi-store rollout: each store only sees its own promotions,
"standalone" (dépannage) stores have no password on their own admin pages
while the "erpnext" store (Artemare) keeps the historical login, code
generation is rate-limited, and the superadmin space can create/list stores.
"""

from fastapi.testclient import TestClient

from app import config, highco, main
from app.models import INTEGRATION_ERPNEXT, INTEGRATION_STANDALONE, STATUS_ACTIVE, GeneratedCode, Promotion, Store


def _client(db):
    def override_get_db():
        yield db

    main.app.dependency_overrides[main.get_db] = override_get_db
    return TestClient(main.app)


def _make_store(db, code, integration=INTEGRATION_STANDALONE, name=None):
    # commit (pas juste flush) : sinon la transaction reste ouverte et bloque
    # (SQLite "database is locked") la session séparée que le démarrage de
    # l'app utilise pour créer le magasin par défaut (voir init_db()).
    store = Store(code=code, name=name or f"Point de vente {code}", integration=integration)
    db.add(store)
    db.commit()
    return store


def test_default_store_created_on_startup(db):
    with _client(db):
        pass
    store = db.query(Store).filter(Store.code == config.DEFAULT_STORE_CODE).first()
    assert store is not None
    assert store.integration == INTEGRATION_ERPNEXT
    main.app.dependency_overrides.clear()


def test_each_store_grid_only_shows_its_own_promotions(db):
    store_a = _make_store(db, "LYO")
    store_b = _make_store(db, "GRE")
    db.add(Promotion(store_id=store_a.id, brand_name="Fixodent", highco_reference="ref-a", status=STATUS_ACTIVE))
    db.add(Promotion(store_id=store_b.id, brand_name="Compeed", highco_reference="ref-b", status=STATUS_ACTIVE))
    db.commit()

    client = _client(db)
    try:
        with client as c:
            resp_a = c.get("/LYO/")
            resp_b = c.get("/GRE/")
        assert "Fixodent" in resp_a.text
        assert "Compeed" not in resp_a.text
        assert "Compeed" in resp_b.text
        assert "Fixodent" not in resp_b.text
    finally:
        main.app.dependency_overrides.clear()


def test_unknown_store_code_returns_404(db):
    client = _client(db)
    try:
        with client as c:
            resp = c.get("/ZZZ/")
        assert resp.status_code == 404
    finally:
        main.app.dependency_overrides.clear()


def test_standalone_store_admin_has_no_password(db):
    store = _make_store(db, "LYO")
    client = _client(db)
    try:
        with client as c:
            resp = c.get("/LYO/admin/pending", follow_redirects=False)
        assert resp.status_code == 200
    finally:
        main.app.dependency_overrides.clear()


def test_erpnext_store_admin_requires_login(db):
    client = _client(db)
    try:
        with client as c:
            resp = c.get(f"/{config.DEFAULT_STORE_CODE}/admin/pending", follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"] == f"/{config.DEFAULT_STORE_CODE}/admin/login"
    finally:
        main.app.dependency_overrides.clear()


def test_standalone_store_new_promotion_form_reachable_without_login(db):
    store = _make_store(db, "LYO")
    client = _client(db)
    try:
        with client as c:
            resp = c.get("/LYO/admin/promotions/new")
        assert resp.status_code == 200
    finally:
        main.app.dependency_overrides.clear()


def test_generate_code_rate_limited_after_threshold(db, monkeypatch):
    monkeypatch.setattr(config, "CODE_GENERATION_RATE_LIMIT_COUNT", 2)
    monkeypatch.setattr(highco, "generate_code", lambda ref: "FAKE-CODE")
    store = _make_store(db, "LYO")
    promo = Promotion(store_id=store.id, brand_name="Fixodent", highco_reference="fake-ref", status=STATUS_ACTIVE)
    db.add(promo)
    db.commit()
    db.refresh(promo)

    client = _client(db)
    try:
        with client as c:
            r1 = c.post(f"/LYO/generate/{promo.id}", headers={"X-Requested-With": "fetch"})
            r2 = c.post(f"/LYO/generate/{promo.id}", headers={"X-Requested-With": "fetch"})
            r3 = c.post(f"/LYO/generate/{promo.id}", headers={"X-Requested-With": "fetch"})
        assert "FAKE-CODE" in r1.text
        assert "FAKE-CODE" in r2.text
        assert "FAKE-CODE" not in r3.text
        assert "Trop de codes générés récemment" in r3.text
    finally:
        main.app.dependency_overrides.clear()


def test_generate_code_scoped_to_its_own_store(db, monkeypatch):
    """A promotion belonging to one store must not be generatable through
    another store's URL, even by guessing the numeric id."""
    monkeypatch.setattr(highco, "generate_code", lambda ref: "FAKE-CODE")
    store_a = _make_store(db, "LYO")
    store_b = _make_store(db, "GRE")
    promo = Promotion(store_id=store_a.id, brand_name="Fixodent", highco_reference="fake-ref", status=STATUS_ACTIVE)
    db.add(promo)
    db.commit()
    db.refresh(promo)

    client = _client(db)
    try:
        with client as c:
            resp = c.post(f"/GRE/generate/{promo.id}", headers={"X-Requested-With": "fetch"})
        assert resp.status_code == 404
    finally:
        main.app.dependency_overrides.clear()


def test_superadmin_dashboard_requires_login(db):
    client = _client(db)
    try:
        with client as c:
            resp = c.get("/superadmin", follow_redirects=False)
        assert resp.status_code == 307
        assert resp.headers["location"] == "/superadmin/login"
    finally:
        main.app.dependency_overrides.clear()


def test_superadmin_can_create_store(db):
    client = _client(db)
    try:
        with client as c:
            c.post("/superadmin/login", data={"password": config.ADMIN_PASSWORD})
            resp = c.post("/superadmin/stores/new", data={"name": "Pharmacie de Lyon", "code": "lyo"}, follow_redirects=False)
        assert resp.status_code == 303
        store = db.query(Store).filter(Store.code == "LYO").first()
        assert store is not None
        assert store.name == "Pharmacie de Lyon"
        assert store.integration == INTEGRATION_STANDALONE
    finally:
        main.app.dependency_overrides.clear()


def test_superadmin_rejects_duplicate_code(db):
    _make_store(db, "LYO")
    db.commit()
    client = _client(db)
    try:
        with client as c:
            c.post("/superadmin/login", data={"password": config.ADMIN_PASSWORD})
            resp = c.post("/superadmin/stores/new", data={"name": "Doublon", "code": "LYO"})
        assert resp.status_code == 400
        assert "déjà utilisé" in resp.text
    finally:
        main.app.dependency_overrides.clear()


def test_superadmin_rejects_code_not_three_letters(db):
    client = _client(db)
    try:
        with client as c:
            c.post("/superadmin/login", data={"password": config.ADMIN_PASSWORD})
            resp = c.post("/superadmin/stores/new", data={"name": "Trop long", "code": "LYON"})
        assert resp.status_code == 400
        assert "3 lettres" in resp.text
    finally:
        main.app.dependency_overrides.clear()


def test_erpnext_history_never_shows_other_store_codes(db, monkeypatch):
    """Regression guard for the join in admin_history: history must stay
    scoped per store even though GeneratedCode has no store_id of its own."""
    monkeypatch.setattr(highco, "generate_code", lambda ref: "SECRET-CODE-FOR-OTHER-STORE")
    store_a = _make_store(db, "LYO")
    store_b = _make_store(db, "GRE")
    promo_a = Promotion(store_id=store_a.id, brand_name="Fixodent", highco_reference="ref-a", status=STATUS_ACTIVE)
    db.add(promo_a)
    db.commit()
    db.refresh(promo_a)

    client = _client(db)
    try:
        with client as c:
            c.post(f"/LYO/generate/{promo_a.id}", headers={"X-Requested-With": "fetch"})
            resp = c.get("/GRE/admin/history")
        assert "SECRET-CODE-FOR-OTHER-STORE" not in resp.text
    finally:
        main.app.dependency_overrides.clear()
