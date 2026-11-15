"""ADOT Bid Tabulations PDF parser & E2C2 Historical Unit Price Portal scraper."""

from __future__ import annotations

import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import pdfplumber
import requests
from bs4 import BeautifulSoup

from src.db import (
    DEFAULT_DB_PATH,
    db_session,
    insert_bid_line,
    upsert_contractor,
    upsert_pay_item,
    upsert_project,
)
from src.normalize import normalize_item_code, normalize_unit

ADOT_E2C2_URL = "https://apps.azdot.gov/e2c2/HistoricalPrice.aspx"
ADOT_BID_TABS_URL = "https://azdot.gov/business/contracts-and-specifications/bid-tabulations"

_MONEY_RE = re.compile(r"^\$?\s*([\d,]+\.\d{2,4})$")
_ITEM_LINE_RE = re.compile(
    r"^(\d{6,8}[A-Z]?)\s+(.+?)\s+(CU\.YD\.|CY|L\.FT\.|LF|L\.SUM|LS|TON|EACH|EA|SQ\.YD\.|SY|SQ\.FT\.|SF|HOUR|HR|GAL|ACRE|DAY|LB|F\.A\.)\s+([\d,]+(?:\.\d+)?)\s+\$?([\d,]+\.\d{2,4})\s+\$?([\d,]+\.\d{2})$",
    re.IGNORECASE,
)


@dataclass
class ParsedBidLine:
    item_code: str
    description: str
    unit: str
    quantity: float
    unit_price: float
    extended_total: float
    bidder_name: str
    is_engineers_estimate: bool = False
    bid_rank: int | None = None
    is_winner: bool = False


@dataclass
class ParsedProjectBidTab:
    agency: str
    agency_project_num: str
    title: str
    route_or_location: str = ""
    county: str = ""
    district: str = ""
    letting_date: str = ""
    award_date: str = ""
    engineers_estimate: float = 0.0
    winning_bid_total: float = 0.0
    awarded_contractor: str = ""
    source_url: str = ""
    spec_system: str = "ADOT"
    lines: list[ParsedBidLine] = field(default_factory=list)


def _to_float(val: str) -> float:
    cleaned = re.sub(r"[,$\s]", "", str(val or "0"))
    return float(cleaned) if cleaned else 0.0


