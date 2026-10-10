"""Gmail search query builder (ING-3): only money-looking mail, no promotions/social."""

from collections.abc import Iterable
from datetime import datetime
from typing import Final

from app.regions.base import RegionPack

_EXCLUDED_CATEGORIES: Final = ("promotions", "social")


def build_query(
    packs: Iterable[RegionPack],
    *,
    after: datetime | None = None,
    before: datetime | None = None,
) -> str:
    """Build the Gmail `q` string from the packs' search terms and an optional time window.

    after/before must be timezone-aware; they become epoch seconds. Gmail applies them
    with up to ~1 minute of slack, so callers that need exact windows (backfill) must
    pad the window and rely on idempotent inserts.
    """
    terms = _unique_terms(packs)
    parts = ["(" + " OR ".join(f'"{term}"' for term in terms) + ")"]
    parts += [f"-category:{category}" for category in _EXCLUDED_CATEGORIES]
    if after is not None:
        parts.append(f"after:{_epoch(after, 'after')}")
    if before is not None:
        parts.append(f"before:{_epoch(before, 'before')}")
    if after is not None and before is not None and after >= before:
        raise ValueError(f"empty window: after={after.isoformat()} >= before={before.isoformat()}")
    return " ".join(parts)


def _unique_terms(packs: Iterable[RegionPack]) -> list[str]:
    """All packs' terms, validated, case-insensitively de-duplicated, first spelling kept."""
    seen: set[str] = set()
    terms: list[str] = []
    for pack in packs:
        for term in pack.search_terms:
            if not any(ch.isalnum() for ch in term):
                # Gmail drops pure symbols ("₹", "$"): the term would match everything.
                raise ValueError(f"search term {term!r} in pack {pack.code!r} has no letters")
            if '"' in term:
                raise ValueError(f"search term {term!r} in pack {pack.code!r} contains a quote")
            if term.casefold() not in seen:
                seen.add(term.casefold())
                terms.append(term)
    if not terms:
        # An empty filter would turn every backfill into a scan of the whole mailbox.
        raise ValueError("no search terms: at least one region pack with terms is required")
    return terms


def _epoch(moment: datetime, name: str) -> int:
    if moment.tzinfo is None or moment.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware (got naive {moment.isoformat()})")
    return int(moment.timestamp())
