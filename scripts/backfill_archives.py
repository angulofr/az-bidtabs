"""Full historical archive backfill runner for ADOT, Maricopa County / MCDOT / FCD, and AZ municipalities."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.contractor_profiles import backfill_contractor_profiles
from src.db import db_session, init_db
from src.scrapers.adot import run_adot_archive_sync
from src.scrapers.adot_archive import (
    sync_adot_as_read_history,
    sync_adot_stb_awards,
    sync_adot_wayback_pdfs,
)
from src.scrapers.bonfire import run_all_bonfire_syncs
from src.scrapers.maricopa_bidsync import run_maricopa_bidsync_sync
from src.scrapers.maricopa_onbase import run_maricopa_onbase_sync
from src.scrapers.mesa_chandler import run_chandler_sync, run_mesa_sync
from src.scrapers.phoenix_opengov import run_phoenix_opengov_sync


def _db_totals(db_path: str | None) -> dict[str, int]:
    with db_session(db_path) as conn:
        projects = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        contractors = conn.execute("SELECT COUNT(*) FROM contractors").fetchone()[0]
        bid_lines = conn.execute("SELECT COUNT(*) FROM bid_lines").fetchone()[0]
        project_bids = conn.execute("SELECT COUNT(*) FROM project_bids").fetchone()[0]
    return {
        "projects": int(projects),
        "contractors": int(contractors),
        "bid_lines": int(bid_lines),
        "project_bids": int(project_bids),
    }


def _run_step(name: str, fn: Any) -> Any:
    print(f"[backfill] Starting {name}...", flush=True)
    try:
        res = fn()
        print(f"[backfill] Finished {name}: {json.dumps(res, default=str)}", flush=True)
        return res
    except Exception as exc:
        err = {"error": str(exc)}
        print(f"[backfill] Error in {name}: {exc}", flush=True)
        return err


def main() -> int:
    parser = argparse.ArgumentParser(description="AZ BidTabs historical archive backfill")
    parser.add_argument("--all", action="store_true", help="Run all historical backfill sources")
    parser.add_argument("--adot-live", action="store_true", help="Run direct CNSADS tabulations sync")
    parser.add_argument("--adot-wayback", action="store_true", help="Run Wayback -> live S3 ADOT PDF recovery")
    parser.add_argument("--adot-stb", action="store_true", help="Run ADOT Transportation Board Awards PDFs (2010-2026)")
    parser.add_argument("--adot-as-read", action="store_true", help="Run ADOT CNSADS As-Read history (2001-2026)")
    parser.add_argument("--mcdot-bidsync", action="store_true", help="Run full unfiltered Maricopa BidSync walk")
    parser.add_argument("--mcdot-onbase", action="store_true", help="Run Maricopa OnBase civil contracts sync")
    parser.add_argument("--municipal", action="store_true", help="Run Phoenix/Bonfire/Mesa/Chandler backfill")
    parser.add_argument("--limit", type=int, default=500, help="Base per-source limit")
    parser.add_argument("--dry-run", action="store_true", help="Discover/parse without DB writes")
    parser.add_argument("--db", type=str, default=None, help="Optional DB path or Postgres URI")
    args = parser.parse_args()

    run_any = any(
        [
            args.all,
            args.adot_live,
            args.adot_wayback,
            args.adot_stb,
            args.adot_as_read,
            args.mcdot_bidsync,
            args.mcdot_onbase,
            args.municipal,
        ]
    )
    if not run_any:
        args.all = True

    init_db(args.db)
    report: dict[str, Any] = {"before": _db_totals(args.db), "steps": {}}

    if args.all or args.adot_live:
        report["steps"]["adot_live"] = _run_step(
            "adot_live",
            lambda: run_adot_archive_sync(args.db, limit=args.limit, dry_run=args.dry_run),
        )

    if args.all or args.adot_wayback:
        report["steps"]["adot_wayback_s3"] = _run_step(
            "adot_wayback_s3",
            lambda: sync_adot_wayback_pdfs(
                args.db, limit=args.limit, max_snapshots=80, dry_run=args.dry_run
            ),
        )

    if args.all or args.adot_stb:
        report["steps"]["adot_stb_awards"] = _run_step(
            "adot_stb_awards",
            lambda: sync_adot_stb_awards(
                args.db, max_pages=18, limit=args.limit, dry_run=args.dry_run
            ),
        )

    if args.all or args.adot_as_read:
        as_read_limit = max(args.limit * 6, 3000)
        report["steps"]["adot_as_read_history"] = _run_step(
            "adot_as_read_history",
            lambda: sync_adot_as_read_history(
                args.db,
                start_year=2001,
                end_year=2026,
                limit=as_read_limit,
                dry_run=args.dry_run,
            ),
        )

    if args.all or args.mcdot_bidsync:
        bidsync_limit = max(args.limit * 2, 1200)
        report["steps"]["mcdot_bidsync_full"] = _run_step(
            "mcdot_bidsync_full",
            lambda: run_maricopa_bidsync_sync(
                args.db,
                limit=bidsync_limit,
                query="",
                max_ajax_pages=100,
                dry_run=args.dry_run,
            ),
        )

    if args.all or args.mcdot_onbase:
        onbase_limit = max(args.limit, 600)
        report["steps"]["maricopa_onbase"] = _run_step(
            "maricopa_onbase",
            lambda: run_maricopa_onbase_sync(
                args.db,
                limit=onbase_limit,
                dry_run=args.dry_run,
            ),
        )

    if args.all or args.municipal:
        muni_limit = max(args.limit, 250)
        report["steps"]["phoenix_opengov"] = _run_step(
            "phoenix_opengov",
            lambda: run_phoenix_opengov_sync(args.db, limit=muni_limit, dry_run=args.dry_run),
        )
        report["steps"]["bonfire"] = _run_step(
            "bonfire",
            lambda: run_all_bonfire_syncs(args.db, limit=muni_limit, dry_run=args.dry_run),
        )
        report["steps"]["mesa"] = _run_step(
            "mesa",
            lambda: run_mesa_sync(args.db, limit=muni_limit, dry_run=args.dry_run),
        )
        report["steps"]["chandler"] = _run_step(
            "chandler",
            lambda: run_chandler_sync(args.db, limit=muni_limit, dry_run=args.dry_run),
        )

    if not args.dry_run:
        report["steps"]["contractors_enriched"] = _run_step(
            "contractors_enriched",
            lambda: backfill_contractor_profiles(args.db),
        )

    report["after"] = _db_totals(args.db)
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
