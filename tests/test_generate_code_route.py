"""Covers the content-negotiation added for the pop-up UX: the same
/generate/{id} route must return the full page (no-JS fallback) or just the
code-card fragment (AJAX pop-up) depending on the X-Requested-With header.
Never calls the real HighCo mechanism — app.highco.generate_code is patched.
"""

from fastapi.testclient import TestClient

from app import highco, main
from app.models import STATUS_ACTIVE, Promotion


def _make_active_promotion(db):
    promo = Promotion(brand_name="Fixodent", highco_reference="fake-ref", status=STATUS_ACTIVE)
    db.add(promo)
    db.commit()
    db.refresh(promo)
    return promo


def test_generate_code_fetch_header_returns_fragment_only(db, monkeypatch):
    monkeypatch.setattr(highco, "generate_code", lambda ref: "FAKE-CODE-123")
    promo = _make_active_promotion(db)

    def override_get_db():
        yield db

    main.app.dependency_overrides[main.get_db] = override_get_db
    try:
        with TestClient(main.app) as client:
            resp = client.post(f"/generate/{promo.id}", headers={"X-Requested-With": "fetch"})
        assert resp.status_code == 200
        assert "FAKE-CODE-123" in resp.text
        assert "<html" not in resp.text.lower()
        assert "Retour à la grille" not in resp.text
    finally:
        main.app.dependency_overrides.clear()


def test_generate_code_without_fetch_header_returns_full_page(db, monkeypatch):
    monkeypatch.setattr(highco, "generate_code", lambda ref: "FAKE-CODE-456")
    promo = _make_active_promotion(db)

    def override_get_db():
        yield db

    main.app.dependency_overrides[main.get_db] = override_get_db
    try:
        with TestClient(main.app) as client:
            resp = client.post(f"/generate/{promo.id}")
        assert resp.status_code == 200
        assert "FAKE-CODE-456" in resp.text
        assert "<html" in resp.text.lower()
        assert "Retour à la grille" in resp.text
    finally:
        main.app.dependency_overrides.clear()
