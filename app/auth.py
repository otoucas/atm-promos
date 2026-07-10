import hashlib
import hmac
import os

from fastapi import Request

from . import config


def is_admin(request: Request) -> bool:
    if config.ADMIN_AUTH_DISABLED:
        return True
    return bool(request.session.get("is_admin"))


def check_password(password: str) -> bool:
    return hmac.compare_digest(password, config.ADMIN_PASSWORD)


# --- Mot de passe par point de vente (magasins "standalone", voir Store.password_hash) ---
# PBKDF2 via hashlib (bibliothèque standard) plutôt qu'une dépendance externe
# (bcrypt/passlib) — ce projet reste volontairement léger en dépendances.
_PBKDF2_ITERATIONS = 200_000


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt.hex()}${derived.hex()}"


def verify_store_password(password: str, stored_hash: str | None) -> bool:
    if not stored_hash:
        return False
    try:
        algo, iterations, salt_hex, hash_hex = stored_hash.split("$")
        derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), int(iterations))
        return hmac.compare_digest(derived.hex(), hash_hex)
    except (ValueError, AttributeError):
        return False
