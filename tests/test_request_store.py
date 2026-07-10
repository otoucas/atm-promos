"""Formulaire public d'inscription (/request-store, ajouté le 2026-07-10) —
en libre-service, sans mot de passe, mais avec les mêmes garde-fous que la
création via /superadmin/stores/new : email @hellopharmacie.com obligatoire,
un email = un sigle, alerte en cas de sigle déjà pris, anti-abus.
"""

from fastapi.testclient import TestClient

from app import config, main
from app.models import Store


def _client(db):
    def override_get_db():
        yield db

    main.app.dependency_overrides[main.get_db] = override_get_db
    return TestClient(main.app)


def _form(name, code, contact_name="Jean Dupont", email_local_part="jdupont"):
    return {"name": name, "code": code, "contact_name": contact_name, "email_local_part": email_local_part}


def test_request_store_form_reachable_without_login(db):
    client = _client(db)
    try:
        with client as c:
            resp = c.get("/request-store")
        assert resp.status_code == 200
    finally:
        main.app.dependency_overrides.clear()


def test_request_store_creates_inactive_store_and_sends_verification(db, monkeypatch):
    sent = []
    monkeypatch.setattr(main, "send_verification_email", lambda store: sent.append(store.code) or True)
    client = _client(db)
    try:
        with client as c:
            resp = c.post("/request-store", data=_form("Pharmacie de Lyon", "LYO"))
        assert resp.status_code == 200
        assert "confirmation" in resp.text
        store = db.query(Store).filter(Store.code == "LYO").first()
        assert store is not None
        assert store.is_active is False
        assert sent == ["LYO"]
    finally:
        main.app.dependency_overrides.clear()


def test_request_store_rejects_duplicate_code_and_alerts(db, monkeypatch):
    monkeypatch.setattr(main, "send_verification_email", lambda store: True)
    alerts = []
    monkeypatch.setattr(main, "send_duplicate_code_alert", lambda code, existing, email: alerts.append(code) or True)
    client = _client(db)
    try:
        with client as c:
            c.post("/request-store", data=_form("Pharmacie A", "LYO", email_local_part="premier"))
            resp = c.post("/request-store", data=_form("Pharmacie B", "LYO", email_local_part="second"))
        assert resp.status_code == 400
        assert "déjà utilisé" in resp.text
        assert alerts == ["LYO"]
    finally:
        main.app.dependency_overrides.clear()


def test_request_store_rejects_second_store_for_same_email(db, monkeypatch):
    monkeypatch.setattr(main, "send_verification_email", lambda store: True)
    client = _client(db)
    try:
        with client as c:
            c.post("/request-store", data=_form("Pharmacie A", "AAA", email_local_part="meme"))
            resp = c.post("/request-store", data=_form("Pharmacie B", "BBB", email_local_part="meme"))
        assert resp.status_code == 400
        assert "un seul sigle" in resp.text
        assert db.query(Store).filter(Store.code == "BBB").first() is None
    finally:
        main.app.dependency_overrides.clear()


def test_request_store_rate_limited_after_threshold(db, monkeypatch):
    monkeypatch.setattr(main, "send_verification_email", lambda store: True)
    monkeypatch.setattr(config, "STORE_REQUEST_RATE_LIMIT_COUNT", 2)
    client = _client(db)
    try:
        with client as c:
            c.post("/request-store", data=_form("Pharmacie A", "AAA", email_local_part="a"))
            c.post("/request-store", data=_form("Pharmacie B", "BBB", email_local_part="b"))
            resp = c.post("/request-store", data=_form("Pharmacie C", "CCC", email_local_part="c"))
        assert resp.status_code == 400
        assert "Trop de demandes" in resp.text
        assert db.query(Store).filter(Store.code == "CCC").first() is None
    finally:
        main.app.dependency_overrides.clear()


def test_request_store_does_not_require_superadmin_password(db, monkeypatch):
    """Contrôle négatif : contrairement à /superadmin/stores/new, aucune
    session admin n'est nécessaire ici."""
    monkeypatch.setattr(main, "send_verification_email", lambda store: True)
    client = _client(db)
    try:
        with client as c:
            resp = c.post("/request-store", data=_form("Pharmacie de Lyon", "LYO"), follow_redirects=False)
        assert resp.status_code == 200
    finally:
        main.app.dependency_overrides.clear()
