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

# --- Pont ERPNext (module atm_nifty) : pousse les promotions vers ERPNext ---
ERPNEXT_URL = os.environ.get("ERPNEXT_URL", "")
ERPNEXT_API_KEY = os.environ.get("ERPNEXT_API_KEY", "")
ERPNEXT_API_SECRET = os.environ.get("ERPNEXT_API_SECRET", "")
# Actif seulement si l'URL et les identifiants sont renseignés.
ERPNEXT_SYNC_ENABLED = bool(ERPNEXT_URL and ERPNEXT_API_KEY and ERPNEXT_API_SECRET)
ERPNEXT_SYNC_INTERVAL_MINUTES = int(os.environ.get("ERPNEXT_SYNC_INTERVAL_MINUTES", "10"))
# Fréquence de relecture de l'état autoritatif depuis ERPNext (minutes).
ERPNEXT_PULL_INTERVAL_MINUTES = int(os.environ.get("ERPNEXT_PULL_INTERVAL_MINUTES", "5"))

# --- Multi-magasins ---
# Code du point de vente historique (Pharmacie Artemare), créé automatiquement
# au démarrage s'il n'existe pas encore. C'est le seul avec integration=erpnext.
DEFAULT_STORE_CODE = os.environ.get("DEFAULT_STORE_CODE", "ART")
DEFAULT_STORE_NAME = os.environ.get("DEFAULT_STORE_NAME", "Pharmacie Artemare")

# Anti-abus sur la génération de code : les pages des points de vente en
# dépannage sont accessibles publiquement depuis Internet, et chaque
# génération consomme un vrai code HighCo à usage unique. On plafonne le
# nombre de générations par promotion sur une fenêtre glissante.
CODE_GENERATION_RATE_LIMIT_COUNT = int(os.environ.get("CODE_GENERATION_RATE_LIMIT_COUNT", "5"))
CODE_GENERATION_RATE_LIMIT_WINDOW_MINUTES = int(os.environ.get("CODE_GENERATION_RATE_LIMIT_WINDOW_MINUTES", "15"))

# --- Compte de connexion par point de vente (magasins "standalone") ---
# Durée de la session si "se souvenir de moi" n'est PAS coché (minutes) —
# au-delà, la session applicative expire même si le cookie du navigateur est
# encore valide. Si coché, pas d'expiration applicative (le cookie de session
# lui-même dure SESSION_COOKIE_MAX_AGE_DAYS).
STORE_SESSION_DEFAULT_MINUTES = int(os.environ.get("STORE_SESSION_DEFAULT_MINUTES", str(8 * 60)))
SESSION_COOKIE_MAX_AGE_DAYS = int(os.environ.get("SESSION_COOKIE_MAX_AGE_DAYS", "90"))
# Durée de validité d'un lien "mot de passe oublié" (minutes).
PASSWORD_RESET_TOKEN_VALIDITY_MINUTES = int(os.environ.get("PASSWORD_RESET_TOKEN_VALIDITY_MINUTES", "60"))

# --- Envoi des emails de demande d'ouverture (confirmation, alerte, mot de
# passe oublié) — VOLONTAIREMENT séparé de GMAIL_ADDRESS ci-dessus : ces
# emails doivent apparaître comme venant du groupement pharmacie
# (@hellopharmacie.com), jamais de la boîte Gmail personnelle utilisée pour
# le relevé des promos Nifty. Tant que ces identifiants ne sont pas fournis,
# l'envoi échoue proprement (best-effort, jamais de repli silencieux sur
# GMAIL_ADDRESS).
STORE_EMAIL_SMTP_HOST = os.environ.get("STORE_EMAIL_SMTP_HOST", "")
STORE_EMAIL_SMTP_PORT = int(os.environ.get("STORE_EMAIL_SMTP_PORT", "465"))
STORE_EMAIL_ADDRESS = os.environ.get("STORE_EMAIL_ADDRESS", "")
STORE_EMAIL_PASSWORD = os.environ.get("STORE_EMAIL_PASSWORD", "")

# --- Demande d'ouverture d'un point de vente (formulaire /superadmin/stores/new) ---
# Domaine imposé pour l'email de contact — seule la partie locale (avant @)
# est saisie dans le formulaire, ce suffixe est ajouté automatiquement.
STORE_CONTACT_EMAIL_DOMAIN = os.environ.get("STORE_CONTACT_EMAIL_DOMAIN", "hellopharmacie.com")
# Adresse qui reçoit les alertes (ex: un sigle déjà pris/en attente est redemandé).
STORE_ALERT_RECIPIENT = os.environ.get("STORE_ALERT_RECIPIENT", "")
# Base publique utilisée pour composer le lien de confirmation envoyé par email
# (le service ne connaît pas son propre nom public, il est derrière un reverse proxy).
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
