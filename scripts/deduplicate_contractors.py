"""One-off / CI script to deduplicate contractor names and backfill ROC/employee profiles."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.contractor_profiles import backfill_contractor_profiles
from src.db import db_session, deduplicate_contractors, init_db


def main() -> int:
    parser = argparse.ArgumentParser(description="Deduplicate AZ BidTabs contractors")
    parser.add_argument("--dry-run", action="store_true", help="Inspect merge groups without mutating DB")
    parser.add_argument("--db", type=str, default=None, help="Optional DB path or Postgres URI")
    args = parser.parse_args()

    init_db(args.db)
    with db_session(args.db) as conn:
        before_cnt = int(conn.execute("SELECT COUNT(*) FROM contractors").fetchone()[0])

    dedup_res = deduplicate_contractors(args.db, dry_run=args.dry_run)
    enriched = 0 if args.dry_run else backfill_contractor_profiles(args.db)

    with db_session(args.db) as conn:
        after_cnt = int(conn.execute("SELECT COUNT(*) FROM contractors").fetchone()[0])

    payload = {
        "dry_run": args.dry_run,
        "contractors_before": before_cnt,
        "contractors_after": after_cnt,
        "groups_merged": dedup_res["groups_merged"],
        "duplicates_removed": dedup_res["duplicates_removed"],
        "profiles_enriched": enriched,
        "merges": dedup_res["merges"],
    }
    print(json.dumps(payload, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
