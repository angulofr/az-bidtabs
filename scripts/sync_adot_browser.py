"""Local Playwright browser sync for ADOT Bid Tabulation PDFs -> Neon Postgres / SQLite.

Runs on your desktop (residential/office IP) so normal browser navigation works on
azdot.gov, checks Neon Postgres for already-archived projects, downloads only new
ADOT Bid Tab PDFs, parses them, and ingests them automatically.

Setup (one time):
    pip install -r requirements.txt playwright
    playwright install chromium

Usage:
    set DATABASE_URL=postgresql://...
    python scripts/sync_adot_browser.py
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
from pathlib import Path
from urllib.parse import urljoin

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.db import db_session, init_db
from src.scrapers.adot import (
    ADOT_BID_TABS_URL,
    ingest_parsed_project,
    parse_adot_bid_tab_pdf,
)


def extract_tracs_from_text(text: str) -> str | None:
    m = re.search(r"(?<![A-Z0-9])([FH]\d{3,5}[0-9A-Z]{2,4})(?![A-Z0-9])", text, re.IGNORECASE)
    return m.group(1).upper() if m else None


def sync_adot_via_browser(
    *,
    db_target: Path | str | None = None,
    url: str = ADOT_BID_TABS_URL,
    limit: int = 100,
    headless: bool = False,
    wait_seconds: int = 8,
    save_dir: Path | None = None,
) -> dict[str, object]:
    """Open ADOT Bid Tabulations in Chromium, download new PDFs, and ingest into DB."""
    from playwright.sync_api import sync_playwright

    init_db(db_target)
    with db_session(db_target) as conn:
        existing_nums = {
            r[0]
            for r in conn.execute(
                "SELECT agency_project_num FROM projects WHERE agency = 'ADOT'"
            ).fetchall()
        }

    download_root = Path(save_dir) if save_dir else Path(tempfile.mkdtemp(prefix="adot_tabs_"))
    download_root.mkdir(parents=True, exist_ok=True)

    discovered = 0
    ingested = 0
    skipped = 0
    total_lines = 0
    errors: list[str] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(accept_downloads=True)
        page = context.new_page()

        print(f"Opening {url} ...")
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        # Allow page scripts/table rendering (and any user interaction if needed) to settle
        page.wait_for_timeout(wait_seconds * 1000)

        anchors = page.eval_on_selector_all(
            "a[href]",
            """els => els.map(e => ({
                href: e.getAttribute('href') || '',
                text: (e.innerText || '').trim()
            }))""",
        )

        pdf_links: list[dict[str, str]] = []
        seen: set[str] = set()
        for a in anchors:
            href = a["href"].strip()
            if ".pdf" not in href.lower():
                continue
            full_url = urljoin(url, href)
            if full_url in seen:
                continue
            seen.add(full_url)
            title = a["text"] or Path(href.split("?")[0]).stem
            # Prefer "Bid Tab" PDFs over unrelated PDFs on the page
            combined = f"{title} {href}".lower()
            if "bid" not in combined and "tab" not in combined and not extract_tracs_from_text(combined):
                continue
            tracs = extract_tracs_from_text(f"{title} {href}") or Path(href.split("?")[0]).stem[:24]
            pdf_links.append({"url": full_url, "title": title, "project_num": tracs})
            if len(pdf_links) >= limit:
                break

        discovered = len(pdf_links)
        print(f"Discovered {discovered} ADOT Bid Tab PDF links.")

        for item in pdf_links:
            proj_num = item["project_num"]
            if proj_num in existing_nums:
                skipped += 1
                continue

            try:
                # Fetch PDF using browser context cookies/session so WAF cookies are reused
                resp = context.request.get(item["url"], timeout=60000)
                if not resp.ok:
                    errors.append(f"{proj_num}: HTTP {resp.status}")
                    continue
                pdf_bytes = resp.body()
                fname = Path(item["url"].split("?")[0]).name or f"{proj_num}.pdf"
                if save_dir:
                    (download_root / fname).write_bytes(pdf_bytes)

                parsed = parse_adot_bid_tab_pdf(pdf_bytes, filename=fname)
                if parsed.agency_project_num == "ADOT-UNKNOWN":
                    parsed.agency_project_num = proj_num
                parsed.source_url = item["url"]

                if not parsed.lines:
                    skipped += 1
                    continue

                ingest_parsed_project(parsed, db_target)
                existing_nums.add(parsed.agency_project_num)
                ingested += 1
                total_lines += len(parsed.lines)
                print(f"  + Ingested {parsed.agency_project_num} ({len(parsed.lines)} bid lines)")
            except Exception as exc:
                errors.append(f"{proj_num}: {exc}")

        browser.close()

    return {
        "discovered": discovered,
        "ingested": ingested,
        "skipped": skipped,
        "total_bid_lines": total_lines,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync ADOT Bid Tab PDFs using Playwright")
    parser.add_argument("--limit", type=int, default=100, help="Max PDFs to inspect")
    parser.add_argument("--headless", action="store_true", help="Run Chromium headless")
    parser.add_argument("--wait", type=int, default=8, help="Seconds to wait after page load")
    parser.add_argument("--save-dir", type=str, default=None, help="Optional folder to save PDFs")
    parser.add_argument("--db", type=str, default=None, help="Optional DB path or Postgres URI")
    args = parser.parse_args()

    report = sync_adot_via_browser(
        db_target=args.db,
        limit=args.limit,
        headless=args.headless,
        wait_seconds=args.wait,
        save_dir=Path(args.save_dir) if args.save_dir else None,
    )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