def parse_adot_bid_tab_pdf(pdf_source: bytes | str | Path) -> ParsedProjectBidTab:
    """Parse an ADOT official bid tabulation PDF using pdfplumber.

    Supports both tabular bid-tab layouts and line-oriented ADOT Trns*Port/BAMS exports.
    """
    if isinstance(pdf_source, (str, Path)):
        pdf_ctx = pdfplumber.open(str(pdf_source))
    else:
        pdf_ctx = pdfplumber.open(io.BytesIO(pdf_source))

    all_text_lines: list[str] = []
    extracted_tables: list[list[list[str | None]]] = []

    with pdf_ctx as pdf:
        for page in pdf.pages:
            txt = page.extract_text() or ""
            all_text_lines.extend(line.strip() for line in txt.splitlines() if line.strip())
            tables = page.extract_tables() or []
            extracted_tables.extend(tables)

    full_text = "\n".join(all_text_lines)

    # Extract header metadata
    proj_match = re.search(
        r"(?:TRACS\s*(?:NO\.?|#)?|PROJECT\s*(?:NO\.?|#)?)\s*:?\s*([A-Z0-9\-]+)",
        full_text,
        re.IGNORECASE,
    )
    proj_num = proj_match.group(1) if proj_match else "ADOT-UNKNOWN"

    date_match = re.search(
        r"(?:LETTING\s*DATE|BID\s*OPENING)\s*:?\s*(\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4})",
        full_text,
        re.IGNORECASE,
    )
    letting_date = date_match.group(1) if date_match else ""

    county_match = re.search(r"COUNTY\s*:?\s*([A-Za-z\s]+?)(?:\n|DISTRICT|$)", full_text, re.IGNORECASE)
    county = county_match.group(1).strip().title() if county_match else ""

    route_match = re.search(r"(?:ROUTE|LOCATION)\s*:?\s*([^\n]+)", full_text, re.IGNORECASE)
    route = route_match.group(1).strip() if route_match else ""

    title_match = re.search(r"(?:PROJECT\s*NAME|DESCRIPTION|TITLE)\s*:?\s*([^\n]+)", full_text, re.IGNORECASE)
    title = title_match.group(1).strip() if title_match else f"ADOT Project {proj_num}"

    parsed = ParsedProjectBidTab(
        agency="ADOT",
        agency_project_num=proj_num,
        title=title,
        route_or_location=route,
        county=county,
        letting_date=letting_date,
        source_url=ADOT_BID_TABS_URL,
        spec_system="ADOT",
    )

    # Strategy 1: Parse structured tables if present
    for table in extracted_tables:
        if not table or len(table) < 2:
            continue
        header = [str(c or "").strip().upper() for c in table[0]]
        if any("ITEM" in h for h in header) and any("UNIT" in h for h in header):
            # Identify column indices
            idx_code = next((i for i, h in enumerate(header) if "ITEM" in h or "CODE" in h), 0)
            idx_desc = next((i for i, h in enumerate(header) if "DESC" in h), 1)
            idx_unit = next((i for i, h in enumerate(header) if "UNIT" in h and "PRICE" not in h), 2)
            idx_qty = next((i for i, h in enumerate(header) if "QTY" in h or "QUANT" in h), 3)
            # Remaining columns after qty are often pairs of (Unit Price, Extended) or bidder columns
            for row in table[1:]:
                if not row or len(row) <= max(idx_code, idx_desc, idx_unit, idx_qty):
                    continue
                code = normalize_item_code(str(row[idx_code] or ""))
                if not re.match(r"^\d{5,8}[A-Z]?$", code):
                    continue
                desc = str(row[idx_desc] or "").strip()
                unit = normalize_unit(str(row[idx_unit] or ""))
                try:
                    qty = _to_float(str(row[idx_qty] or "0"))
                except ValueError:
                    continue
                # Look for unit price + extended in remaining columns
                rem = [str(c or "").strip() for c in row[idx_qty + 1 :] if str(c or "").strip()]
                if len(rem) >= 2:
                    try:
                        up = _to_float(rem[0])
                        ext = _to_float(rem[1])
                        parsed.lines.append(
                            ParsedBidLine(
                                item_code=code,
                                description=desc,
                                unit=unit,
                                quantity=qty,
                                unit_price=up,
                                extended_total=ext,
                                bidder_name="LOW BIDDER",
                                bid_rank=1,
                                is_winner=True,
                            )
                        )
                    except ValueError:
                        continue

    # Strategy 2: Fallback line-by-line regex parsing
    if not parsed.lines:
        current_bidder = "LOW BIDDER"
        current_is_ee = False
        current_rank = 1
        for line in all_text_lines:
            if "ENGINEER'S ESTIMATE" in line.upper() or "ENGINEERS ESTIMATE" in line.upper():
                current_bidder = "ENGINEER'S ESTIMATE"
                current_is_ee = True
                current_rank = 0
                continue
            bidder_hdr = re.match(r"^BIDDER\s*#?(\d+)\s*:\s*(.+)$", line, re.IGNORECASE)
            if bidder_hdr:
                current_rank = int(bidder_hdr.group(1))
                current_bidder = bidder_hdr.group(2).strip()
                current_is_ee = False
                continue

            m = _ITEM_LINE_RE.match(line)
            if m:
                code, desc, unit, qty_s, up_s, ext_s = m.groups()
                parsed.lines.append(
                    ParsedBidLine(
                        item_code=normalize_item_code(code),
                        description=desc.strip(),
                        unit=normalize_unit(unit),
                        quantity=_to_float(qty_s),
                        unit_price=_to_float(up_s),
                        extended_total=_to_float(ext_s),
                        bidder_name=current_bidder,
                        is_engineers_estimate=current_is_ee,
                        bid_rank=None if current_is_ee else current_rank,
                        is_winner=(not current_is_ee and current_rank == 1),
                    )
                )

    return parsed


