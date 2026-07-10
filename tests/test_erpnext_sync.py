"""Le pont ERPNext ne doit concerner que le point de vente historique
(Artemare, integration=erpnext) — les promotions des autres points de vente
(format dépannage) ne doivent jamais être poussées ni relues depuis ERPNext.
"""

from app import config, erpnext_sync
from app.models import INTEGRATION_ERPNEXT, INTEGRATION_STANDALONE, STATUS_ACTIVE, Promotion, Store


def _make_store(db, code, integration):
    store = Store(code=code, name=f"Point de vente {code}", integration=integration)
    db.add(store)
    db.commit()
    return store


def test_sync_all_disabled_returns_zero(db, monkeypatch):
    monkeypatch.setattr(config, "ERPNEXT_SYNC_ENABLED", False)
    assert erpnext_sync.sync_all(db) == (0, 0)


def test_sync_all_returns_zero_when_no_erpnext_store_exists(db, monkeypatch):
    monkeypatch.setattr(config, "ERPNEXT_SYNC_ENABLED", True)
    assert erpnext_sync.sync_all(db) == (0, 0)


def test_sync_all_only_pushes_erpnext_store_promotions(db, monkeypatch):
    monkeypatch.setattr(config, "ERPNEXT_SYNC_ENABLED", True)
    art = _make_store(db, config.DEFAULT_STORE_CODE, INTEGRATION_ERPNEXT)
    other = _make_store(db, "LYO", INTEGRATION_STANDALONE)
    db.add(Promotion(store_id=art.id, brand_name="Fixodent", highco_reference="ref-art", status=STATUS_ACTIVE))
    db.add(Promotion(store_id=other.id, brand_name="Compeed", highco_reference="ref-lyo", status=STATUS_ACTIVE))
    db.commit()

    pushed_brands = []
    monkeypatch.setattr(erpnext_sync, "push_promotion", lambda promo: pushed_brands.append(promo.brand_name) or True)

    pushed, failed = erpnext_sync.sync_all(db)

    assert pushed == 1
    assert failed == 0
    assert pushed_brands == ["Fixodent"]


def test_pull_from_erpnext_returns_zero_when_no_erpnext_store_exists(db, monkeypatch):
    monkeypatch.setattr(config, "ERPNEXT_SYNC_ENABLED", True)
    assert erpnext_sync.pull_from_erpnext(db) == (0, 0, 0)


def test_pull_from_erpnext_adopted_promotion_attached_to_erpnext_store(db, monkeypatch):
    monkeypatch.setattr(config, "ERPNEXT_SYNC_ENABLED", True)
    art = _make_store(db, config.DEFAULT_STORE_CODE, INTEGRATION_ERPNEXT)

    class FakeResponse:
        def raise_for_status(self):
            pass

        def json(self):
            return {
                "message": [
                    {
                        "name": "AP-0001",
                        "service_id": None,
                        "status": STATUS_ACTIVE,
                        "brand_name": "Nouvelle Marque",
                        "highco_reference": "ref-desk",
                    }
                ]
            }

    monkeypatch.setattr(erpnext_sync.httpx, "get", lambda *a, **k: FakeResponse())
    monkeypatch.setattr(erpnext_sync.httpx, "post", lambda *a, **k: FakeResponse())

    updated, adopted, errors = erpnext_sync.pull_from_erpnext(db)

    assert (updated, adopted, errors) == (0, 1, 0)
    promo = db.query(Promotion).filter(Promotion.highco_reference == "ref-desk").first()
    assert promo is not None
    assert promo.store_id == art.id
