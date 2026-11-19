"""Maricopa County / MCDOT historical BidSync (Periscope) line-item scraper."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup

from src.db import (
    db_session,
    upsert_contractor,
    upsert_project,
    upsert_project_bid,
    upsert_project_document,
)
from src.scrapers.adot import ParsedBidLine, ParsedProjectBidTab, ingest_parsed_project
from src.scrapers.bonfire import is_civil_project

BIDSYNC_MARICOPA_SEARCH_URL = (
    "https://www.bidsync.com/bidsync-app-web/shared/shared/"
    "embeddedSearchResults.xhtml?srchoid_override=217285&pastonly=1"
)
BIDSYNC_SEARCH_POST_URL = (
    "https://www.bidsync.com/bidsync-app-web/shared/shared/embeddedSearchResults.xhtml"
)
_BIDSYNC_TABLE_ID = "elasticBidSearchForm:bidSearchParametersComponent:linksBidSearchResults"
DEFAULT_CIVIL_QUERY = (
    "DBB OR asphalt OR roadway OR pavement OR concrete OR waterline OR sewer OR drainage OR bridge OR traffic"
)

_MARICOPA_CIVIL_ABBREV_RE = re.compile(
    r"\b(RD|HWY|AVE|BLVD|PKWY|FCD|MCDOT|GUARD\s*RAIL|CHANNEL|CULVERT|SIGNING|SIGNAGE|"
    r"SIGNAL|STRIPI?NG|OVERLAY|MILLING|DETENTION|RETENTION|LEVEE|CANAL|RIPRAP|"
    r"ROUNDABOUT|DRAINAGE|EXCAVATION|GRADING|PAVING|RESURFACING|INTERSECTION|"
    r"FLOOD|STORMWATER|SEWER|WATERLINE|SIDEWALK|CURB|BRIDGE|ROADWAY|ROAD|STREET|"
    r"CONCRETE|ASPHALT|PAVEMENT|AGGREGATE|GRAVEL|PARKING\s+LOT|DRY\s*WELL|"
    r"CIVIL\s+ENGINEERING|LAND\s+SURVEY\w*|DUST\s+CONTROL|STABILIZATION|ITS)\b"
    r"|(?:CRACK|SLURRY|CHIP)\s+SEAL|\bTT\d{3,5}\b|\bJOC\b|\bMC\d{3,5}\b",
    re.IGNORECASE,
)
_MARICOPA_NON_CIVIL_EXCLUSIONS = (
    "TRAILER",
    "BOAT",
    "KAYAK",
    "MATTRESS",
    "SHOE",
    "FLOUR",
    "ANIMAL",
    "JANITORIAL",
    "AMMUNITION",
    "GLOVE",
    "UNIFORM",
    "SOFTWARE",
    "COPIER",
    "PRINTING SERVICE",
    "PHARMACEUTICAL",
    "VETERINARY",
    "FURNITURE",
)


def is_maricopa_civil_project(title: str) -> bool:
    """Return True if a Maricopa BidSync title looks like heavy-civil/MCDOT/FCD construction."""
    upper = (title or "").upper()
    if not upper:
        return False
    strong_civil = any(
        tok in upper
        for tok in ("ROADWAY", "PAVING", "ASPHALT", "BRIDGE", "CULVERT", "STORM DRAIN", "MCDOT", "FCD", "SEWER", "WATERLINE")
    )
    if any(excl in upper for excl in _MARICOPA_NON_CIVIL_EXCLUSIONS) and not strong_civil:
        return False
    return is_civil_project(title) or bool(_MARICOPA_CIVIL_ABBREV_RE.search(title))

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


def _extract_listings_from_soup(
    soup: BeautifulSoup,
    seen_tabs: set[str],
    *,
    civil_only: bool = True,
) -> list[BidSyncListing]:
    batch: list[BidSyncListing] = []
    for tr in soup.select("tr"):
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
        if civil_only and not is_maricopa_civil_project(title):
            continue
        seen_tabs.add(tab_url)
        batch.append(
            BidSyncListing(
                project_num=proj_num,
                title=title,
                agency=agency,
                end_date=end_date,
                tab_url=tab_url,
            )
        )
    return batch


def _parse_primefaces_ajax_response(xml_text: str) -> tuple[BeautifulSoup | None, str | None]:
    """Extract HTML table update and updated ViewState from PrimeFaces partial XML."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None, None
    html_chunks: list[str] = []
    new_vs: str | None = None
    for upd in root.findall(".//update"):
        uid = upd.get("id") or ""
        text = upd.text or ""
        if _BIDSYNC_TABLE_ID in uid:
            html_chunks.append(text)
        elif "ViewState" in uid and text.strip():
            new_vs = text.strip()
    if not html_chunks:
        return None, new_vs
    return BeautifulSoup("".join(html_chunks), "html.parser"), new_vs


