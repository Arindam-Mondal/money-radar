"""Preview what the ING-3 search filter matches in YOUR mailbox. Local terminal only.

Read-only (gmail.readonly). Prints dates, senders and subjects to your terminal; nothing
is stored or sent anywhere else. Don't paste the output into chats or issues.

Run from backend/:
    uv run python -m scripts.gmail_preview                  # last 7 days, 20 messages
    uv run python -m scripts.gmail_preview --days 30 --limit 50
    uv run python -m scripts.gmail_preview --per-term       # how many hits each term adds
    uv run python -m scripts.gmail_preview --terms "Rs,INR,UPI"   # try other terms
"""

import argparse
from datetime import UTC, datetime, timedelta
from itertools import islice
from pathlib import Path

from app.pipeline.gmail.client import GmailMailSource
from app.pipeline.gmail.query import build_query
from app.regions import RegionPack, load_region_packs

_TOKEN = Path(__file__).resolve().parents[2] / "secrets" / "gmail_token.json"
_COUNT_CAP = 5000  # stop counting here; enough to compare terms


def _count(source: GmailMailSource, query: str) -> int:
    return sum(1 for _ in islice(source.iter_message_ids(query), _COUNT_CAP))


def main() -> None:
    parser = argparse.ArgumentParser(description="Preview the Gmail money filter")
    parser.add_argument("--days", type=int, default=7, help="look back this many days")
    parser.add_argument("--limit", type=int, default=20, help="messages to list")
    parser.add_argument("--terms", help="comma list to try instead of the region packs")
    parser.add_argument("--per-term", action="store_true", help="count hits per term")
    args = parser.parse_args()

    if args.terms:
        terms = tuple(t.strip() for t in args.terms.split(",") if t.strip())
        packs = [RegionPack(code="cli", currencies=(), search_terms=terms)]
    else:
        packs = load_region_packs(["india"])

    now = datetime.now(UTC)
    window = {"after": now - timedelta(days=args.days), "before": now}
    query = build_query(packs, **window)
    source = GmailMailSource.from_token_file(_TOKEN)

    print(f"\nQuery: {query}\n")
    everything = _count(source, f"after:{int(window['after'].timestamp())}")
    matched = _count(source, query)
    print(f"Last {args.days} days: {matched} matched out of {everything} messages")
    print(f"(counts stop at {_COUNT_CAP})\n")

    if args.per_term:
        print("Hits per term (same window, promotions/social excluded):")
        for pack in packs:
            for term in pack.search_terms:
                one = RegionPack(code=pack.code, currencies=(), search_terms=(term,))
                print(f"  {term!r:<14} {_count(source, build_query([one], **window))}")
        print()

    print(f"Newest {args.limit} matches:")
    print(f"  {'received (local time)':<22} {'from':<40} subject")
    for message_id in islice(source.iter_message_ids(query), args.limit):
        msg = source.get_message(message_id)
        received = msg.received_at.astimezone().strftime("%Y-%m-%d %H:%M")
        sender = (msg.header("From") or "?")[:40]
        subject = (msg.header("Subject") or "(no subject)")[:70]
        print(f"  {received:<22} {sender:<40} {subject}")


if __name__ == "__main__":
    main()
