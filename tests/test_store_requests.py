import email

from app import config, store_requests


def test_build_contact_email_appends_domain_and_lowercases():
    assert store_requests.build_contact_email("JDupont") == f"jdupont@{config.STORE_CONTACT_EMAIL_DOMAIN}"


def test_build_contact_email_strips_whitespace():
    assert store_requests.build_contact_email("  jdupont  ") == f"jdupont@{config.STORE_CONTACT_EMAIL_DOMAIN}"


def test_generate_verification_token_is_unique_and_url_safe():
    tokens = {store_requests.generate_verification_token() for _ in range(50)}
    assert len(tokens) == 50
    for token in tokens:
        assert len(token) > 20
        assert all(c.isalnum() or c in "-_" for c in token)


def test_send_verification_email_returns_false_without_raising_when_not_configured(monkeypatch):
    """Volontairement séparé de GMAIL_ADDRESS (relevé Nifty) — tant que les
    identifiants @hellopharmacie.com ne sont pas fournis, l'envoi échoue
    proprement plutôt que de se replier sur la boîte Gmail personnelle."""
    monkeypatch.setattr(config, "STORE_EMAIL_SMTP_HOST", "")
    monkeypatch.setattr(config, "STORE_EMAIL_ADDRESS", "")
    monkeypatch.setattr(config, "STORE_EMAIL_PASSWORD", "")

    class FakeStore:
        code = "LYO"
        name = "Pharmacie de Lyon"
        contact_name = "Jean Dupont"
        contact_email = "jdupont@hellopharmacie.com"
        verification_token = "abc123"

    assert store_requests.send_verification_email(FakeStore()) is False


def test_send_verification_email_never_falls_back_to_gmail_address(monkeypatch):
    """Même si GMAIL_ADDRESS est configuré (relevé Nifty), l'envoi ne doit
    jamais l'utiliser — voir CLAUDE.md."""
    monkeypatch.setattr(config, "GMAIL_ADDRESS", "olivier.toucas.pro@gmail.com")
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "fake-app-password")
    monkeypatch.setattr(config, "STORE_EMAIL_SMTP_HOST", "")
    monkeypatch.setattr(config, "STORE_EMAIL_ADDRESS", "")
    monkeypatch.setattr(config, "STORE_EMAIL_PASSWORD", "")

    class FakeStore:
        code = "LYO"
        name = "Pharmacie de Lyon"
        contact_name = "Jean Dupont"
        contact_email = "jdupont@hellopharmacie.com"
        verification_token = "abc123"

    assert store_requests.send_verification_email(FakeStore()) is False


def test_send_verification_email_uses_store_email_address_when_configured(monkeypatch):
    monkeypatch.setattr(config, "STORE_EMAIL_SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(config, "STORE_EMAIL_SMTP_PORT", 465)
    monkeypatch.setattr(config, "STORE_EMAIL_ADDRESS", "olivier.toucas@hellopharmacie.com")
    monkeypatch.setattr(config, "STORE_EMAIL_PASSWORD", "fake-password")
    monkeypatch.setattr(config, "PUBLIC_BASE_URL", "https://atm.hellopharmacie.com/nifty")

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

    monkeypatch.setattr(store_requests.smtplib, "SMTP_SSL", FakeSMTP)

    class FakeStore:
        code = "LYO"
        name = "Pharmacie de Lyon"
        contact_name = "Jean Dupont"
        contact_email = "jdupont@hellopharmacie.com"
        verification_token = "abc123"

    assert store_requests.send_verification_email(FakeStore()) is True
    assert calls["host"] == "smtp.example.com"
    assert calls["login"] == ("olivier.toucas@hellopharmacie.com", "fake-password")
    from_addr, to_addrs, message = calls["sendmail"]
    assert from_addr == "olivier.toucas@hellopharmacie.com"
    assert to_addrs == ["jdupont@hellopharmacie.com"]
    parsed = email.message_from_string(message)
    body = parsed.get_payload(decode=True).decode("utf-8")
    assert "abc123" in body


def test_send_duplicate_code_alert_returns_false_without_recipient_configured(monkeypatch):
    monkeypatch.setattr(config, "STORE_ALERT_RECIPIENT", "")

    class FakeStore:
        name = "Pharmacie de Lyon"
        contact_email = "jdupont@hellopharmacie.com"
        email_verified_at = None

    assert store_requests.send_duplicate_code_alert("LYO", FakeStore(), "autre@hellopharmacie.com") is False
