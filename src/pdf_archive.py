"""Durable PDF artifact archival helper (local disk + public GitHub Release URLs)."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Any

DEFAULT_PDF_DIR = Path(os.environ.get("AZ_BIDTABS_PDF_DIR", "data/pdfs"))
DEFAULT_GITHUB_REPO = os.environ.get("AZ_BIDTABS_GITHUB_REPO", "angulofr/az-bidtabs")

BUCKET_RELEASE_TAGS: dict[str, str] = {
    "adot": "pdf-archive-adot",
    "stb": "pdf-archive-stb",
    "maricopa": "pdf-archive-maricopa",
}


def release_tag_for_bucket(bucket: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_-]", "", (bucket or "adot").lower()) or "adot"
    return BUCKET_RELEASE_TAGS.get(cleaned, f"pdf-archive-{cleaned}")


def release_download_url(
    bucket: str,
    asset_name: str,
    *,
    repo: str = DEFAULT_GITHUB_REPO,
) -> str:
    tag = release_tag_for_bucket(bucket)
    return f"https://github.com/{repo}/releases/download/{tag}/{asset_name}"


def save_pdf_artifact(
    pdf_bytes: bytes,
    *,
    bucket: str,
    filename_hint: str,
    source_url: str,
    pdf_dir: Path | str | None = None,
) -> dict[str, Any] | None:
    """Persist PDF bytes deterministically to local disk and return document metadata.

    Returns `None` when `pdf_bytes` is empty or does not look like a PDF.
    """
    if not pdf_bytes or not bytes(pdf_bytes).lstrip()[:8].startswith(b"%PDF"):
        return None

    raw = bytes(pdf_bytes)
    digest = hashlib.sha256(raw).hexdigest()
    safe_bucket = re.sub(r"[^a-z0-9_-]", "", (bucket or "adot").lower()) or "adot"

    stem = Path(filename_hint.split("?")[0]).stem or "document"
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._-")[:64] or "document"
    asset_name = f"{safe_bucket}_{safe_stem}_{digest[:10]}.pdf"

    base_dir = Path(pdf_dir) if pdf_dir is not None else DEFAULT_PDF_DIR
    target_dir = base_dir / safe_bucket
    target_dir.mkdir(parents=True, exist_ok=True)
    target_path = target_dir / asset_name
    if not target_path.exists() or target_path.stat().st_size != len(raw):
        target_path.write_bytes(raw)

    archive_url: str | None = None
    if os.environ.get("AZ_BIDTABS_ASSUME_RELEASE_URLS", "").strip() == "1":
        archive_url = release_download_url(safe_bucket, asset_name)

    return {
        "bucket": safe_bucket,
        "asset_name": asset_name,
        "local_path": str(target_path),
        "sha256": digest,
        "file_size": len(raw),
        "source_url": source_url,
        "archive_url": archive_url,
    }
