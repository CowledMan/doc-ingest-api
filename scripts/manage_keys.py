"""Create or list document ingestion API keys."""

from __future__ import annotations

import argparse
import json
import sys

from doc_ingest_api.config import Settings
from doc_ingest_api.database import create_database
from doc_ingest_api.models import ApiKey
from doc_ingest_api.security import create_api_key, serialize_api_key


def build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("create", "list"))
    parser.add_argument("--database-url", help="Database URL; defaults to DATABASE_URL")
    parser.add_argument("--name", help="Human-readable key name (required with create)")
    parser.add_argument(
        "--monthly-page-quota",
        type=int,
        default=500,
        help="Monthly page quota for newly created keys (default: 500)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Run the key management command and return a process exit code."""
    args = build_parser().parse_args(argv)
    settings = Settings()
    database_url = args.database_url or settings.database_url
    if args.action == "create" and not args.name:
        print("--name is required with create", file=sys.stderr)
        return 2
    if args.monthly_page_quota < 0:
        print("--monthly-page-quota must be non-negative", file=sys.stderr)
        return 2

    database = create_database(database_url)
    database.create_all()
    if args.action == "create":
        raw_key = create_api_key(
            database.session_factory,
            name=args.name or "",
            monthly_page_quota=args.monthly_page_quota,
        )
        print(json.dumps({"api_key": raw_key}, ensure_ascii=False, indent=2))
        return 0

    with database.session_factory() as session:
        keys = session.query(ApiKey).order_by(ApiKey.id).all()
        print(json.dumps([serialize_api_key(key) for key in keys], ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
