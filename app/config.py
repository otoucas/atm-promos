import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
LOGO_DIR = DATA_DIR / "logos"
LOGO_DIR.mkdir(parents=True, exist_ok=True)

DATABASE_URL = os.environ.get("DATABASE_URL", f"sqlite:///{DATA_DIR / 'app.db'}")

SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-me")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "changeme")
# Bypasses the admin login entirely — for prototyping only. Never enable this
# on a deployment reachable beyond a fully trusted private network.
ADMIN_AUTH_DISABLED = os.environ.get("ADMIN_AUTH_DISABLED", "0") == "1"

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
GMAIL_IMAP_HOST = os.environ.get("GMAIL_IMAP_HOST", "imap.gmail.com")
GMAIL_MAILBOX = os.environ.get("GMAIL_MAILBOX", "INBOX")
GMAIL_SENDER_FILTER = os.environ.get("GMAIL_SENDER_FILTER", "")  # comma-separated domains/addresses, e.g. "highco-data.fr,gestion-promo.fr"
# Remove the \Inbox label (Gmail-specific) from an email once a promotion has
# been extracted from it, so the inbox doesn't fill up with processed mail.
GMAIL_ARCHIVE_AFTER_PROCESSING = os.environ.get("GMAIL_ARCHIVE_AFTER_PROCESSING", "1") == "1"

POLL_INTERVAL_MINUTES = int(os.environ.get("POLL_INTERVAL_MINUTES", "15"))
ARCHIVE_CHECK_INTERVAL_MINUTES = int(os.environ.get("ARCHIVE_CHECK_INTERVAL_MINUTES", "60"))
# Hour (0-23, server local time) for the once-a-day review: archive anything
# expired (including stale never-validated pending promotions) and log a
# summary of what's visible/upcoming — in addition to the hourly safety net above.
DAILY_REVIEW_HOUR = int(os.environ.get("DAILY_REVIEW_HOUR", "6"))

DISABLE_GMAIL_POLLER = os.environ.get("DISABLE_GMAIL_POLLER", "0") == "1"

# Rappel mensuel (e-mail) des promotions Nifty à préparer pour le mois
# suivant : validations en attente, campagnes qui démarrent/se terminent,
# conflits non résolus. Vide = désactivé. Envoyé via le même compte Gmail
# que le relevé (mot de passe d'application déjà configuré ci-dessus).
MONTHLY_PREVIEW_RECIPIENT = os.environ.get("MONTHLY_PREVIEW_RECIPIENT", "")
MONTHLY_PREVIEW_DAY = int(os.environ.get("MONTHLY_PREVIEW_DAY", "25"))
MONTHLY_PREVIEW_HOUR = int(os.environ.get("MONTHLY_PREVIEW_HOUR", "7"))
