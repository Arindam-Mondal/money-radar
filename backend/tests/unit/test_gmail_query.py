"""build_query: terms, exclusions, time window, and the inputs it must refuse."""

from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.pipeline.gmail.query import build_query
from app.regions import RegionPack, load_region_packs

JAN_1 = datetime(2026, 1, 1, tzinfo=UTC)  # epoch 1767225600
JAN_2 = datetime(2026, 1, 2, tzinfo=UTC)  # epoch 1767312000


def _pack(*terms: str, code: str = "test") -> RegionPack:
    return RegionPack(code=code, currencies=("XXX",), search_terms=terms)


def test_india_pack_query_without_window() -> None:
    query = build_query(load_region_packs(["india"]))

    assert (
        query == '("Rs" OR "INR" OR "debited" OR "credited") -category:promotions -category:social'
    )


def test_window_becomes_epoch_seconds() -> None:
    query = build_query([_pack("Rs")], after=JAN_1, before=JAN_2)

    assert query.endswith("after:1767225600 before:1767312000")


def test_non_utc_timezones_are_converted_to_the_same_instant() -> None:
    ist = timezone(timedelta(hours=5, minutes=30))
    jan_1_in_ist = JAN_1.astimezone(ist)  # 05:30 IST == 00:00 UTC

    assert build_query([_pack("Rs")], after=jan_1_in_ist) == build_query([_pack("Rs")], after=JAN_1)


def test_terms_from_several_packs_are_deduplicated_case_insensitively() -> None:
    query = build_query([_pack("Rs", "debited", code="a"), _pack("DEBITED", "USD", code="b")])

    assert query.startswith('("Rs" OR "debited" OR "USD")')


@pytest.mark.parametrize("term", ["₹", "$", " ", ""])
def test_symbol_only_terms_are_rejected(term: str) -> None:
    with pytest.raises(ValueError, match="no letters"):
        build_query([_pack("Rs", term)])


def test_quote_in_term_is_rejected() -> None:
    with pytest.raises(ValueError, match="quote"):
        build_query([_pack('Rs" OR "x')])


def test_no_terms_is_rejected() -> None:
    with pytest.raises(ValueError, match="no search terms"):
        build_query([_pack()])


def test_naive_datetime_is_rejected() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        build_query([_pack("Rs")], after=datetime(2026, 1, 1))


def test_empty_window_is_rejected() -> None:
    with pytest.raises(ValueError, match="empty window"):
        build_query([_pack("Rs")], after=JAN_2, before=JAN_1)
