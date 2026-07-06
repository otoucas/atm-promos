"""Business rules that only need an in-memory list of promotions, not a
fresh DB query — kept separate from models.py to keep that file about
schema, this one about "what do we do with the data"."""

from typing import Iterable, Set

from .models import Promotion


def _dates_overlap(a: Promotion, b: Promotion) -> bool:
    if not (a.valid_from and a.valid_until and b.valid_from and b.valid_until):
        return False
    return a.valid_from <= b.valid_until and b.valid_from <= a.valid_until


def _products_overlap(a: Promotion, b: Promotion):
    """None if either side has no product_codes yet (unknown), else whether
    they share at least one Winpharma product code."""
    codes_a, codes_b = set(a.product_codes_list), set(b.product_codes_list)
    if not codes_a or not codes_b:
        return None
    return bool(codes_a & codes_b)


def _same_operation(a: Promotion, b: Promotion) -> bool:
    """Whether two promotions cover the same real-world operation, for
    conflict-flagging purposes. A lab/brand often runs several distinct
    product ranges at once (e.g. elmex® sensitive vs. elmex® junior) with
    unrelated promotions — so once product_codes are known for both sides,
    that's the source of truth, not the shared brand name. Brand name is
    only a fallback while product_codes haven't been filled in yet (e.g.
    still pending admin review)."""
    same_reference = bool(a.highco_reference) and a.highco_reference == b.highco_reference
    if same_reference:
        return True
    overlap = _products_overlap(a, b)
    if overlap is not None:
        return overlap
    return bool(a.brand_name) and a.brand_name.strip().lower() == (b.brand_name or "").strip().lower()


def find_conflicting_ids(promotions: Iterable[Promotion]) -> Set[int]:
    """IDs of promotions whose validity period overlaps another promotion of
    the same product range (or the same underlying HighCo link) — e.g. a
    renewed or updated campaign received twice. Flagged, not auto-removed:
    an operator may still need either one, so this only drives a visual
    warning."""
    promos = list(promotions)
    conflicts: Set[int] = set()
    for i in range(len(promos)):
        for j in range(i + 1, len(promos)):
            a, b = promos[i], promos[j]
            if _same_operation(a, b) and _dates_overlap(a, b):
                conflicts.add(a.id)
                conflicts.add(b.id)
    return conflicts
