"""Upload locally archived PDFs to public GitHub Releases and bind `archive_url` in Postgres/SQLite."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db import db_session, init_db
from src.pdf_archive import (
    DEFAULT_GITHUB_REPO,
    DEFAULT_PDF_DIR,
    release_download_url,
    release_tag_for_bucket,
)


def _run_gh(args: list[str], *, repo: str) -> subprocess.CompletedProcess[str]:
    cmd = ["gh", *args, "-R", repo]
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _ensure_release_and_existing_assets(tag: str, bucket: str, *, repo: str) -> set[str]:
    res = _run_gh(["release", "view", tag, "--json", "assets"], repo=repo)
    if res.returncode != 0:
        create_res = _run_gh(
            [
                "release",
                "create",
                tag,
                "--title",
                f"AZ BidTabs PDF Archive ({bucket})",
                "--notes",
                "Durable public archive of scraped ADOT & Maricopa County PDF bid tabs, awards, and contracts.",
            ],
            repo=repo,
        )
        if create_res.returncode != 0:
            raise RuntimeError(f"Failed creating release {tag}: {create_res.stderr}")
        return set()

    try:
        payload = json.loads(res.stdout or "{}")
        return {
            str(a.get("name") or "")
            for a in (payload.get("assets") or [])
            if a.get("name")
        }
    except json.JSONDecodeError:
        return set()


def upload_and_link_pdfs(
    *,
    pdf_dir: Path = DEFAULT_PDF_DIR,
    db_path: str | None = None,
    repo: str = DEFAULT_GITHUB_REPO,
    batch_size: int = 25,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Upload PDFs from `pdf_dir/<bucket>/*.pdf` to GitHub Releases and set `project_documents.archive_url`."""
    init_db(db_path)
    summary: dict[str, Any] = {"buckets": {}, "db_updated": 0}
    if not pdf_dir.exists():
        return summary

    uploaded_assets_by_bucket: dict[str, list[str]] = {}

    for bucket_dir in sorted(p for p in pdf_dir.iterdir() if p.is_dir()):
        bucket = bucket_dir.name
        pdfs = sorted(bucket_dir.glob("*.pdf"))
        if not pdfs:
            continue
        tag = release_tag_for_bucket(bucket)
        existing_assets = set() if dry_run else _ensure_release_and_existing_assets(tag, bucket, repo=repo)
        to_upload = [p for p in pdfs if p.name not in existing_assets]

        uploaded_count = 0
        errors: list[str] = []
        if not dry_run:
            for idx in range(0, len(to_upload), batch_size):
                chunk = to_upload[idx : idx + batch_size]
                res = _run_gh(
                    ["release", "upload", tag, "--clobber", *[str(p) for p in chunk]],
                    repo=repo,
                )
                if res.returncode == 0:
                    uploaded_count += len(chunk)
                    existing_assets.update(p.name for p in chunk)
                else:
                    errors.append(res.stderr.strip()[:300])
        else:
            existing_assets.update(p.name for p in pdfs)

        uploaded_assets_by_bucket[bucket] = sorted(existing_assets)
        summary["buckets"][bucket] = {
            "release_tag": tag,
            "local_pdfs": len(pdfs),
            "newly_uploaded": uploaded_count,
            "errors": errors,
        }

    if not dry_run and uploaded_assets_by_bucket:
        with db_session(db_path) as conn:
            pending_rows = conn.execute(
                """
                SELECT doc_id, bucket, asset_name
                FROM project_documents
                WHERE asset_name IS NOT NULL
                  AND (archive_url IS NULL OR archive_url = '')
                """
            ).fetchall()
            updated = 0
            for doc_id, bucket, asset_name in pending_rows:
                b = str(bucket or "adot")
                a = str(asset_name or "")
                if a and a in set(uploaded_assets_by_bucket.get(b, [])):
                    url = release_download_url(b, a, repo=repo)
                    conn.execute(
                        "UPDATE project_documents SET archive_url = ? WHERE doc_id = ?",
                        (url, int(doc_id)),
                    )
                    updated += 1
            summary["db_updated"] = updated

    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload saved PDFs to GitHub Releases")
    parser.add_argument("--pdf-dir", type=Path, default=DEFAULT_PDF_DIR)
    parser.add_argument("--db", type=str, default=None)
    parser.add_argument("--repo", type=str, default=os.environ.get("GITHUB_REPOSITORY", DEFAULT_GITHUB_REPO))
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    res = upload_and_link_pdfs(
        pdf_dir=args.pdf_dir,
        db_path=args.db,
        repo=args.repo,
        dry_run=args.dry_run,
    )
    print(json.dumps(res, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
