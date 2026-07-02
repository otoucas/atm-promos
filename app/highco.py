"""Adapter that turns a promotion's HighCo reference (the URL/identifier decoded
from its QR code) into a fresh redemption code for the till.

⚠️ UNVERIFIED: the exact shape of HighCo's response (plain text, JSON, HTML page,
or a Wallet pass file) has not been confirmed against a real QR code yet. This
adapter makes a best-effort attempt at all three and raises HighCoResponseError
with the raw response attached when it can't confidently extract a code — check
that error's `.raw_excerpt` against a real scan before relying on this in
production, and adjust `_extract_code` accordingly.
"""

import re

import httpx

_CODE_PATTERN = re.compile(r"\b[A-Z0-9]{6,14}\b")
_JSON_CODE_KEYS = ("code", "coupon_code", "redemption_code", "voucher_code", "barcode")


class HighCoResponseError(RuntimeError):
    def __init__(self, message: str, raw_excerpt: str = ""):
        super().__init__(message)
        self.raw_excerpt = raw_excerpt


def generate_code(reference: str, timeout: float = 15.0) -> str:
    """Call the HighCo endpoint identified by `reference` and return a fresh code.

    Each call is expected to mint a new, single-use code (one call = one till
    transaction) — do not cache or reuse the result.
    """
    try:
        response = httpx.get(reference, timeout=timeout, follow_redirects=True)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HighCoResponseError(f"Requête HighCo échouée : {exc}") from exc

    return _extract_code(response)


def _extract_code(response: httpx.Response) -> str:
    content_type = response.headers.get("content-type", "")

    if "application/vnd.apple.pkpass" in content_type or "wallet" in content_type:
        raise HighCoResponseError(
            "La réponse HighCo est un pass Wallet — extraction non implémentée, "
            "à traiter une fois le format confirmé.",
            raw_excerpt=f"content-type={content_type}",
        )

    if "json" in content_type:
        try:
            data = response.json()
        except ValueError as exc:
            raise HighCoResponseError("Réponse JSON illisible", raw_excerpt=response.text[:500]) from exc
        for key in _JSON_CODE_KEYS:
            if key in data and data[key]:
                return str(data[key])
        raise HighCoResponseError(
            "Réponse JSON sans champ de code reconnu", raw_excerpt=str(data)[:500]
        )

    text = response.text.strip()

    if "text/html" in content_type or text.startswith("<"):
        match = _CODE_PATTERN.search(text)
        if match:
            return match.group(0)
        raise HighCoResponseError("Aucun code trouvé dans la page HTML", raw_excerpt=text[:500])

    if text:
        match = _CODE_PATTERN.fullmatch(text) or _CODE_PATTERN.search(text)
        if match:
            return match.group(0)
        return text  # fall back to whatever plain text was returned

    raise HighCoResponseError("Réponse HighCo vide ou de format non reconnu", raw_excerpt=repr(response.content[:200]))
