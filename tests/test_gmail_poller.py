from datetime import date

from app.gmail_poller import (
    _find_mergeable_promotion,
    _guess_brand_name,
    _guess_operation_label,
    _merge_into_existing,
)
from app.models import STATUS_ACTIVE, STATUS_ARCHIVED, STATUS_PENDING, Promotion, Store


def test_guess_brand_name_from_typical_subject():
    assert _guess_brand_name("PROMO [NIFTY] FIXODENT 2€ de remise") == "FIXODENT"


def test_guess_brand_name_without_nifty_marker():
    assert _guess_brand_name("PROMO ELMEX 1€") == "ELMEX"


def test_guess_brand_name_falls_back_to_full_subject():
    assert _guess_brand_name("Une offre spéciale sans le format habituel") == "Une offre spéciale sans le format habituel"


def test_guess_brand_name_empty_subject():
    assert _guess_brand_name("") == "Promotion à nommer"


def test_guess_operation_label_euro_amount():
    assert _guess_operation_label("PROMO [NIFTY] FIXODENT 2€ de remise") == "2€"


def test_guess_operation_label_percentage():
    assert _guess_operation_label("PROMO [NIFTY] P&G 20% de remise") == "20%"


def test_guess_operation_label_no_amount():
    assert _guess_operation_label("PROMO [NIFTY] MARQUE sans montant") == ""


def _make_store(db, code="ART"):
    store = Store(code=code, name="Pharmacie test", integration="erpnext")
    db.add(store)
    db.flush()
    return store


def test_find_mergeable_promotion_matches_same_reference(db):
    store = _make_store(db)
    existing = Promotion(store_id=store.id, brand_name="Fixodent", highco_reference="same-link", status=STATUS_PENDING)
    db.add(existing)
    db.flush()

    found = _find_mergeable_promotion(db, "same-link", store.id)
    assert found is not None
    assert found.id == existing.id


def test_find_mergeable_promotion_ignores_archived(db):
    store = _make_store(db)
    archived = Promotion(store_id=store.id, brand_name="Fixodent", highco_reference="same-link", status=STATUS_ARCHIVED)
    db.add(archived)
    db.flush()

    assert _find_mergeable_promotion(db, "same-link", store.id) is None


def test_find_mergeable_promotion_no_match_for_unknown_reference(db):
    store = _make_store(db)
    db.add(Promotion(store_id=store.id, brand_name="Fixodent", highco_reference="some-link", status=STATUS_ACTIVE))
    db.flush()

    assert _find_mergeable_promotion(db, "other-link", store.id) is None


def test_find_mergeable_promotion_does_not_cross_stores(db):
    """Two different stores could theoretically receive the same HighCo QR
    link — they must never be merged into a single tile."""
    store_a = _make_store(db, "ART")
    store_b = _make_store(db, "LYO")
    db.add(Promotion(store_id=store_a.id, brand_name="Fixodent", highco_reference="same-link", status=STATUS_ACTIVE))
    db.flush()

    assert _find_mergeable_promotion(db, "same-link", store_b.id) is None


def test_merge_into_existing_appends_new_product():
    existing = Promotion(brand_name="Fixodent", highco_reference="ref", concerned_products="Fixodent")
    _merge_into_existing(existing, "Fixodent Extra Fort", date(2026, 7, 1), date(2026, 9, 30), None)
    assert "Fixodent Extra Fort" in existing.concerned_products
    assert "Fixodent" in existing.concerned_products


def test_merge_into_existing_does_not_duplicate_already_listed_product():
    existing = Promotion(brand_name="Fixodent", highco_reference="ref", concerned_products="Fixodent, Fixodent Extra Fort")
    _merge_into_existing(existing, "Fixodent Extra Fort", None, None, None)
    assert existing.concerned_products.count("Fixodent Extra Fort") == 1


def test_merge_into_existing_fills_missing_start_date():
    existing = Promotion(brand_name="Fixodent", highco_reference="ref", valid_from=None, valid_until=date(2026, 9, 30))
    _merge_into_existing(existing, "Fixodent", date(2026, 7, 1), None, None)
    assert existing.valid_from == date(2026, 7, 1)


def test_merge_into_existing_extends_later_end_date():
    existing = Promotion(brand_name="Fixodent", highco_reference="ref", valid_until=date(2026, 8, 31))
    _merge_into_existing(existing, "Fixodent", None, date(2026, 9, 30), None)
    assert existing.valid_until == date(2026, 9, 30)


def test_merge_into_existing_does_not_shorten_end_date():
    existing = Promotion(brand_name="Fixodent", highco_reference="ref", valid_until=date(2026, 9, 30))
    _merge_into_existing(existing, "Fixodent", None, date(2026, 8, 31), None)
    assert existing.valid_until == date(2026, 9, 30)


def test_merge_into_existing_fills_missing_logo_only():
    existing = Promotion(brand_name="Fixodent", highco_reference="ref", logo_path=None, logo_url=None)
    _merge_into_existing(existing, "Fixodent", None, None, "new-logo.png")
    assert existing.logo_path == "new-logo.png"


def test_merge_into_existing_does_not_override_existing_logo():
    existing = Promotion(brand_name="Fixodent", highco_reference="ref", logo_path="original.png", logo_url=None)
    _merge_into_existing(existing, "Fixodent", None, None, "new-logo.png")
    assert existing.logo_path == "original.png"
