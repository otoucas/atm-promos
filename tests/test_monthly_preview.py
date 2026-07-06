import email
from datetime import date

from app import config, monthly_preview
from app.models import STATUS_ACTIVE, STATUS_PENDING, Promotion
from app.monthly_preview import _next_month_range, build_preview_email, send_email


def test_next_month_range_normal_month():
    start, end = _next_month_range(date(2026, 7, 6))
    assert start == date(2026, 8, 1)
    assert end == date(2026, 8, 31)


def test_next_month_range_december_rolls_over_to_january():
    start, end = _next_month_range(date(2026, 12, 15))
    assert start == date(2027, 1, 1)
    assert end == date(2027, 1, 31)


def test_next_month_range_handles_leap_february():
    start, end = _next_month_range(date(2028, 1, 10))
    assert start == date(2028, 2, 1)
    assert end == date(2028, 2, 29)


def test_build_preview_email_lists_pending_starting_ending_and_conflicts(db):
    # Une en attente de validation, peu importe ses dates.
    db.add(Promotion(brand_name="Fixodent", highco_reference="ref-pending", status=STATUS_PENDING))
    # Une active qui démarre le mois prochain.
    db.add(
        Promotion(
            brand_name="Compeed",
            highco_reference="ref-starting",
            status=STATUS_ACTIVE,
            valid_from=date(2026, 8, 1),
            valid_until=date(2026, 10, 31),
        )
    )
    # Une active qui se termine le mois prochain.
    db.add(
        Promotion(
            brand_name="Voltaheat",
            highco_reference="ref-ending",
            status=STATUS_ACTIVE,
            valid_from=date(2026, 5, 1),
            valid_until=date(2026, 8, 15),
        )
    )
    # Deux actives en conflit (même marque, dates qui se chevauchent, pas de product_codes).
    db.add(
        Promotion(
            brand_name="PIC Solution",
            highco_reference="ref-conflict-a",
            status=STATUS_ACTIVE,
            valid_from=date(2026, 5, 1),
            valid_until=date(2026, 9, 30),
        )
    )
    db.add(
        Promotion(
            brand_name="PIC Solution",
            highco_reference="ref-conflict-b",
            status=STATUS_ACTIVE,
            valid_from=date(2026, 6, 1),
            valid_until=date(2026, 9, 30),
        )
    )
    db.flush()

    subject, body = build_preview_email(db, today=date(2026, 7, 25))

    assert "août 2026" in subject
    assert "Fixodent" in body
    assert "Compeed" in body
    assert "Voltaheat" in body
    assert "PIC Solution" in body
    assert "Conflits actifs non résolus" in body


def test_build_preview_email_handles_empty_state(db):
    subject, body = build_preview_email(db, today=date(2026, 7, 25))
    assert "août 2026" in subject
    assert "Rien en attente de validation" in body


def test_send_email_requires_recipient(monkeypatch):
    monkeypatch.setattr(config, "GMAIL_ADDRESS", "boite@example.com")
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "fake-app-password")
    monkeypatch.setattr(config, "MONTHLY_PREVIEW_RECIPIENT", "")
    try:
        send_email("Sujet", "Corps")
        assert False, "aurait dû lever une exception"
    except RuntimeError as exc:
        assert "MONTHLY_PREVIEW_RECIPIENT" in str(exc)


def test_send_email_uses_smtp_ssl_with_gmail_credentials(monkeypatch):
    monkeypatch.setattr(config, "GMAIL_ADDRESS", "boite@example.com")
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "fake-app-password")
    monkeypatch.setattr(config, "MONTHLY_PREVIEW_RECIPIENT", "olivier.toucas@hellopharmacie.com")

    calls = {}

    class FakeSMTP:
        def __init__(self, host, port):
            calls["host"] = host
            calls["port"] = port

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def login(self, user, password):
            calls["login"] = (user, password)

        def sendmail(self, from_addr, to_addrs, message):
            calls["sendmail"] = (from_addr, to_addrs, message)

    monkeypatch.setattr(monthly_preview.smtplib, "SMTP_SSL", FakeSMTP)

    send_email("Sujet du test", "Corps du test")

    assert calls["host"] == "smtp.gmail.com"
    assert calls["port"] == 465
    assert calls["login"] == ("boite@example.com", "fake-app-password")
    from_addr, to_addrs, message = calls["sendmail"]
    assert from_addr == "boite@example.com"
    assert to_addrs == ["olivier.toucas@hellopharmacie.com"]
    assert "Sujet du test" in message
    parsed = email.message_from_string(message)
    assert parsed.get_payload(decode=True).decode("utf-8") == "Corps du test"


def test_run_monthly_preview_skips_when_no_recipient_configured(monkeypatch, caplog):
    monkeypatch.setattr(config, "MONTHLY_PREVIEW_RECIPIENT", "")
    called = []
    monkeypatch.setattr(monthly_preview, "send_email", lambda *a, **k: called.append(True))

    monthly_preview.run_monthly_preview()

    assert called == []
