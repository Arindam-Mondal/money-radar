"""Region pack registry: REGION_PACKS names -> RegionPack objects."""

from collections.abc import Iterable

from app.regions.base import RegionPack
from app.regions.india import INDIA

_REGISTRY: dict[str, RegionPack] = {pack.code: pack for pack in (INDIA,)}


def load_region_packs(codes: Iterable[str]) -> list[RegionPack]:
    """Look up packs by code, in the given order. Unknown codes fail loudly."""
    unknown = [code for code in codes if code not in _REGISTRY]
    if unknown:
        raise ValueError(f"unknown region pack(s) {unknown}; known: {sorted(_REGISTRY)}")
    return [_REGISTRY[code] for code in codes]


__all__ = ["RegionPack", "load_region_packs"]
