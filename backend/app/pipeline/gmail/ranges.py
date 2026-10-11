"""Date-range arithmetic for backfill (ING-2): which days of a request are not ingested yet.

Ranges are half-open, [start, end): `end` is the first day NOT included. Adjacent ranges
then share an endpoint ([1, 5) + [5, 8) = [1, 8)) and a range's length is end - start,
with no +1/-1 anywhere. Pure functions: no database, no Gmail.
"""

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date


@dataclass(frozen=True, slots=True, order=True)
class DateRange:
    start: date
    end: date  # exclusive

    def __post_init__(self) -> None:
        if self.start >= self.end:
            raise ValueError(f"empty range: start {self.start} must be before end {self.end}")

    @property
    def days(self) -> int:
        return (self.end - self.start).days

    def __contains__(self, day: date) -> bool:
        return self.start <= day < self.end


def merge(ranges: Iterable[DateRange]) -> list[DateRange]:
    """The same days as sorted, non-overlapping, non-touching ranges."""
    merged: list[DateRange] = []
    for current in sorted(ranges):  # order=True: by start, then end
        if merged and current.start <= merged[-1].end:  # overlaps or touches the last one
            last = merged[-1]
            merged[-1] = DateRange(last.start, max(last.end, current.end))
        else:
            merged.append(current)
    return merged


def compute_gaps(requested: DateRange, covered: Iterable[DateRange]) -> list[DateRange]:
    """The parts of `requested` not in any covered range, sorted, as few ranges as possible.

    One sweep over the merged coverage with a cursor: everything before the cursor is
    either covered or already emitted as a gap. O(n log n) for the sort, O(n) after.
    """
    gaps: list[DateRange] = []
    cursor = requested.start
    for block in merge(covered):
        if block.start >= requested.end:
            break  # sorted: this and every later block are past the request
        if block.end <= cursor:
            continue  # entirely before the part we still have to explain
        if block.start > cursor:
            gaps.append(DateRange(cursor, block.start))
        cursor = block.end
    if cursor < requested.end:
        gaps.append(DateRange(cursor, requested.end))
    return gaps