def scrape_adot_e2c2_historical(
    item_code: str | None = None,
    *,
    timeout: int = 20,
    session: requests.Session | None = None,
) -> list[dict[str, Any]]:
    """Query ADOT E2C2 Historical Unit Price Portal (apps.azdot.gov/e2c2/HistoricalPrice.aspx).

    Handles ASP.NET ViewState extraction and parses the historical price grid.
    """
    sess = session or requests.Session()
    sess.headers.update(
        {"User-Agent": "AZ-BidTabs/1.0 (+https://azbidtabs.example; heavy-civil-intel)"}
    )
    resp = sess.get(ADOT_E2C2_URL, timeout=timeout)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    if item_code:
        # Extract ASP.NET hidden form fields for postback search
        payload: dict[str, str] = {}
        for inp in soup.select("input[type='hidden']"):
            name = inp.get("name")
            if name:
                payload[name] = inp.get("value", "")

        # Common E2C2 item search input names
        for inp in soup.select("input[type='text']"):
            name = inp.get("name", "")
            if "item" in name.lower() or "code" in name.lower():
                payload[name] = normalize_item_code(item_code)
                break

        btn = soup.select_one("input[type='submit']")
        if btn and btn.get("name"):
            payload[btn["name"]] = btn.get("value", "Search")

        post_resp = sess.post(ADOT_E2C2_URL, data=payload, timeout=timeout)
        post_resp.raise_for_status()
        soup = BeautifulSoup(post_resp.text, "html.parser")

    records: list[dict[str, Any]] = []
    for table in soup.select("table"):
        headers = [th.get_text(strip=True).lower() for th in table.select("tr th")]
        if not headers:
            continue
        if any("item" in h for h in headers) and any("price" in h or "unit" in h for h in headers):
            for tr in table.select("tr")[1:]:
                cells = [td.get_text(strip=True) for td in tr.select("td")]
                if len(cells) >= 4:
                    records.append(dict(zip(headers, cells)))
    return records


def ingest_parsed_project(
    parsed: ParsedProjectBidTab,
    db_path: Path | str | None = None,
) -> int:
    """Persist a ParsedProjectBidTab into SQLite or Postgres."""
    with db_session(db_path) as conn:
        awarded_cid = (
            upsert_contractor(conn, parsed.awarded_contractor)
            if parsed.awarded_contractor
            else None
        )
        project_id = upsert_project(
            conn,
            agency=parsed.agency,
            agency_project_num=parsed.agency_project_num,
            title=parsed.title,
            route_or_location=parsed.route_or_location,
            county=parsed.county,
            district=parsed.district,
            letting_date=parsed.letting_date,
            award_date=parsed.award_date,
            engineers_estimate=parsed.engineers_estimate,
            winning_bid_total=parsed.winning_bid_total,
            awarded_contractor_id=awarded_cid,
            source_url=parsed.source_url,
        )
        for line in parsed.lines:
            cid = (
                None
                if line.is_engineers_estimate
                else upsert_contractor(conn, line.bidder_name)
            )
            item_id = upsert_pay_item(
                conn,
                spec_system=parsed.spec_system,
                item_code=line.item_code,
                description=line.description,
                unit=line.unit,
            )
            insert_bid_line(
                conn,
                project_id=project_id,
                contractor_id=cid,
                is_engineers_estimate=line.is_engineers_estimate,
                item_id=item_id,
                quantity=line.quantity,
                unit_price=line.unit_price,
                extended_total=line.extended_total,
                bid_rank=line.bid_rank,
                is_winner=line.is_winner,
            )
        return project_id


def discover_adot_bid_tab_pdfs(
    url: str = ADOT_BID_TABS_URL,
    *,
    limit: int = 50,
    timeout: int = 20,
    session: requests.Session | None = None,
) -> list[dict[str, str]]:
    """Discover active ADOT Bid Tabulation PDF links before ADOT's ~4-month expiry."""
    sess = session or requests.Session()
    sess.headers.update(
        {"User-Agent": "AZ-BidTabs/1.0 (+https://azbidtabs.example; heavy-civil-intel)"}
    )
    resp = sess.get(url, timeout=timeout)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    discovered: list[dict[str, str]] = []
    seen_urls: set[str] = set()
    for a in soup.select("a[href]"):
        href = str(a.get("href", "")).strip()
        if ".pdf" not in href.lower():
            continue
        full_url = urljoin(url, href)
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        link_text = a.get_text(" ", strip=True) or Path(href.split("?")[0]).stem
        tracs_match = re.search(r"\b([FH]\d{3,5}[0-9A-Z]{2,4})\b", f"{link_text} {href}", re.IGNORECASE)
        proj_num = tracs_match.group(1).upper() if tracs_match else Path(href.split("?")[0]).stem[:24]
        discovered.append(
            {
                "url": full_url,
                "title": link_text,
                "project_num": proj_num,
            }
        )
        if len(discovered) >= limit:
            break
    return discovered


