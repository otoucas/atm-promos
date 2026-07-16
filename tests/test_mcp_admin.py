"""Réglages MCP par magasin (/{code}/admin/mcp, ajouté le 2026-07-10) : jeton
de connexion, choix auto-publication vs file d'attente, journal des
lectures/soumissions consultable et supprimable.
"""

from fastapi.testclient import TestClient

from app import main
from app.models import INTEGRATION_STANDALONE, MCP_ACTION_LIST, MCP_ACTION_SUBMIT, McpActivityLog, Store


def _client(db):
    def override_get_db():
        yield db

    main.app.dependency_overrides[main.get_db] = override_get_db
    return TestClient(main.app)


def _make_store(db, code, integration=INTEGRATION_STANDALONE):
    store = Store(code=code, name=f"Point de vente {code}", integration=integration)
    db.add(store)
    db.commit()
    return store


def _login_as_store(client, db, store, password="un-bon-mot-de-passe"):
    """Fait passer le magasin par le circuit de vérification pour obtenir une
    session admin authentifiée (les routes MCP sont protégées comme les
    autres pages de réglages)."""
    from app.auth import hash_password

    store.password_hash = hash_password(password)
    if not store.contact_email:
        store.contact_email = "x@hellopharmacie.com"
    db.commit()
    client.post(f"/{store.code}/admin/login", data={"email": store.contact_email, "password": password})


def test_mcp_page_requires_login(db):
    store = _make_store(db, "LYO")
    client = _client(db)
    try:
        with client as c:
            resp = c.get("/LYO/admin/mcp", follow_redirects=False)
        assert resp.status_code == 307
    finally:
        main.app.dependency_overrides.clear()


def test_generate_and_regenerate_token(db):
    store = _make_store(db, "LYO")
    client = _client(db)
    try:
        with client as c:
            _login_as_store(c, db, store)
            resp = c.get("/LYO/admin/mcp")
            assert resp.status_code == 200
            assert "Aucun jeton" in resp.text

            c.post("/LYO/admin/mcp/token")
        db.refresh(store)
        first_token = store.mcp_token
        assert first_token

        with client as c:
            c.post("/LYO/admin/mcp/token")
        db.refresh(store)
        assert store.mcp_token != first_token
    finally:
        main.app.dependency_overrides.clear()


def test_toggle_auto_publish(db):
    store = _make_store(db, "LYO")
    client = _client(db)
    try:
        with client as c:
            _login_as_store(c, db, store)
            assert store.mcp_auto_publish is False
            c.post("/LYO/admin/mcp/auto-publish", data={"auto_publish": "true"})
        db.refresh(store)
        assert store.mcp_auto_publish is True

        with client as c:
            c.post("/LYO/admin/mcp/auto-publish", data={})
        db.refresh(store)
        assert store.mcp_auto_publish is False
    finally:
        main.app.dependency_overrides.clear()


def test_activity_log_visible_and_deletable(db):
    store = _make_store(db, "LYO")
    db.add(McpActivityLog(store_id=store.id, action=MCP_ACTION_LIST, detail=None))
    db.add(McpActivityLog(store_id=store.id, action=MCP_ACTION_SUBMIT, detail="Fixodent"))
    db.commit()
    log_id = db.query(McpActivityLog).filter_by(detail="Fixodent").first().id

    client = _client(db)
    try:
        with client as c:
            _login_as_store(c, db, store)
            resp = c.get("/LYO/admin/mcp")
            assert "Fixodent" in resp.text
            assert "Lecture des promotions" in resp.text

            c.post(f"/LYO/admin/mcp/log/{log_id}/delete")
        assert db.query(McpActivityLog).filter_by(id=log_id).first() is None
        assert db.query(McpActivityLog).filter_by(store_id=store.id).count() == 1

        with client as c:
            c.post("/LYO/admin/mcp/log/clear")
        assert db.query(McpActivityLog).filter_by(store_id=store.id).count() == 0
    finally:
        main.app.dependency_overrides.clear()


def test_mcp_page_shows_skill_download_and_no_gmail_wording(db):
    store = _make_store(db, "LYO")
    client = _client(db)
    try:
        with client as c:
            _login_as_store(c, db, store)
            resp = c.get("/LYO/admin/mcp")
        assert "NIFTY Coopérateur" in resp.text
        assert "/static/downloads/nifty-cooperateur.zip" in resp.text
        assert "Gmail" not in resp.text
    finally:
        main.app.dependency_overrides.clear()


def test_set_import_mode(db):
    store = _make_store(db, "LYO")
    client = _client(db)
    try:
        with client as c:
            _login_as_store(c, db, store)
            assert store.mcp_import_mode is None
            c.post("/LYO/admin/mcp/import-mode", data={"import_mode": "folder"})
        db.refresh(store)
        assert store.mcp_import_mode == "folder"

        with client as c:
            c.post("/LYO/admin/mcp/import-mode", data={"import_mode": "email"})
        db.refresh(store)
        assert store.mcp_import_mode == "email"
    finally:
        main.app.dependency_overrides.clear()


def test_activity_log_deleted_when_store_deleted(db):
    """cascade="all, delete-orphan" sur Store.mcp_activity_logs : supprimer un
    magasin ne doit pas laisser de logs orphelins."""
    store = _make_store(db, "LYO")
    db.add(McpActivityLog(store_id=store.id, action=MCP_ACTION_LIST, detail=None))
    db.commit()

    db.delete(store)
    db.commit()

    assert db.query(McpActivityLog).count() == 0
