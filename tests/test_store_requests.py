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


def test_send_verification_email_returns_false_without_raising_when_gmail_not_configured(monkeypatch):
    monkeypatch.setattr(config, "GMAIL_ADDRESS", "")
    monkeypatch.setattr(config, "GMAIL_APP_PASSWORD", "")

    class FakeStore:
        code = "LYO"
        name = "Pharmacie de Lyon"
        contact_name = "Jean Dupont"
        contact_email = "jdupont@hellopharmacie.com"
        verification_token = "abc123"

    assert store_requests.send_verification_email(FakeStore()) is False


def test_send_duplicate_code_alert_returns_false_without_recipient_configured(monkeypatch):
    monkeypatch.setattr(config, "STORE_ALERT_RECIPIENT", "")

    class FakeStore:
        name = "Pharmacie de Lyon"
        contact_email = "jdupont@hellopharmacie.com"
        email_verified_at = None

    assert store_requests.send_duplicate_code_alert("LYO", FakeStore(), "autre@hellopharmacie.com") is False
