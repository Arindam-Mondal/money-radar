"""Backfill CLI arguments: inclusive --from/--to in, half-open DateRange out."""

from datetime import date

import pytest

from app.pipeline.gmail.backfill import parse_args
from app.pipeline.gmail.ranges import DateRange


def test_to_is_inclusive() -> None:
    # "September" as a person says it -> [Sep 1, Oct 1) as the code stores it.
    assert parse_args(["--from", "2026-09-01", "--to", "2026-09-30"]) == DateRange(
        date(2026, 9, 1), date(2026, 10, 1)
    )


def test_single_day() -> None:
    assert parse_args(["--from", "2026-09-05", "--to", "2026-09-05"]).days == 1


def test_to_before_from_is_an_error(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit):
        parse_args(["--from", "2026-09-05", "--to", "2026-09-01"])
    assert "is before --from" in capsys.readouterr().err


def test_bad_date_is_an_error() -> None:
    with pytest.raises(SystemExit):
        parse_args(["--from", "2026-13-01", "--to", "2026-09-01"])