def run_adot_archive_sync(
    db_path: Path | str | None = None,
    *,
    limit: int = 25,
    dry_run: bool = False,
    timeout: int = 25,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Discover, download, parse, and archive ADOT bid tab PDFs not yet in the database."""
    sess = session or requests.Session()
    links = discover_adot_bid_tab_pdfs(limit=limit, timeout=timeout, session=sess)

    with db_session(db_path) as conn:
        existing_nums = {
            r[0]
            for r in conn.execute(
                "SELECT agency_project_num FROM projects WHERE agency = 'ADOT'"
            ).fetchall()
        }

    ingested = 0
    skipped = 0
    errors: list[str] = []

    for item in links:
        if item["project_num"] in existing_nums:
            skipped += 1
            continue
        if dry_run:
            ingested += 1
            continue
        try:
            pdf_resp = sess.get(item["url"], timeout=timeout)
            pdf_resp.raise_for_status()
            parsed = parse_adot_bid_tab_pdf(pdf_resp.content)
            if parsed.agency_project_num == "ADOT-UNKNOWN":
                parsed.agency_project_num = item["project_num"]
            if not parsed.title or parsed.title.startswith("ADOT Project "):
                parsed.title = item["title"]
            parsed.source_url = item["url"]
            if parsed.lines:
                ingest_parsed_project(parsed, db_path)
                ingested += 1
            else:
                skipped += 1
        except Exception as exc:
            errors.append(f"{item['project_num']}: {exc}")

    return {
        "discovered": len(links),
        "ingested": ingested,
        "skipped": skipped,
        "errors": errors,
    }


_DISTRICT_TO_COUNTY = {
    "CENTRAL DISTRICT": "Maricopa",
    "SOUTHCENT DISTRICT": "Pima",
    "NORTHWEST DISTRICT": "Mohave",
    "NORTHCENT DISTRICT": "Coconino",
    "SOUTHWEST DISTRICT": "Yuma",
    "NORTHEAST DISTRICT": "Navajo",
    "SOUTHEAST DISTRICT": "Cochise",
}


def is_e2c2_csv(file_bytes: bytes) -> bool:
    """Return True if CSV bytes look like an ADOT E2C2 SSRS Bid History export."""
    head_text = file_bytes[:4096].decode("utf-8-sig", errors="ignore")
    return "Tracs_Number" in head_text and "Item_Number" in head_text and "Bid_Date" in head_text


def _parse_e2c2_float(val: Any) -> float:
    if val is None:
        return 0.0
    s = str(val).strip().replace(",", "").replace("$", "")
    if not s or s.lower() == "nan":
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def _parse_e2c2_date(val: Any) -> str:
    s = str(val or "").strip()
    m = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if m:
        mm, dd, yyyy = m.groups()
        return f"{yyyy}-{int(mm):02d}-{int(dd):02d}"
    return s


def ingest_e2c2_csv(
    file_bytes: bytes,
    db_path: Path | str | None = None,
) -> dict[str, int]:
    """Parse and batch-ingest an ADOT E2C2 Bid History CSV export into SQLite or Postgres."""
    import pandas as pd

    text = file_bytes.decode("utf-8-sig", errors="replace")
    lines = text.splitlines()
    header_idx = 0
    for i, line in enumerate(lines[:20]):
        if "Tracs_Number" in line and "Item_Number" in line:
            header_idx = i
            break

    df = pd.read_csv(io.StringIO("\n".join(lines[header_idx:])))
    df.columns = [str(c).strip() for c in df.columns]

    if df.empty:
        return {"projects": 0, "pay_items": 0, "bid_lines": 0}

    with db_session(db_path) as conn:
        # Pre-create/lookup placeholder bidders for E2C2 anonymous rank columns
        bidder_cids = {
            1: upsert_contractor(conn, "ADOT LOW BIDDER (RANK 1)"),
            2: upsert_contractor(conn, "ADOT BIDDER (RANK 2)"),
            3: upsert_contractor(conn, "ADOT BIDDER (RANK 3)"),
        }

        # Cache existing pay items
        item_cache: dict[tuple[str, str], int] = {
            (r[1], r[2]): int(r[0])
            for r in conn.execute(
                "SELECT item_id, item_code, unit FROM pay_items WHERE spec_system = 'ADOT'"
            ).fetchall()
        }

        # Group by Tracs_Number to compute project totals and upsert projects
        df["Tracs_Number"] = df["Tracs_Number"].astype(str).str.strip()
        project_ids: dict[str, int] = {}
        new_items = 0
        bid_line_rows: list[tuple[Any, ...]] = []

        for tracs, grp in df.groupby("Tracs_Number", sort=False):
            if not tracs or tracs.lower() == "nan":
                continue
            first = grp.iloc[0]
            dist = str(first.get("DistName", "") or "").strip()
            county = _DISTRICT_TO_COUNTY.get(dist.upper(), "")
            bid_date = _parse_e2c2_date(first.get("Bid_Date", ""))
            route = str(first.get("Route", "") or "").strip()
            title = str(first.get("projname", "") or "").strip() or f"ADOT {tracs}"

            ee_total = 0.0
            win_total = 0.0
            grp_rows: list[tuple[int, float, float, float, float, float]] = []

            for _, r in grp.iterrows():
                code = normalize_item_code(str(r.get("Item_Number", "")))
                if not code:
                    continue
                desc = str(r.get("Description", "") or code).strip()
                unit = normalize_unit(str(r.get("Measure", "EACH") or "EACH"))
                qty = _parse_e2c2_float(r.get("Quantity"))
                if qty <= 0:
                    continue

                key = (code, unit)
                if key not in item_cache:
                    item_cache[key] = upsert_pay_item(conn, "ADOT", code, desc, unit)
                    new_items += 1
                item_id = item_cache[key]

                p_ee = _parse_e2c2_float(r.get("State"))
                p_b1 = _parse_e2c2_float(r.get("Bidder_1"))
                p_b2 = _parse_e2c2_float(r.get("Bidder_2"))
                p_b3 = _parse_e2c2_float(r.get("Bidder_3"))

                ee_total += qty * p_ee
                win_total += qty * p_b1
                grp_rows.append((item_id, qty, p_ee, p_b1, p_b2, p_b3))

            if not grp_rows:
                continue

            pid = upsert_project(
                conn,
                agency="ADOT",
                agency_project_num=tracs,
                title=title,
                route_or_location=route,
                county=county or None,
                district=dist or None,
                letting_date=bid_date or None,
                engineers_estimate=round(ee_total, 2),
                winning_bid_total=round(win_total, 2),
                awarded_contractor_id=bidder_cids[1] if win_total > 0 else None,
                source_url=ADOT_E2C2_URL,
            )
            project_ids[tracs] = pid

            # Clear prior lines for this project on re-import to avoid duplicates
            conn.execute("DELETE FROM bid_lines WHERE project_id = ?", (pid,))

            for item_id, qty, p_ee, p_b1, p_b2, p_b3 in grp_rows:
                if p_ee > 0:
                    bid_line_rows.append(
                        (pid, None, 1, item_id, qty, p_ee, round(qty * p_ee, 2), None, 0)
                    )
                for rank, price in ((1, p_b1), (2, p_b2), (3, p_b3)):
                    if price > 0:
                        bid_line_rows.append(
                            (
                                pid,
                                bidder_cids[rank],
                                0,
                                item_id,
                                qty,
                                price,
                                round(qty * price, 2),
                                rank,
                                1 if rank == 1 else 0,
                            )
                        )

        if bid_line_rows:
            conn.executemany(
                """
                INSERT INTO bid_lines (
                    project_id, contractor_id, is_engineers_estimate, item_id,
                    quantity, unit_price, extended_total, bid_rank, is_winner
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                bid_line_rows,
            )

    return {
        "projects": len(project_ids),
        "pay_items": new_items,
        "bid_lines": len(bid_line_rows),
    }


