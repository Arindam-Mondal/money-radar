"""Backfill (ING-2): ingest mail for a range of calendar days, skipping days already done.

    python -m app.pipeline.gmail.backfill --from 2026-09-01 --to 2026-09-30

Coverage lives in `sync_ranges`. Each run asks compute_gaps which requested days are not
covered, ingests each gap page by page, and records the gap as covered only after its last
page. Running the same backfill twice therefore fetches nothing the second time.
"""

import argparse
import logging
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from itertools import batched
from typing import Final
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import SyncRange
from app.db.session import get_sessionmaker
from app.pipeline.gmail.auth import GmailAuthError
from app.pipeline.gmail.client import GmailMailSource
from app.pipeline.gmail.ingest import IngestCounts, ingest_messages
from app.pipeline.gmail.query import build_query
from app.pipeline.gmail.ranges import DateRange, compute_gaps
from app.pipeline.gmail.source import MailSource
from app.regions import load_region_packs
from app.regions.base import RegionPack

logger = logging.getLogger("money_radar.pipeline")

# Commit every this many messages: progress survives a crash, transactions stay short.
BATCH_SIZE: Final = 100
# Gmail applies after:/before: with up to ~1 minute of drift (measured in 1.3). Search a
# little beyond each edge; mail from a neighbouring day is recorded once anyway.
EDGE_PAD: Final = timedelta(minutes=10)


@dataclass(frozen=True, slots=True)
class BackfillResult:
    requested: DateRange | None  # after clamping to complete days; None if nothing was left
    gaps: list[DateRange]  # the uncovered parts that this run ingested
    listed: int  # ids the searches returned, including ones already recorded
    counts: IngestCounts


def backfill(
    session: Session,
    source: MailSource,
    packs: Iterable[RegionPack],
    requested: DateRange,
    *,
    zone: ZoneInfo,
    now: datetime,
) -> BackfillResult:
    """Ingest every uncovered day of `requested`. COMMITS: once per batch, once per gap.

    Only complete days are backfilled: the request is cut off at today (in `zone`), since
    marking a day covered while mail can still arrive for it would hide that mail from
    every later backfill. Today's mail is the poller's job.
    """
    packs = list(packs)
    today = now.astimezone(zone).date()
    if requested.start >= today:
        return BackfillResult(None, [], 0, IngestCounts())
    requested = DateRange(requested.start, min(requested.end, today))

    covered = [DateRange(r.start_date, r.end_date) for r in session.scalars(select(SyncRange))]
    gaps = compute_gaps(requested, covered)
    listed = 0
    counts = IngestCounts()
    for gap in gaps:
        query = build_query(
            packs,
            after=_midnight(gap.start, zone) - EDGE_PAD,
            before=_midnight(gap.end, zone) + EDGE_PAD,
        )
        for batch in batched(source.iter_message_ids(query), BATCH_SIZE):
            listed += len(batch)
            counts += ingest_messages(session, source, batch, now=now)
            session.commit()
            logger.info("backfill: %d listed, %d recorded so far", listed, counts.recorded)
        # Only now, after the last page: the whole gap is in `messages`.
        session.add(SyncRange(start_date=gap.start, end_date=gap.end))
        session.commit()
        logger.info("backfill: %s .. %s covered", gap.start, gap.end - timedelta(days=1))
    return BackfillResult(requested, gaps, listed, counts)


def _midnight(day: date, zone: ZoneInfo) -> datetime:
    """Start of `day` in `zone`, as an aware datetime (build_query turns it into epoch)."""
    return datetime.combine(day, time.min, tzinfo=zone)


# --- CLI ---------------------------------------------------------------------------


def parse_args(argv: Sequence[str] | None = None) -> DateRange:
    """--from/--to are inclusive calendar days, as people say them; returns the half-open range."""
    parser = argparse.ArgumentParser(
        prog="python -m app.pipeline.gmail.backfill",
        description="Ingest Gmail money mail for a range of days (covered days are skipped).",
    )
    parser.add_argument(
        "--from", dest="first", type=date.fromisoformat, required=True, metavar="YYYY-MM-DD"
    )
    parser.add_argument(
        "--to", dest="last", type=date.fromisoformat, required=True, metavar="YYYY-MM-DD"
    )
    args = parser.parse_args(argv)
    if args.last < args.first:
        parser.error(f"--to {args.last} is before --from {args.first}")
    return DateRange(args.first, args.last + timedelta(days=1))


def main(argv: Sequence[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s:     %(name)s - %(message)s")
    requested = parse_args(argv)
    settings = get_settings()
    try:
        source = GmailMailSource.from_token_file(settings.gmail_token_file)
    except GmailAuthError as e:
        logger.error("backfill not started: %s", e)
        raise SystemExit(1) from None
    packs = load_region_packs(settings.region_packs)
    with get_sessionmaker()() as session:
        result = backfill(
            session, source, packs, requested, zone=settings.zone, now=datetime.now(UTC)
        )
    if result.requested is None:
        logger.info("backfill: nothing to do, %s is not a complete day yet", requested.start)
        return
    # Counts only: never log subjects, senders or bodies.
    logger.info(
        "backfill done: days=%d gaps=%d listed=%d recorded=%d unverified=%d failed=%d",
        result.requested.days,
        len(result.gaps),
        result.listed,
        result.counts.recorded,
        result.counts.unverified,
        result.counts.failed,
    )


if __name__ == "__main__":
    main()
