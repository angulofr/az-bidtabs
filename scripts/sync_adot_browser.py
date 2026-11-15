"""Browser-based sync for ADOT Bid Tabulation PDFs -> Neon Postgres / SQLite.

Works in three modes:
1. Cloud GitHub Actions headless Chromium (default when run with --headless)
2. Managed Cloud Browser via CDP WebSocket (set BROWSER_WS_ENDPOINT / --ws-endpoint)
   or residential proxy (set PROXY_SERVER / --proxy)
3. Local desktop Chromium (--wait 8, interactive or Task Scheduler)

Usage:
    set DATABASE_URL=postgresql://...
    python scripts/sync_adot_browser.py --headless --limit 25
"""

from __future__ import annotations

import argparse
import json
import os
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

_DESKTOP_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def extract_tracs_from_text(text: str) -> str | None:
    m = re.search(r"(?<![A-Z0-9])([FH]\d{3,5}[0-9A-Z]{2,4})(?![A-Z0-9])", text, re.IGNORECASE)
    return m.group(1).upper() if m else None


def _get_already_pdf_archived_adot_nums(db_target: Path | str | None) -> set[str]:
    """Return ADOT project numbers that already have named-contractor PDF data.

    Projects imported solely from E2C2 (anonymous 'ADOT LOW BIDDER (RANK 1)') are
    intentionally NOT skipped so the PDF parser can upgrade them with contractor names.
    """
    with db_session(db_target) as conn:
        rows = conn.execute(
            """
            SELECT p.agency_project_num, p.source_url, c.canonical_name
            FROM projects p
            LEFT JOIN contractors c ON p.awarded_contractor_id = c.contractor_id
            WHERE p.agency = 'ADOT'
            """
        ).fetchall()

    archived: set[str] = set()
    for proj_num, src_url, contractor_name in rows:
        cname = str(contractor_name or "")
        is_anonymous_e2c2 = cname.startswith("ADOT LOW BIDDER") or cname.startswith("ADOT BIDDER")
        if src_url == ADOT_BID_TABS_URL or (cname and not is_anonymous_e2c2):
            archived.add(str(proj_num))
    return archived


def _collect_pdf_links_from_frames(
    page,
    base_url: str,
    *,
    limit: int,
    seen: set[str],
) -> tuple[list[dict[str, str]], list[str], int]:
    """Scan all frames on the current Playwright page for ADOT Bid Tab PDF links."""
    anchors: list[dict[str, str]] = []
    frame_urls: list[str] = []
    for frame in page.frames:
        frame_urls.append(frame.url)
        try:
            frame_anchors = frame.eval_on_selector_all(
                "a[href]",
                """els => els.map(e => {
                    const row = e.closest('tr');
                    const rowText = row ? (row.innerText || row.textContent || '') : '';
                    return {
                        href: e.getAttribute('href') || '',
                        text: (e.innerText || e.textContent || '').trim(),
                        rowText: rowText.replace(/\\s+/g, ' ').trim(),
                        titleAttr: e.getAttribute('title') || e.getAttribute('aria-label') || ''
                    };
                })""",
            )
            for a in frame_anchors:
                a["base_url"] = frame.url or base_url
            anchors.extend(frame_anchors)
        except Exception:
            pass

    new_links: list[dict[str, str]] = []
    for a in anchors:
        href = a["href"].strip()
        full_url = urljoin(a.get("base_url") or base_url, href)
        href_lower = full_url.lower()
        is_s3_adot_doc = "azdotcloud-prod-s3-files/document/" in href_lower
        is_pdf_href = ".pdf" in href_lower
        if not (is_s3_adot_doc or is_pdf_href):
            continue
        if full_url in seen:
            continue
        seen.add(full_url)

        row_ctx = " ".join(
            p for p in (a.get("text", ""), a.get("titleAttr", ""), a.get("rowText", "")) if p
        ).strip()
        combined = f"{row_ctx} {href}".lower()
        if not is_s3_adot_doc and (
            "bid" not in combined and "tab" not in combined and not extract_tracs_from_text(combined)
        ):
            continue

        tracs = (
            extract_tracs_from_text(f"{row_ctx} {href}")
            or Path(href.split("?")[0]).stem[:24]
        )
        title = a.get("rowText") or a.get("text") or Path(href.split("?")[0]).stem
        new_links.append({"url": full_url, "title": title[:160], "project_num": tracs})
        if len(seen) >= limit:
            break

    return new_links, frame_urls, len(anchors)


