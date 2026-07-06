from datetime import date

from app.date_extraction import extract_validity_dates


def test_textual_range_same_year():
    start, end = extract_validity_dates("Cette offre sera valable du 15 octobre au 30 novembre 2025.")
    assert start == date(2025, 10, 15)
    assert end == date(2025, 11, 30)


def test_textual_end_only():
    start, end = extract_validity_dates("Profitez-en, valable jusqu'au 31/07/26.")
    assert start is None
    assert end == date(2026, 7, 31)


def test_textual_end_only_full_words():
    start, end = extract_validity_dates("Offre jusqu'au 20 octobre 2025 dans votre pharmacie.")
    assert start is None
    assert end == date(2025, 10, 20)


def test_textual_start_only():
    start, end = extract_validity_dates("Disponible à partir du 01/07/2025 en pharmacie.")
    assert start == date(2025, 7, 1)
    assert end is None


def test_textual_start_only_full_words():
    start, end = extract_validity_dates("Disponible à partir du 1er juillet 2025 en pharmacie.")
    assert start == date(2025, 7, 1)
    assert end is None


def test_numeric_range():
    start, end = extract_validity_dates("Promotion du 01/07/2025 au 31/08/2025 dans votre officine.")
    assert start == date(2025, 7, 1)
    assert end == date(2025, 8, 31)


def test_no_match_returns_none_none():
    start, end = extract_validity_dates("Un texte quelconque sans aucune date de validité.")
    assert start is None
    assert end is None


def test_empty_text():
    assert extract_validity_dates("") == (None, None)


def test_invalid_calendar_date_is_ignored():
    # 31 février n'existe pas : on ne doit pas planter, et la borne invalide doit rester None.
    start, end = extract_validity_dates("Valable jusqu'au 31 février 2025.")
    assert start is None
    assert end is None
