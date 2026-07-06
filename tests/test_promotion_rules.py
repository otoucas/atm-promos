from datetime import date

from app.models import Promotion
from app.promotion_rules import find_conflicting_ids


def _promo(id, brand_name="Marque", highco_reference="ref-1", valid_from=None, valid_until=None, product_codes=None):
    return Promotion(
        id=id,
        brand_name=brand_name,
        highco_reference=highco_reference,
        valid_from=valid_from,
        valid_until=valid_until,
        product_codes=product_codes,
    )


def test_same_brand_overlapping_dates_conflict():
    a = _promo(1, valid_from=date(2026, 5, 1), valid_until=date(2026, 7, 31))
    b = _promo(2, valid_from=date(2026, 6, 1), valid_until=date(2026, 8, 31))
    assert find_conflicting_ids([a, b]) == {1, 2}


def test_same_brand_non_overlapping_dates_no_conflict():
    a = _promo(1, valid_from=date(2026, 1, 1), valid_until=date(2026, 2, 28))
    b = _promo(2, valid_from=date(2026, 6, 1), valid_until=date(2026, 8, 31))
    assert find_conflicting_ids([a, b]) == set()


def test_different_brands_no_conflict():
    a = _promo(1, brand_name="Marque A", highco_reference="ref-a", valid_from=date(2026, 5, 1), valid_until=date(2026, 7, 31))
    b = _promo(2, brand_name="Marque B", highco_reference="ref-b", valid_from=date(2026, 5, 1), valid_until=date(2026, 7, 31))
    assert find_conflicting_ids([a, b]) == set()


def test_same_highco_reference_conflicts_even_with_different_brand_names():
    a = _promo(1, brand_name="Marque A", highco_reference="same-link", valid_from=date(2026, 5, 1), valid_until=date(2026, 7, 31))
    b = _promo(2, brand_name="Marque B", highco_reference="same-link", valid_from=date(2026, 5, 1), valid_until=date(2026, 7, 31))
    assert find_conflicting_ids([a, b]) == {1, 2}


def test_missing_dates_never_conflict():
    a = _promo(1, valid_from=None, valid_until=None)
    b = _promo(2, valid_from=date(2026, 5, 1), valid_until=date(2026, 7, 31))
    assert find_conflicting_ids([a, b]) == set()


def test_same_brand_different_product_range_no_conflict():
    # elmex® sensitive vs. elmex® junior : même marque, gammes différentes,
    # codes produit renseignés et disjoints -> pas de conflit malgré le chevauchement de dates.
    a = _promo(1, brand_name="elmex®", highco_reference="ref-a", valid_from=date(2026, 5, 25), valid_until=date(2026, 8, 25), product_codes="1001,1002")
    b = _promo(2, brand_name="elmex®", highco_reference="ref-b", valid_from=date(2026, 5, 25), valid_until=date(2026, 8, 25), product_codes="2001,2002")
    assert find_conflicting_ids([a, b]) == set()


def test_same_brand_overlapping_product_range_conflicts():
    a = _promo(1, brand_name="PIC Solution", highco_reference="ref-a", valid_from=date(2026, 5, 1), valid_until=date(2026, 8, 31), product_codes="1001,1002")
    b = _promo(2, brand_name="PIC Solution", highco_reference="ref-b", valid_from=date(2026, 5, 1), valid_until=date(2026, 8, 31), product_codes="1002,3003")
    assert find_conflicting_ids([a, b]) == {1, 2}


def test_same_brand_falls_back_when_product_codes_not_yet_known():
    # Ni l'une ni l'autre n'a encore de product_codes (promo tout juste ingérée) -> repli sur la marque.
    a = _promo(1, brand_name="Fixodent", highco_reference="ref-a", valid_from=date(2026, 7, 1), valid_until=date(2026, 9, 30))
    b = _promo(2, brand_name="Fixodent", highco_reference="ref-b", valid_from=date(2026, 7, 1), valid_until=date(2026, 9, 30))
    assert find_conflicting_ids([a, b]) == {1, 2}


def test_only_one_side_has_product_codes_falls_back_to_brand():
    a = _promo(1, brand_name="Paranix", highco_reference="ref-a", valid_from=date(2026, 7, 1), valid_until=date(2026, 9, 30), product_codes="4001")
    b = _promo(2, brand_name="Paranix", highco_reference="ref-b", valid_from=date(2026, 7, 1), valid_until=date(2026, 9, 30))
    assert find_conflicting_ids([a, b]) == {1, 2}
