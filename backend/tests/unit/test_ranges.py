"""DateRange / merge / compute_gaps: examples first, then properties checked by Hypothesis."""

from datetime import date, timedelta
from itertools import pairwise

import pytest
from hypothesis import given
from hypothesis import strategies as st

from app.pipeline.gmail.ranges import DateRange, compute_gaps, merge

JAN = DateRange(date(2026, 1, 1), date(2026, 2, 1))  # all of January (Feb 1 not included)


def _r(start_day: int, end_day: int) -> DateRange:
    """January days as a half-open range: _r(5, 10) is Jan 5 .. Jan 9."""
    return DateRange(date(2026, 1, start_day), date(2026, 1, end_day))


# --- DateRange -------------------------------------------------------------


def test_range_is_half_open() -> None:
    assert _r(5, 10).days == 5  # 5, 6, 7, 8, 9
    assert date(2026, 1, 10) not in _r(5, 10)
    assert date(2026, 1, 5) in _r(5, 10)


@pytest.mark.parametrize(("start", "end"), [(10, 10), (10, 5)])
def test_empty_or_backwards_range_is_rejected(start: int, end: int) -> None:
    with pytest.raises(ValueError, match="empty"):
        _r(start, end)


# --- merge ------------------------------------------------------------------


def test_merge_joins_overlapping_and_touching_ranges() -> None:
    # [1,5) and [5,8) touch: one range, no zero-length gap between them.
    assert merge([_r(5, 8), _r(1, 5), _r(3, 4), _r(20, 25)]) == [_r(1, 8), _r(20, 25)]


def test_merge_of_nothing_is_nothing() -> None:
    assert merge([]) == []


# --- compute_gaps: examples ----------------------------------------------------


def test_nothing_covered_means_one_gap_for_the_whole_request() -> None:
    assert compute_gaps(JAN, []) == [JAN]


def test_fully_covered_means_no_gaps() -> None:
    assert compute_gaps(_r(5, 10), [_r(1, 31)]) == []


def test_hole_in_the_middle() -> None:
    rest_of_jan = DateRange(date(2026, 1, 20), date(2026, 2, 1))
    assert compute_gaps(JAN, [_r(1, 10), rest_of_jan]) == [_r(10, 20)]


def test_covered_ranges_outside_the_request_are_ignored() -> None:
    december = DateRange(date(2025, 12, 1), date(2026, 1, 1))  # ends exactly where JAN starts
    assert compute_gaps(_r(1, 10), [december, _r(15, 20)]) == [_r(1, 10)]


def test_partial_overlap_at_both_edges() -> None:
    assert compute_gaps(_r(5, 25), [_r(1, 8), _r(22, 30)]) == [_r(8, 22)]


def test_several_gaps_in_order() -> None:
    covered = [_r(3, 5), _r(10, 12), _r(10, 11)]  # unsorted input, with a duplicate
    assert compute_gaps(_r(1, 15), covered) == [_r(1, 3), _r(5, 10), _r(12, 15)]


# --- compute_gaps: properties ---------------------------------------------------
# A brute-force model: a range is just a set of days. The properties say that
# compute_gaps agrees with set arithmetic for ANY inputs Hypothesis can think of.

_EPOCH = date(2026, 1, 1)


@st.composite
def date_ranges(draw: st.DrawFn) -> DateRange:
    start = draw(st.integers(min_value=0, max_value=60))
    length = draw(st.integers(min_value=1, max_value=30))
    return DateRange(_EPOCH + timedelta(days=start), _EPOCH + timedelta(days=start + length))


def _days(ranges: list[DateRange]) -> set[date]:
    return {r.start + timedelta(days=i) for r in ranges for i in range(r.days)}


@given(requested=date_ranges(), covered=st.lists(date_ranges(), max_size=8))
def test_gaps_are_exactly_the_requested_days_not_covered(
    requested: DateRange, covered: list[DateRange]
) -> None:
    gaps = compute_gaps(requested, covered)

    assert _days(gaps) == _days([requested]) - _days(covered)


@given(requested=date_ranges(), covered=st.lists(date_ranges(), max_size=8))
def test_gaps_are_sorted_and_never_touch(requested: DateRange, covered: list[DateRange]) -> None:
    gaps = compute_gaps(requested, covered)

    # Touching gaps would mean two backfill jobs where one would do.
    assert all(a.end < b.start for a, b in pairwise(gaps))


@given(requested=date_ranges(), covered=st.lists(date_ranges(), max_size=8))
def test_once_the_gaps_are_filled_nothing_is_left(
    requested: DateRange, covered: list[DateRange]
) -> None:
    # The backfill loop's promise: process the gaps, mark them covered, and a re-run is a no-op.
    gaps = compute_gaps(requested, covered)

    assert compute_gaps(requested, covered + gaps) == []
