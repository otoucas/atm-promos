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

GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
GMAIL_IMAP_HOST = os.environ.get("GMAIL_IMAP_HOST", "imap.gmail.com")
GMAIL_MAILBOX = os.environ.get("GMAIL_MAILBOX", "INBOX")
GMAIL_SENDER_FILTER = os.environ.get("GMAIL_SENDER_FILTER", "")  # e.g. "highco.com" — optional narrowing filter

POLL_INTERVAL_MINUTES = int(os.environ.get("POLL_INTERVAL_MINUTES", "15"))
ARCHIVE_CHECK_INTERVAL_MINUTES = int(os.environ.get("ARCHIVE_CHECK_INTERVAL_MINUTES", "60"))

DISABLE_GMAIL_POLLER = os.environ.get("DISABLE_GMAIL_POLLER", "0") == "1"
