"""India region pack (INR)."""

from app.regions.base import RegionPack

INDIA = RegionPack(
    code="india",
    currencies=("INR",),
    # No "₹": Gmail's search tokenizer drops the symbol, so the term is empty. Alone it
    # matches every message; inside an OR it adds nothing (measured on a real mailbox).
    search_terms=("Rs", "INR", "debited", "credited"),
)
