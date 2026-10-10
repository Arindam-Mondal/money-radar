"""RegionPack: everything region-specific, so the pipeline itself stays region-neutral."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class RegionPack:
    code: str  # registry key used in REGION_PACKS, e.g. "india"
    currencies: tuple[str, ...]  # ISO 4217 codes, e.g. ("INR",)
    search_terms: tuple[str, ...]  # Gmail search words that indicate a money message
    # P2 adds recognizers and channels; P4 adds bank parsers.
