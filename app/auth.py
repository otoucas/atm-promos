import hmac

from fastapi import Request

from . import config


def is_admin(request: Request) -> bool:
    return bool(request.session.get("is_admin"))


def check_password(password: str) -> bool:
    return hmac.compare_digest(password, config.ADMIN_PASSWORD)