def fetch_bidsync_listings(
    limit: int = 20,
    *,
    query: str = DEFAULT_CIVIL_QUERY,
    civil_only: bool = True,
    max_ajax_pages: int = 100,
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
    seen_tabs: set[str] = set()
    listings = _extract_listings_from_soup(soup, seen_tabs, civil_only=civil_only)

    # Paginate via PrimeFaces AJAX in chunks of 50 when more historical bids are requested.
    # BidSync occasionally renders 45-47 rows on full 50-row pages, so always step by 50
    # and stop only when 0 rows return.
    vs_input = soup.find("input", {"name": "javax.faces.ViewState"})
    vs_val = str(vs_input.get("value") or "") if vs_input else ""
    first_offset = 0 if not query else 10

    for _ in range(max_ajax_pages):
        if len(listings) >= limit or not vs_val:
            break
        ajax_data = {
            "javax.faces.partial.ajax": "true",
            "javax.faces.source": _BIDSYNC_TABLE_ID,
            "javax.faces.partial.execute": _BIDSYNC_TABLE_ID,
            "javax.faces.partial.render": _BIDSYNC_TABLE_ID,
            _BIDSYNC_TABLE_ID: _BIDSYNC_TABLE_ID,
            f"{_BIDSYNC_TABLE_ID}_pagination": "true",
            f"{_BIDSYNC_TABLE_ID}_first": str(first_offset),
            f"{_BIDSYNC_TABLE_ID}_rows": "50",
            f"{_BIDSYNC_TABLE_ID}_encodeFeature": "true",
            "elasticBidSearchForm": "elasticBidSearchForm",
            "elasticBidSearchForm:bidSearchParametersComponent:j_idt323": query,
            f"{_BIDSYNC_TABLE_ID}_rppDD": "50",
            "javax.faces.ViewState": vs_val,
        }
        try:
            ajax_resp = sess.post(BIDSYNC_SEARCH_POST_URL, data=ajax_data, timeout=timeout)
            if not ajax_resp.ok:
                break
            ajax_soup, next_vs = _parse_primefaces_ajax_response(ajax_resp.text)
            if next_vs:
                vs_val = next_vs
            if ajax_soup is None:
                break
            raw_data_rows = sum(
                1 for tr in ajax_soup.select("tr") if len(tr.find_all("td", recursive=False)) >= 6
            )
            if raw_data_rows == 0:
                break
            listings.extend(
                _extract_listings_from_soup(ajax_soup, seen_tabs, civil_only=civil_only)
            )
            first_offset += 50
            if raw_data_rows < 25:
                break
        except requests.RequestException:
            break

    return listings[:limit]


def _split_item_code_and_desc(raw_label: str, fallback_idx: int) -> tuple[str, str]:
    text = re.sub(r"\s+", " ", (raw_label or "").strip())
    if " - " in text:
        code_part, desc_part = text.split(" - ", 1)
        code_clean = re.sub(r"\s+", "", code_part.upper())[:40]
        return (code_clean or f"ITEM-{fallback_idx}", desc_part.strip() or text)
    return (f"ITEM-{fallback_idx}", text)


def parse_bidsync_tab_with_totals(
    html: str,
    *,
    project_num: str = "",
    title: str = "",
    letting_date: str = "",
    source_url: str = "",
) -> tuple[ParsedProjectBidTab, list[tuple[str, float, bool]]]:
    """Parse an unauthenticated BidSync `awgroupreview&expandall=Y` HTML page.

    Returns `(parsed_project, bidder_totals)` where `bidder_totals` contains
    `(supplier_name, bid_total, is_winner)` even for lump-sum / total-only bids
    that have no itemized unit-price lines.
    """
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
    raw_totals: list[tuple[str, bool, float]] = []

    target_table = None
    leaf_tables = [tbl for tbl in soup.find_all("table") if len(tbl.find_all("table")) == 0]
    for tbl in leaf_tables:
        if "Unit Price" in tbl.get_text(" ", strip=True):
            target_table = tbl
            break
    if target_table is None:
        for tbl in leaf_tables:
            txt_upper = tbl.get_text(" ", strip=True).upper()
            has_supplier_img = any(
                "click to open supplier bid information" in str(img.get("alt") or "").lower()
                for img in tbl.find_all("img")
            )
            if has_supplier_img or "TOTAL PRICE:" in txt_upper:
                target_table = tbl
                break

    if target_table is None:
        return parsed, []

    current_supplier = ""
    current_awarded = False
    current_lines: list[ParsedBidLine] = []
    current_total = 0.0
    in_items = False

    def _flush_supplier() -> None:
        nonlocal current_supplier, current_awarded, current_lines, current_total, in_items
        if current_supplier:
            total = current_total or (
                round(sum(l.extended_total for l in current_lines), 2) if current_lines else 0.0
            )
            if current_lines:
                supplier_blocks.append((current_supplier, current_awarded, total, current_lines))
            if total > 0 or current_awarded:
                raw_totals.append((current_supplier, current_awarded, total))
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

    # Build ranked bidder_totals even for lump-sum / total-only contracts
    raw_totals.sort(key=lambda b: (not b[1], b[2]))
    any_marked_awarded = any(b[1] for b in raw_totals)
    bidder_totals: list[tuple[str, float, bool]] = []
    for rank, (supplier, is_awarded, total) in enumerate(raw_totals, start=1):
        is_winner = is_awarded or (rank == 1 and not any_marked_awarded)
        bidder_totals.append((supplier, total, is_winner))
        if is_winner and not parsed.awarded_contractor:
            parsed.awarded_contractor = supplier
            parsed.winning_bid_total = total

    if supplier_blocks:
        supplier_blocks.sort(key=lambda b: (not b[1], b[2]))
        any_block_awarded = any(b[1] for b in supplier_blocks)
        for rank, (supplier, is_awarded, total, lines) in enumerate(supplier_blocks, start=1):
            is_winner = is_awarded or (rank == 1 and not any_block_awarded)
            if is_winner:
                parsed.awarded_contractor = supplier
                parsed.winning_bid_total = total
            for line in lines:
                line.bid_rank = rank
                line.is_winner = is_winner
                parsed.lines.append(line)

    return parsed, bidder_totals


def parse_bidsync_tab_html(
    html: str,
    *,
    project_num: str = "",
    title: str = "",
    letting_date: str = "",
    source_url: str = "",
) -> ParsedProjectBidTab:
    """Parse an unauthenticated BidSync `awgroupreview&expandall=Y` HTML page."""
    parsed, _ = parse_bidsync_tab_with_totals(
        html,
        project_num=project_num,
        title=title,
        letting_date=letting_date,
        source_url=source_url,
    )
    return parsed


def _ingest_bidsync_totals_only(
    parsed: ParsedProjectBidTab,
    bidder_totals: list[tuple[str, float, bool]],
    db_path: str | Any,
) -> int:
    with db_session(db_path) as conn:
        winner_cid = None
        if parsed.awarded_contractor:
            winner_cid = upsert_contractor(conn, parsed.awarded_contractor)
        pid = upsert_project(
            conn,
            agency=parsed.agency,
            agency_project_num=parsed.agency_project_num,
            title=parsed.title,
            county=parsed.county,
            letting_date=parsed.letting_date or None,
            winning_bid_total=parsed.winning_bid_total if parsed.winning_bid_total else None,
            awarded_contractor_id=winner_cid,
            source_url=parsed.source_url,
        )
        if parsed.source_url:
            upsert_project_document(
                conn,
                project_id=pid,
                doc_type="bidsync_tab",
                title=f"Maricopa BidSync Bid Tabulation ({parsed.agency_project_num})",
                source_url=parsed.source_url,
            )
        recorded = 0
        for rank, (supplier, total, is_winner) in enumerate(bidder_totals, start=1):
            cid = upsert_contractor(conn, supplier)
            upsert_project_bid(
                conn,
                project_id=pid,
                contractor_id=cid,
                is_engineers_estimate=False,
                bid_total=total,
                bid_rank=rank,
                is_winner=is_winner,
            )
            recorded += 1
    return recorded


def run_maricopa_bidsync_sync(
    db_path: str | Any = None,
    *,
    limit: int = 15,
    query: str = DEFAULT_CIVIL_QUERY,
    max_ajax_pages: int = 100,
    dry_run: bool = False,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Discover Maricopa County / MCDOT civil bids in BidSync and ingest line items or lump-sum totals."""
    sess = session or requests.Session()
    listings = fetch_bidsync_listings(
        limit=limit,
        query=query,
        max_ajax_pages=max_ajax_pages,
        session=sess,
    )

    with db_session(db_path) as conn:
        existing_nums = {
            r[0]
            for r in conn.execute(
                """SELECT agency_project_num FROM projects
                   WHERE agency = 'MCDOT'
                     AND (
                       EXISTS (SELECT 1 FROM bid_lines bl WHERE bl.project_id = projects.project_id)
                       OR EXISTS (SELECT 1 FROM project_bids pb WHERE pb.project_id = projects.project_id)
                     )"""
            ).fetchall()
        }

    ingested = 0
    ingested_summary = 0
    skipped = 0
    total_lines = 0
    total_project_bids = 0
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
            parsed, bidder_totals = parse_bidsync_tab_with_totals(
                resp.text,
                project_num=item.project_num,
                title=item.title,
                letting_date=item.end_date,
                source_url=item.tab_url,
            )
            if parsed.lines:
                if not dry_run:
                    ingest_parsed_project(parsed, db_path, doc_type="bidsync_tab")
                existing_nums.add(parsed.agency_project_num)
                ingested += 1
                total_lines += len(parsed.lines)
            elif bidder_totals:
                if not dry_run:
                    cnt = _ingest_bidsync_totals_only(parsed, bidder_totals, db_path)
                else:
                    cnt = len(bidder_totals)
                existing_nums.add(parsed.agency_project_num)
                ingested += 1
                ingested_summary += 1
                total_project_bids += cnt
            else:
                skipped += 1
        except Exception as exc:
            errors.append(f"{item.project_num}: {exc}")

    return {
        "agency": "MCDOT",
        "discovered": len(listings),
        "ingested": ingested,
        "ingested_summary_only": ingested_summary,
        "skipped": skipped,
        "total_bid_lines": total_lines,
        "total_project_bids": total_project_bids,
        "errors": errors,
    }
