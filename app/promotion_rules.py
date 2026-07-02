"""Business rules that only need an in-memory list of promotions, not a
fresh DB query — kept separate from models.py to keep that file about
schema, this one about "what do we do with the data"."""

from typing import Iterable, Set

from .models import Promotion


def _dates_overlap(a: Promotion, b: Promotion) -> bool:
    if not (a.valid_from and a.valid_until and b.valid_from and b.valid_until):
        return False
    return a.valid_from <= b.valid_until and b.valid_from <= a.valid_until


def _same_operation(a: Promotion, b: Promotion) -> bool:
    same_brand = bool(a.brand_name) and a.brand_name.strip().lower() == (b.brand_name or "").strip().lower()
    same_reference = bool(a.highco_reference) and a.highco_reference == b.highco_reference
    return same_brand or same_reference


def find_conflicting_ids(promotions: Iterable[Promotion]) -> Set[int]:
    """IDs of promotions whose validity period overlaps another promotion of
    the same brand (or the same underlying HighCo link) — e.g. a renewed or
    updated campaign received twice. Flagged, not auto-removed: an operator
    may still need either one, so this only drives a visual warning."""
    promos = list(promotions)
    conflicts: Set[int] = set()
    for i in range(len(promos)):
        for j in range(i + 1, len(promos)):
            a, b = promos[i], promos[j]
            if _same_operation(a, b) and _dates_overlap(a, b):
                conflicts.add(a.id)
                conflicts.add(b.id)
    return conflicts
