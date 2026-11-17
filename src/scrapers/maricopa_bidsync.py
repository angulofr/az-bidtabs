"""Maricopa County / MCDOT historical BidSync (Periscope) line-item scraper."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from src.db import db_session
from src.scrapers.adot import ParsedBidLine, ParsedProjectBidTab, ingest_parsed_project

BIDSYNC_MARICOPA_SEARCH_URL = (
    "https://www.bidsync.com/bidsync-app-web/shared/shared/"
    "embeddedSearchResults.xhtml?srchoid_override=217285&pastonly=1"
)
BIDSYNC_SEARCH_POST_URL = (
    "https://www.bidsync.com/bidsync-app-web/shared/shared/embeddedSearchResults.xhtml"
)
DEFAULT_CIVIL_QUERY = (
    "asphalt OR roadway OR pavement OR concrete OR waterline OR sewer OR drainage OR bridge OR traffic"
)

_MONEY_RE = re.compile(r"^\$?\s*([\d,]+\.\d{2,4})$")
_QTY_RE = re.compile(r"^[\d,]+(?:\.\d+)?$")


@dataclass
class BidSyncListing:
    project_num: str
    title: str
    agency: str
    end_date: str
    tab_url: str


def _normalize_bidsync_tab_url(raw_url: str) -> str:
    """Convert any BidSync `awgrouplist` link to unauthenticated `awgroupreview&expandall=Y`."""
    if not raw_url:
        return ""
    full = raw_url if raw_url.startswith("http") else f"https://www.bidsync.com{raw_url}"
    parsed = urlparse(full)
    qs = parse_qs(parsed.query, keep_blank_values=True)
    qs["ac"] = ["awgroupreview"]
    qs["expandall"] = ["Y"]
    qs.setdefault("supsrch", [""])
    new_query = urlencode(qs, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def _parse_bidsync_date(raw_date: str) -> str:
    text = re.sub(r"\s+", " ", (raw_date or "").strip())
    # e.g., "Oct 19, 2021 2:00:00 PM MST" -> "2021-10-19"
    m = re.match(r"^([A-Za-z]{3}\s+\d{1,2},\s+\d{4})", text)
    if m:
        try:
            return datetime.strptime(m.group(1), "%b %d, %Y").strftime("%Y-%m-%d")
        except ValueError:
            pass
    return ""


def _parse_money(val: str) -> float | None:
    cleaned = (val or "").strip()
    m = _MONEY_RE.match(cleaned)
    if not m:
        return None
    return float(m.group(1).replace(",", ""))


def _parse_qty(val: str) -> float | None:
    cleaned = (val or "").strip()
    if not _QTY_RE.match(cleaned):
        return None
    return float(cleaned.replace(",", ""))


def fetch_bidsync_listings(
    limit: int = 20,
    *,
    query: str = DEFAULT_CIVIL_QUERY,
    timeout: int = 25,
    session: requests.Session | None = None,
) -> list[BidSyncListing]:
    """Search Maricopa County's public BidSync archive and return listings with Tab URLs."""
    sess = session or requests.Session()
    sess.headers.update(
        {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    )

    resp = sess.get(BIDSYNC_MARICOPA_SEARCH_URL, timeout=timeout)
    resp.raise_for_status()
    html = resp.text

    if query:
        soup = BeautifulSoup(html, "html.parser")
        form = soup.find("form", {"id": "elasticBidSearchForm"})
        if form is not None:
            data: dict[str, str] = {}
            for inp in form.select("input"):
                name = inp.get("name")
                if not name:
                    continue
                itype = (inp.get("type") or "").lower()
                if itype in ("hidden", "text", ""):
                    data[name] = inp.get("value") or ""
            data["elasticBidSearchForm:bidSearchParametersComponent:j_idt323"] = query
            data["elasticBidSearchForm"] = "elasticBidSearchForm"
            post_resp = sess.post(BIDSYNC_SEARCH_POST_URL, data=data, timeout=timeout)
            post_resp.raise_for_status()
            html = post_resp.text

    soup = BeautifulSoup(html, "html.parser")
    listings: list[BidSyncListing] = []
    seen_tabs: set[str] = set()

    for tr in soup.select("table tr"):
        tds = tr.find_all("td", recursive=False)
        if len(tds) < 6:
            continue
        tab_anchor = tr.find("a", href=lambda h: h and "awgroup" in h)
        if not tab_anchor:
            continue
        proj_num = tds[0].get_text(" ", strip=True)
        title = tds[1].get_text(" ", strip=True)
        agency = tds[2].get_text(" ", strip=True)
        end_date = _parse_bidsync_date(tds[4].get_text(" ", strip=True))
        tab_url = _normalize_bidsync_tab_url(str(tab_anchor.get("href") or ""))
        if not proj_num or not tab_url or tab_url in seen_tabs:
            continue
        seen_tabs.add(tab_url)
        listings.append(
            BidSyncListing(
                project_num=proj_num,
                title=title,
                agency=agency,
                end_date=end_date,
                tab_url=tab_url,
            )
        )
        if len(listings) >= limit:
            break

    return listings


def _split_item_code_and_desc(raw_label: str, fallback_idx: int) -> tuple[str, str]:
    text = re.sub(r"\s+", " ", (raw_label or "").strip())
    if " - " in text:
        code_part, desc_part = text.split(" - ", 1)
        code_clean = re.sub(r"\s+", "", code_part.upper())[:40]
        return (code_clean or f"ITEM-{fallback_idx}", desc_part.strip() or text)
    return (f"ITEM-{fallback_idx}", text)


def parse_bidsync_tab_html(
    html: str,
    *,
    project_num: str = "",
    title: str = "",
    letting_date: str = "",
    source_url: str = "",
) -> ParsedProjectBidTab:
    """Parse an unauthenticated BidSync `awgroupreview&expandall=Y` HTML page."""
    soup = BeautifulSoup(html, "html.parser")

    if not project_num or not title:
        header_text = soup.get_text(" ", strip=True)
        m = re.search(r"Bid\s+#([A-Za-z0-9._-]+)\s*-\s*(.+?)\s+Expand All Offers", header_text)
        if m:
            project_num = project_num or m.group(1).strip()
            title = title or m.group(2).strip()

    parsed = ParsedProjectBidTab(
        agency="MCDOT",
        agency_project_num=project_num or "MCDOT-UNKNOWN",
        title=title or f"MCDOT {project_num}",
        county="Maricopa",
        letting_date=letting_date,
        source_url=source_url,
        spec_system="MAG",
    )

    supplier_blocks: list[tuple[str, bool, float, list[ParsedBidLine]]] = []

    target_table = None
    for tbl in soup.find_all("table"):
        if len(tbl.find_all("table")) == 0 and "Unit Price" in tbl.get_text(" ", strip=True):
            target_table = tbl
            break

    if target_table is None:
        return parsed

    current_supplier = ""
    current_awarded = False
    current_lines: list[ParsedBidLine] = []
    current_total = 0.0
    in_items = False

    def _flush_supplier() -> None:
        nonlocal current_supplier, current_awarded, current_lines, current_total, in_items
        if current_supplier and current_lines:
            total = current_total or round(sum(l.extended_total for l in current_lines), 2)
            supplier_blocks.append((current_supplier, current_awarded, total, current_lines))
        current_supplier = ""
        current_awarded = False
        current_lines = []
        current_total = 0.0
        in_items = False

    for tr in target_table.find_all("tr"):
        imgs = [str(img.get("alt") or "") for img in tr.find_all("img")]
        cells = [td.get_text(" ", strip=True) for td in tr.find_all(["th", "td"])]
        non_empty = [c for c in cells if c]
        if not non_empty:
            continue

        is_supplier_row = "Click to open supplier bid information" in imgs and not (
            len(non_empty) >= 3 and "Unit Price" in non_empty and "Qty" in non_empty
        )
        if is_supplier_row:
            _flush_supplier()
            current_supplier = non_empty[0]
            current_awarded = any("awarded" in alt.lower() for alt in imgs)
            continue

        if "Unit Price" in non_empty and "Qty" in non_empty and "Price" in non_empty:
            in_items = True
            continue

        if non_empty[0].upper().startswith("TOTAL PRICE"):
            if len(non_empty) >= 2:
                tot = _parse_money(non_empty[1])
                if tot is not None:
                    current_total = tot
            _flush_supplier()
            continue

        if in_items and current_supplier and len(non_empty) >= 4:
            label = non_empty[0]
            up = _parse_money(non_empty[1])
            qty = _parse_qty(non_empty[2])
            ext = _parse_money(non_empty[3])
            if up is None or qty is None or ext is None:
                continue
            code, desc = _split_item_code_and_desc(label, len(current_lines) + 1)
            unit = "L.SUM" if qty == 1 and any(
                kw in desc.upper()
                for kw in ("MOBILIZATION", "PARTNERING", "TRAFFIC CONTROL", "ALLOWANCE", "AZPDES")
            ) else "UNIT"
            current_lines.append(
                ParsedBidLine(
                    item_code=code,
                    description=desc,
                    unit=unit,
                    quantity=qty,
                    unit_price=up,
                    extended_total=ext,
                    bidder_name=current_supplier,
                )
            )

    _flush_supplier()

    if not supplier_blocks:
        return parsed

    # Rank bidders by total ascending (keeping awarded supplier first if marked)
    supplier_blocks.sort(key=lambda b: (not b[1], b[2]))
    for rank, (supplier, is_awarded, total, lines) in enumerate(supplier_blocks, start=1):
        is_winner = is_awarded or (rank == 1 and not any(b[1] for b in supplier_blocks))
        if is_winner:
            parsed.awarded_contractor = supplier
            parsed.winning_bid_total = total
        for line in lines:
            line.bid_rank = rank
            line.is_winner = is_winner
            parsed.lines.append(line)

    return parsed


def run_maricopa_bidsync_sync(
    db_path: str | Any = None,
    *,
    limit: int = 15,
    query: str = DEFAULT_CIVIL_QUERY,
    dry_run: bool = False,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Discover Maricopa County / MCDOT civil bids in BidSync and ingest line items."""
    sess = session or requests.Session()
    listings = fetch_bidsync_listings(limit=limit, query=query, session=sess)

    with db_session(db_path) as conn:
        existing_nums = {
            r[0]
            for r in conn.execute(
                "SELECT agency_project_num FROM projects WHERE agency = 'MCDOT'"
            ).fetchall()
        }

    ingested = 0
    skipped = 0
    total_lines = 0
    errors: list[str] = []

    for item in listings:
        if item.project_num in existing_nums:
            skipped += 1
            continue
        try:
            resp = sess.get(item.tab_url, timeout=25)
            if not resp.ok or "DPXLogin" in resp.url:
                skipped += 1
                continue
            parsed = parse_bidsync_tab_html(
                resp.text,
                project_num=item.project_num,
                title=item.title,
                letting_date=item.end_date,
                source_url=item.tab_url,
            )
            if not parsed.lines:
                skipped += 1
                continue
            if not dry_run:
                ingest_parsed_project(parsed, db_path)
            existing_nums.add(parsed.agency_project_num)
            ingested += 1
            total_lines += len(parsed.lines)
        except Exception as exc:
            errors.append(f"{item.project_num}: {exc}")

    return {
        "agency": "MCDOT",
        "discovered": len(listings),
        "ingested": ingested,
        "skipped": skipped,
        "total_bid_lines": total_lines,
        "errors": errors,
    }
