"""Best-effort automatic brand logo lookup.

Pharmacy/parapharmacy brands rarely have a predictable .com domain, so this is
inherently approximate. The admin validation screen lets the pharmacist upload
a replacement logo whenever the auto-fetched one is wrong or missing — treat
this as a convenience, not a guarantee.
"""

import re

import httpx

_LOGO_API = "https://logo.clearbit.com/{domain}"


def _slugify_domain(brand_name: str) -> str:
    slug = re.sub(r"[^a-z0-9]", "", brand_name.lower())
    return f"{slug}.com"


def fetch_logo_url(brand_name: str) -> str | None:
    """Return a hotlinkable logo URL for the brand, or None if nothing was found."""
    if not brand_name.strip():
        return None

    domain = _slugify_domain(brand_name)
    url = _LOGO_API.format(domain=domain)
    try:
        response = httpx.head(url, timeout=5.0, follow_redirects=True)
        if response.status_code == 200:
            return url
    except httpx.HTTPError:
        pass
    return None
