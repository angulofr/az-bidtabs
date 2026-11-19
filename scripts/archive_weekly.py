"""Weekly automated archival script for ADOT Bid Tabulations and Phoenix OpenGov.

Designed to run via GitHub Actions (before ADOT's ~4-month bid tab expiry) or locally.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.contractor_profiles import backfill_contractor_profiles
from src.db import deduplicate_contractors, init_db
from src.scrapers.adot import run_adot_archive_sync
from src.scrapers.adot_archive import sync_adot_as_read_history, sync_adot_stb_awards
from src.scrapers.bidnet_az import run_all_bidnet_syncs
from src.scrapers.bonfire import run_all_bonfire_syncs
from src.scrapers.maricopa_bidsync import run_maricopa_bidsync_sync
from src.scrapers.maricopa_onbase import run_maricopa_onbase_sync
from src.scrapers.mesa_chandler import run_chandler_sync, run_mesa_sync
from src.scrapers.phoenix_opengov import run_phoenix_opengov_sync


def main() -> int:
    parser = argparse.ArgumentParser(description="AZ BidTabs weekly archive sync")
    parser.add_argument("--limit", type=int, default=30, help="Max items per source")
    parser.add_argument("--dry-run", action="store_true", help="Discover without writing to DB")
    parser.add_argument("--db", type=str, default=None, help="Optional DB path or Postgres URI (defaults to DATABASE_URL)")
    args = parser.parse_args()

    init_db(args.db)
    report: dict[str, object] = {
        "adot": None,
        "adot_stb_awards": None,
        "adot_as_read_recent": None,
        "phoenix_opengov": None,
        "bonfire": None,
        "bidnet_az": None,
        "mcdot_bidsync": None,
        "maricopa_onbase": None,
        "mesa": None,
        "chandler": None,
    }

    try:
        report["adot"] = run_adot_archive_sync(args.db, limit=args.limit, dry_run=args.dry_run)
    except Exception as exc:
        report["adot"] = {"error": str(exc)}

    try:
        report["adot_stb_awards"] = sync_adot_stb_awards(
            args.db, max_pages=2, limit=max(args.limit, 10), dry_run=args.dry_run
        )
    except Exception as exc:
        report["adot_stb_awards"] = {"error": str(exc)}

    try:
        report["adot_as_read_recent"] = sync_adot_as_read_history(
            args.db,
            start_year=2024,
            end_year=2026,
            limit=max(args.limit, 50),
            dry_run=args.dry_run,
        )
    except Exception as exc:
        report["adot_as_read_recent"] = {"error": str(exc)}

    try:
        report["phoenix_opengov"] = run_phoenix_opengov_sync(
            args.db, limit=args.limit, dry_run=args.dry_run
        )
    except Exception as exc:
        report["phoenix_opengov"] = {"error": str(exc)}

    report["bonfire"] = run_all_bonfire_syncs(
        args.db, limit=args.limit, dry_run=args.dry_run
    )

    try:
        report["mcdot_bidsync"] = run_maricopa_bidsync_sync(
            args.db, limit=args.limit, dry_run=args.dry_run
        )
    except Exception as exc:
        report["mcdot_bidsync"] = {"error": str(exc)}

    try:
        report["maricopa_onbase"] = run_maricopa_onbase_sync(
            args.db,
            limit=max(args.limit, 50),
            include_expiration_pdf=True,
            fetch_contract_pdfs=True,
            pdf_limit=25,
            dry_run=args.dry_run,
        )
    except Exception as exc:
        report["maricopa_onbase"] = {"error": str(exc)}

    try:
        report["bidnet_az"] = run_all_bidnet_syncs(
            args.db, limit=max(args.limit, 50), dry_run=args.dry_run
        )
    except Exception as exc:
        report["bidnet_az"] = {"error": str(exc)}

    try:
        report["mesa"] = run_mesa_sync(args.db, limit=args.limit, dry_run=args.dry_run)
    except Exception as exc:
        report["mesa"] = {"error": str(exc)}

    try:
        report["chandler"] = run_chandler_sync(args.db, limit=args.limit, dry_run=args.dry_run)
    except Exception as exc:
        report["chandler"] = {"error": str(exc)}

    if not args.dry_run:
        try:
            report["contractors_deduped"] = deduplicate_contractors(args.db)
            if isinstance(report["contractors_deduped"], dict):
                report["contractors_deduped"] = {
                    "groups_merged": report["contractors_deduped"].get("groups_merged", 0),
                    "duplicates_removed": report["contractors_deduped"].get("duplicates_removed", 0),
                }
        except Exception as exc:
            report["contractors_deduped"] = {"error": str(exc)}
        try:
            report["contractors_enriched"] = backfill_contractor_profiles(args.db)
        except Exception as exc:
            report["contractors_enriched"] = {"error": str(exc)}

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
