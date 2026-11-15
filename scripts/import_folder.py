"""CLI batch importer for folders of ADOT Bid Tab PDFs and Municipal/OpenGov CSV/XLSX exports."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db import init_db
from src.scrapers.adot import ingest_parsed_project, parse_adot_bid_tab_pdf
from src.scrapers.phoenix_opengov import parse_opengov_bid_tab_export

SUPPORTED_EXTS = {".pdf", ".csv", ".xlsx", ".xls"}


def import_folder(
    folder: Path | str,
    *,
    db_target: Path | str | None = None,
    source: str = "auto",
    agency: str = "City of Phoenix",
    county: str = "Maricopa",
) -> dict[str, object]:
    """Scan `folder` recursively and ingest all supported bid tabulation files."""
    root = Path(folder)
    if not root.exists() or not root.is_dir():
        raise FileNotFoundError(f"Folder not found: {root}")

    init_db(db_target)
    files = sorted(
        p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS
    )

    ingested = 0
    skipped = 0
    total_lines = 0
    errors: list[str] = []

    for fpath in files:
        ext = fpath.suffix.lower()
        try:
            use_adot = source == "adot" or (source == "auto" and ext == ".pdf")
            if use_adot:
                parsed = parse_adot_bid_tab_pdf(fpath)
                if parsed.agency_project_num == "ADOT-UNKNOWN":
                    parsed.agency_project_num = fpath.stem[:32]
            else:
                parsed = parse_opengov_bid_tab_export(
                    fpath.read_bytes(),
                    fpath.name,
                    agency=agency,
                    project_num=fpath.stem[:32],
                    title=f"{agency}: {fpath.stem}",
                    county=county,
                )

            if not parsed.lines:
                skipped += 1
                continue

            ingest_parsed_project(parsed, db_target)
            ingested += 1
            total_lines += len(parsed.lines)
        except Exception as exc:
            errors.append(f"{fpath.name}: {exc}")

    return {
        "folder": str(root),
        "files_found": len(files),
        "ingested_projects": ingested,
        "skipped_files": skipped,
        "total_bid_lines": total_lines,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Batch-import a folder of ADOT PDFs and OpenGov CSV/XLSX bid tabs"
    )
    parser.add_argument("folder", help="Path to folder containing bid tab files")
    parser.add_argument(
        "--source",
        choices=["auto", "adot", "opengov"],
        default="auto",
        help="File format routing (default: auto by extension)",
    )
    parser.add_argument(
        "--agency",
        default="City of Phoenix",
        help="Agency name for CSV/XLSX imports (default: City of Phoenix)",
    )
    parser.add_argument(
        "--county",
        default="Maricopa",
        help="County name for CSV/XLSX imports (default: Maricopa)",
    )
    parser.add_argument(
        "--db",
        default=None,
        help="Optional DB path or Postgres DATABASE_URL (defaults to env/secrets or SQLite)",
    )
    args = parser.parse_args()

    report = import_folder(
        args.folder,
        db_target=args.db,
        source=args.source,
        agency=args.agency,
        county=args.county,
    )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
