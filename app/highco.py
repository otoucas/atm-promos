"""Adapter that turns a promotion's HighCo reference (the URL/identifier decoded
from its QR code) into a fresh redemption code for the till.

✅ VERIFIED (2026-07-02) against a real, live HighCo Nifty link (Fixodent
promotion). The flow is a two-step Apple Wallet (PassKit) distribution:

1. GET the reference URL with a mobile Safari User-Agent, following redirects.
   HighCo's platform (highcodata.walletpass.fr) lands on an HTML page with an
   "Add to Wallet" button. The button's `onclick="pkpassGenerate(...)"` call
   carries the parameters needed for step 2 (pass_template_id, url_id, token,
   token_mgs, csrf_token) — no JS execution needed, they're plain text in the
   HTML. A session cookie (PHPSESSID) is set on this request and must be
   carried into step 2, or the CSRF token is rejected.
2. POST that data as JSON to `{origin}/pass/apple/generate` (path taken from
   `<meta name="url">` on the landing page) with
   `Accept: application/vnd.apple.pkpass`, reusing the same cookies. A 200
   response body *is* the `.pkpass` file (a zip — see pkpass_utils.py) whose
   `barcodes[0].message` is the actual redemption code (e.g. "HCNxftz4UVR3r",
   barcode format Code128 — a linear barcode, not a QR, which matches a
   pharmacy till scanner).

Each call mints a new, real pass/code — do not retry blindly or call more
than once per till transaction.
"""

import re
from urllib.parse import urljoin

import httpx

from .pkpass_utils import extract_code_from_pkpass

# iOS Safari UA — HighCo's wallet-distribution platform only serves the
# "Add to Wallet" landing page (and thus the generate button/params) to
# clients that look wallet-capable.
_MOBILE_USER_AGENT = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
)

_GENERATE_CALL_PATTERN = re.compile(
    r"pkpassGenerate\(\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*,\s*'([^']*)'\s*\)"
)
_GENERATE_URL_META_PATTERN = re.compile(r'<meta\s+name="url"\s+content="([^"]+)"')


class HighCoResponseError(RuntimeError):
    def __init__(self, message: str, raw_excerpt: str = ""):
        super().__init__(message)
        self.raw_excerpt = raw_excerpt


def generate_code(reference: str, timeout: float = 15.0) -> str:
    """Call the HighCo endpoint identified by `reference` and return a fresh code.

    Each call mints a new, single-use code (one call = one till transaction) —
    do not cache or reuse the result, and don't call this speculatively.
    """
    with httpx.Client(
        timeout=timeout, follow_redirects=True, headers={"User-Agent": _MOBILE_USER_AGENT}
    ) as client:
        try:
            landing = client.get(reference)
            landing.raise_for_status()
        except httpx.HTTPError as exc:
            raise HighCoResponseError(f"Requête vers le lien HighCo échouée : {exc}") from exc

        params = _parse_generate_params(landing.text)
        generate_url = _parse_generate_url(landing.text, str(landing.url))

        try:
            pass_response = client.post(
                generate_url,
                json={
                    "pass_template_id": params[0],
                    "url_id": params[1],
                    "token": params[2],
                    "token_mgs": params[3],
                    "csrf_token": params[4],
                },
                headers={"Accept": "application/vnd.apple.pkpass", "Referer": str(landing.url)},
            )
        except httpx.HTTPError as exc:
            raise HighCoResponseError(f"Requête de génération du pass échouée : {exc}") from exc

    return _extract_code_from_generate_response(pass_response)


def _parse_generate_params(html: str) -> tuple:
    match = _GENERATE_CALL_PATTERN.search(html)
    if not match:
        raise HighCoResponseError(
            "Page HighCo inattendue : bouton 'Ajouter au Wallet' introuvable "
            "(structure de page différente de celle vérifiée le 2026-07-02 ?)",
            raw_excerpt=html[:500],
        )
    return match.groups()


def _parse_generate_url(html: str, landing_url: str) -> str:
    match = _GENERATE_URL_META_PATTERN.search(html)
    path = match.group(1) if match else "/pass/apple/generate"
    return urljoin(landing_url, path)


def _extract_code_from_generate_response(response: httpx.Response) -> str:
    content_type = response.headers.get("content-type", "")

    if response.status_code == 200 and "application/vnd.apple.pkpass" in content_type:
        code = extract_code_from_pkpass(response.content)
        if code:
            return code
        raise HighCoResponseError(
            "Pass Wallet reçu mais aucun code trouvé dans pass.json", raw_excerpt=f"{len(response.content)} bytes"
        )

    # Non-200 responses are JSON, sometimes with a {"redirect": "..."} the JS
    # follows client-side (e.g. "already generated" / expired states).
    try:
        data = response.json()
    except ValueError:
        data = None

    if data is not None:
        raise HighCoResponseError(
            f"HighCo a refusé la génération du pass (HTTP {response.status_code}) : {data}",
            raw_excerpt=str(data)[:500],
        )

    raise HighCoResponseError(
        f"Réponse inattendue lors de la génération du pass (HTTP {response.status_code}, {content_type})",
        raw_excerpt=response.text[:500],
    )