def _advance_tabulations_iframe_page(page) -> bool:
    """Try advancing to the next page inside an ADOT tabulations iframe if pagination exists."""
    for frame in page.frames:
        if "tabulations" not in (frame.url or "").lower():
            continue
        try:
            clicked = frame.evaluate(
                """() => {
                    const candidates = Array.from(document.querySelectorAll(
                        'button, a, [role="button"], li.next a, .pagination a, .pagination button'
                    ));
                    for (const el of candidates) {
                        const txt = (el.innerText || el.textContent || el.getAttribute('aria-label') || '').trim().toLowerCase();
                        const isNext = txt === 'next' || txt === '›' || txt === '»' || txt === 'next page' || txt.startsWith('next ');
                        if (!isNext) continue;
                        const disabled = el.disabled || el.getAttribute('aria-disabled') === 'true'
                            || (el.className || '').toLowerCase().includes('disabled')
                            || (el.parentElement && (el.parentElement.className || '').toLowerCase().includes('disabled'));
                        if (disabled) continue;
                        el.click();
                        return true;
                    }
                    return false;
                }"""
            )
            if clicked:
                page.wait_for_timeout(2000)
                return True
        except Exception:
            pass
    return False


def sync_adot_via_browser(
    *,
    db_target: Path | str | None = None,
    url: str = ADOT_BID_TABS_URL,
    limit: int = 100,
    headless: bool = False,
    wait_seconds: int = 8,
    save_dir: Path | None = None,
    ws_endpoint: str | None = None,
    proxy_server: str | None = None,
    proxy_username: str | None = None,
    proxy_password: str | None = None,
    dry_run: bool = False,
    probe_urls: list[str] | None = None,
) -> dict[str, object]:
    """Open ADOT Bid Tabulations in Chromium (local or cloud CDP), download new PDFs, and ingest."""
    from playwright.sync_api import sync_playwright

    ws_endpoint = (ws_endpoint or os.environ.get("BROWSER_WS_ENDPOINT", "")).strip() or None
    proxy_server = (proxy_server or os.environ.get("PROXY_SERVER", "")).strip() or None
    proxy_username = (proxy_username or os.environ.get("PROXY_USERNAME", "")).strip() or None
    proxy_password = (proxy_password or os.environ.get("PROXY_PASSWORD", "")).strip() or None

    init_db(db_target)
    existing_nums = _get_already_pdf_archived_adot_nums(db_target)

    download_root = Path(save_dir) if save_dir else Path(tempfile.mkdtemp(prefix="adot_tabs_"))
    download_root.mkdir(parents=True, exist_ok=True)

    discovered = 0
    ingested = 0
    skipped = 0
    total_lines = 0
    page_status: int | None = None
    page_title = ""
    mode = "cdp" if ws_endpoint else ("proxy" if proxy_server else "direct")
    errors: list[str] = []
    probe_results: list[dict[str, object]] = []

    with sync_playwright() as pw:
        if ws_endpoint:
            print(f"Connecting to managed cloud browser via CDP ...")
            browser = pw.chromium.connect_over_cdp(ws_endpoint)
            context = (
                browser.contexts[0]
                if browser.contexts
                else browser.new_context(
                    accept_downloads=True,
                    user_agent=_DESKTOP_UA,
                    viewport={"width": 1440, "height": 900},
                    locale="en-US",
                )
            )
        else:
            launch_kwargs: dict[str, object] = {
                "headless": headless,
                "args": ["--disable-blink-features=AutomationControlled"],
            }
            if proxy_server:
                proxy_cfg: dict[str, str] = {"server": proxy_server}
                if proxy_username:
                    proxy_cfg["username"] = proxy_username
                if proxy_password:
                    proxy_cfg["password"] = proxy_password
                launch_kwargs["proxy"] = proxy_cfg
            browser = pw.chromium.launch(**launch_kwargs)
            context = browser.new_context(
                accept_downloads=True,
                user_agent=_DESKTOP_UA,
                viewport={"width": 1440, "height": 900},
                locale="en-US",
            )

        page = context.new_page()
        page.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', { get: () => undefined });"
        )

        print(f"Opening {url} (mode={mode}, headless={headless}) ...")
        nav_resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page_status = nav_resp.status if nav_resp else None
        try:
            page.wait_for_load_state("networkidle", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(wait_seconds * 1000)
        page_title = page.title()
        print(f"Loaded page: HTTP {page_status} | Title: {page_title!r} | Frames: {len(page.frames)}")

        pdf_links: list[dict[str, str]] = []
        seen: set[str] = set()
        frame_urls: list[str] = []
        total_anchors = 0

        for page_idx in range(10):
            batch, frame_urls, anchor_count = _collect_pdf_links_from_frames(
                page, url, limit=limit, seen=seen
            )
            if page_idx == 0:
                total_anchors = anchor_count
            pdf_links.extend(batch)
            if len(pdf_links) >= limit or not batch:
                break
            if not _advance_tabulations_iframe_page(page):
                break

        discovered = len(pdf_links)
        print(f"Discovered {discovered} ADOT Bid Tab PDF links (scanned {total_anchors} anchors on page 1).")
        if discovered == 0:
            print(f"Frame URLs: {frame_urls}")
        elif dry_run and pdf_links:
            for idx, item in enumerate(pdf_links):
                print(f"  [{idx}] {item['project_num']} | {item['title']}")
            for idx, sample_item in enumerate(pdf_links[:3]):
                try:
                    s_resp = context.request.get(sample_item["url"], timeout=60000)
                    s_bytes = s_resp.body() if s_resp.ok else b""
                    is_pdf = s_bytes.lstrip().startswith(b"%PDF")
                    if is_pdf:
                        s_parsed = parse_adot_bid_tab_pdf(s_bytes, filename=f"{sample_item['project_num']}.pdf")
                        print(
                            f"Sample[{idx}] fetch: HTTP {s_resp.status} bytes={len(s_bytes)} | "
                            f"parsed proj={s_parsed.agency_project_num!r} date={s_parsed.letting_date!r} "
                            f"contractor={s_parsed.awarded_contractor!r} lines={len(s_parsed.lines)}"
                        )
                    else:
                        print(f"Sample[{idx}] fetch: HTTP {s_resp.status} bytes={len(s_bytes)} is_pdf=False")
                except Exception as exc:
                    print(f"Sample[{idx}] dry-run check error: {exc}")

        if probe_urls:
            for p_url in probe_urls:
                try:
                    p_resp = page.goto(p_url, wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(4000)
                    p_status = p_resp.status if p_resp else None
                    p_title = page.title()
                    summary = page.evaluate(
                        """() => {
                            const anchors = Array.from(document.querySelectorAll('a[href]')).map(e => ({
                                href: e.getAttribute('href') || '',
                                text: (e.innerText || e.textContent || '').replace(/\\s+/g, ' ').trim()
                            }));
                            const docLinks = anchors.filter(a => {
                                const h = a.href.toLowerCase();
                                const t = a.text.toLowerCase();
                                return h.includes('.pdf') || h.includes('.xlsx') || h.includes('.csv')
                                    || t.includes('bid result') || t.includes('tabulation')
                                    || h.includes('download') || h.includes('file') || h.includes('award');
                            });
                            return {
                                total_anchors: anchors.length,
                                table_rows: document.querySelectorAll('tr').length,
                                doc_links_count: docLinks.length,
                                sample_docs: docLinks.slice(0, 6)
                            };
                        }"""
                    )
                    info = {
                        "url": p_url,
                        "status": p_status,
                        "title": p_title,
                        **summary,
                    }
                    probe_results.append(info)
                    print(
                        f"Probe {p_url}: HTTP {p_status} | Title={p_title!r} | "
                        f"rows={summary['table_rows']} doc_links={summary['doc_links_count']}"
                    )
                    for s in summary["sample_docs"]:
                        print(f"    -> {s['text'][:60]!r} => {s['href'][:120]}")
                except Exception as exc:
                    probe_results.append({"url": p_url, "error": str(exc)})
                    print(f"Probe {p_url} failed: {exc}")

        for item in pdf_links:
            proj_num = item["project_num"]
            if proj_num in existing_nums:
                skipped += 1
                continue
            if dry_run:
                ingested += 1
                continue

            try:
                resp = context.request.get(item["url"], timeout=60000)
                if not resp.ok:
                    errors.append(f"{proj_num}: HTTP {resp.status}")
                    continue
                pdf_bytes = resp.body()
                if not pdf_bytes.lstrip().startswith(b"%PDF"):
                    errors.append(f"{proj_num}: Response was not a PDF (possible WAF/HTML block)")
                    continue

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

    report: dict[str, object] = {
        "mode": mode,
        "page_status": page_status,
        "page_title": page_title,
        "discovered": discovered,
        "ingested": ingested,
        "skipped": skipped,
        "total_bid_lines": total_lines,
        "errors": errors,
    }
    if probe_results:
        report["probe_results"] = probe_results
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync ADOT Bid Tab PDFs using Playwright")
    parser.add_argument("--limit", type=int, default=100, help="Max PDFs to inspect")
    parser.add_argument("--headless", action="store_true", help="Run Chromium headless")
    parser.add_argument("--wait", type=int, default=8, help="Seconds to wait after page load")
    parser.add_argument("--save-dir", type=str, default=None, help="Optional folder to save PDFs")
    parser.add_argument("--db", type=str, default=None, help="Optional DB path or Postgres URI")
    parser.add_argument("--ws-endpoint", type=str, default=None, help="Managed browser CDP WebSocket URL")
    parser.add_argument("--proxy", type=str, default=None, help="HTTP/SOCKS proxy URL")
    parser.add_argument("--dry-run", action="store_true", help="Discover PDFs without ingesting")
    parser.add_argument("--probe-url", action="append", default=[], help="Extra URLs to probe in Playwright")
    args = parser.parse_args()

    report = sync_adot_via_browser(
        db_target=args.db,
        limit=args.limit,
        headless=args.headless,
        wait_seconds=args.wait,
        save_dir=Path(args.save_dir) if args.save_dir else None,
        ws_endpoint=args.ws_endpoint,
        proxy_server=args.proxy,
        dry_run=args.dry_run,
        probe_urls=args.probe_url or None,
    )
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

