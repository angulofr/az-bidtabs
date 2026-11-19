"""Historical ADOT archive scrapers.

Covers three verified public ADOT sources beyond the active ~4-month tabulation window:
1. Wayback CDX snapshots of `cnsads.azdot.gov/tabulations` -> live AWS S3 itemized Bid Tab PDFs.
2. Arizona State Transportation Board (`aztransportationboard.gov/meeting-documents`) Awards PDFs (2010-2026).
3. ADOT CNSADS As-Read bid results (`cnsads.azdot.gov/as-read/details/<YYYYNNN>`, 2001-2026).
"""

from __future__ import annotations

import io
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urljoin

import pdfplumber
import requests
from bs4 import BeautifulSoup

from src.db import (
    db_session,
    upsert_contractor,
    upsert_project,
    upsert_project_bid,
)
from src.scrapers.adot import (
    ADOT_CNSADS_TABULATIONS_URL,
    TRACS_RE,
    _extract_pdf_links_from_html,
    get_itemized_adot_project_nums,
    ingest_parsed_project,
    parse_adot_bid_tab_pdf,
)

WAYBACK_CDX_URL = "https://web.archive.org/cdx/search/cdx"
STB_MEETING_DOCS_URL = "https://aztransportationboard.gov/meeting-documents"
ADOT_AS_READ_INDEX_URL = "https://cnsads.azdot.gov/as-read"
ADOT_AS_READ_DETAIL_URL = "https://cnsads.azdot.gov/as-read/details/{detail_id}"

_EXTENDED_TRACS_RE = re.compile(
    r"\b([FHTS][0-9]{3,5}[0-9A-Z]{2,4}|[A-Z]{2}[0-9]{3,4}[0-9A-Z]{2,3})\b"
)
_LEGACY_S3_HOST_FRAGMENT = "adot-cmss3bp00001"
_AWARD_AMOUNT_RE = re.compile(r"\$\s*([\d,]+\.\d{2})")
_MONEY_VAL_RE = re.compile(r"^\$?\s*([\d,]+\.\d{2})$")
_DATE_MDY_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")

_ADOT_COUNTY_CODES: dict[str, str] = {
    "AP": "Apache",
    "CH": "Cochise",
    "CN": "Coconino",
    "GI": "Gila",
    "GH": "Graham",
    "GN": "Greenlee",
    "LP": "La Paz",
    "MA": "Maricopa",
    "MO": "Mohave",
    "NA": "Navajo",
    "PM": "Pima",
    "PN": "Pinal",
    "SC": "Santa Cruz",
    "YV": "Yavapai",
    "YU": "Yuma",
    "SW": "Statewide",
}

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _parse_money(text: str) -> float | None:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    m = _MONEY_VAL_RE.match(cleaned)
    if not m:
        m = _AWARD_AMOUNT_RE.search(cleaned)
    if not m:
        return None
    try:
        return float(m.group(1).replace(",", ""))
    except ValueError:
        return None


def _parse_mdy_date(text: str) -> str:
    m = _DATE_MDY_RE.search(text or "")
    if not m:
        return ""
    try:
        month, day, year = int(m.group(1)), int(m.group(2)), int(m.group(3))
        return datetime(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _county_from_route(route_str: str) -> str | None:
    tokens = re.findall(r"\b([A-Z]{2})\b", (route_str or "").upper())
    for tok in tokens:
        if tok in _ADOT_COUNTY_CODES:
            return _ADOT_COUNTY_CODES[tok]
    return None


def _extract_tracs(text: str) -> str | None:
    m = TRACS_RE.search((text or "").upper())
    if m:
        return m.group(1)
    m2 = _EXTENDED_TRACS_RE.search((text or "").upper())
    return m2.group(1) if m2 else None


def _is_valid_session(session: requests.Session | None) -> requests.Session:
    sess = session or requests.Session()
    sess.headers.update(_BROWSER_HEADERS)
    return sess


# ---------------------------------------------------------------------------
# 1. Wayback CDX -> live AWS S3 itemized Bid Tab PDFs
# ---------------------------------------------------------------------------


def discover_wayback_tabulation_pdfs(
    *,
    session: requests.Session | None = None,
    max_snapshots: int = 80,
    delay_seconds: float = 3.5,
    timeout: int = 25,
) -> list[dict[str, str]]:
    """Query Wayback CDX for archived `cnsads.azdot.gov/tabulations` HTML pages and collect live S3 PDF URLs."""
    sess = _is_valid_session(session)
    cdx_params = {
        "url": ADOT_CNSADS_TABULATIONS_URL,
        "output": "json",
        "fl": "timestamp,original,statuscode,digest",
        "filter": "statuscode:200",
        "collapse": "digest",
        "limit": str(max_snapshots),
    }
    resp = sess.get(WAYBACK_CDX_URL, params=cdx_params, timeout=timeout)
    resp.raise_for_status()
    rows = resp.json()
    if not isinstance(rows, list) or len(rows) <= 1:
        return []

    snapshots: list[tuple[str, str]] = []
    for row in rows[1:]:
        if len(row) >= 2:
            snapshots.append((str(row[0]), str(row[1])))

    discovered: list[dict[str, str]] = []
    seen_pdf_urls: set[str] = set()

    for idx, (ts, orig_url) in enumerate(snapshots[:max_snapshots]):
        if idx > 0 and delay_seconds > 0:
            time.sleep(delay_seconds)
        wb_url = f"https://web.archive.org/web/{ts}id_/{orig_url}"
        html = ""
        for attempt in range(2):
            try:
                snap_resp = sess.get(wb_url, timeout=timeout)
                if snap_resp.status_code == 429:
                    time.sleep(max(delay_seconds * 2, 5.0))
                    continue
                if snap_resp.ok:
                    html = snap_resp.text
                    break
            except requests.RequestException:
                if attempt == 0 and delay_seconds > 0:
                    time.sleep(delay_seconds)
        if not html:
            continue

        for entry in _extract_pdf_links_from_html(html, orig_url):
            pdf_url = entry["url"]
            # Strip any accidental wayback proxy prefix if present so we hit live S3 directly
            wb_inner = re.search(r"https://web\.archive\.org/web/\d+(?:id_)?/(https?://.+)", pdf_url)
            if wb_inner:
                pdf_url = wb_inner.group(1)
            if _LEGACY_S3_HOST_FRAGMENT in pdf_url.lower():
                continue
            if pdf_url in seen_pdf_urls:
                continue
            seen_pdf_urls.add(pdf_url)
            discovered.append(
                {
                    "pdf_url": pdf_url,
                    "title": entry.get("title", ""),
                    "project_num": entry.get("project_num", ""),
                    "snapshot_timestamp": ts,
                }
            )

    return discovered


def sync_adot_wayback_pdfs(
    db_path: Path | str | None = None,
    *,
    limit: int = 200,
    max_snapshots: int = 60,
    delay_seconds: float = 3.5,
    dry_run: bool = False,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Recover historical itemized ADOT Bid Tab PDFs archived on AWS S3 via Wayback CDX."""
    sess = _is_valid_session(session)
    candidates = discover_wayback_tabulation_pdfs(
        session=sess,
        max_snapshots=max_snapshots,
        delay_seconds=delay_seconds,
    )
    existing_itemized = get_itemized_adot_project_nums(db_path)

    ingested = 0
    skipped = 0
    total_lines = 0
    errors: list[str] = []

    for item in candidates[:limit]:
        proj_hint = item.get("project_num") or ""
        if proj_hint and proj_hint in existing_itemized:
            skipped += 1
            continue
        pdf_url = item["pdf_url"]
        try:
            pdf_resp = sess.get(pdf_url, timeout=30)
            if not pdf_resp.ok:
                skipped += 1
                continue
            parsed = parse_adot_bid_tab_pdf(pdf_resp.content, filename=pdf_url.split("/")[-1])
            parsed.source_url = pdf_url
            if (
                (not parsed.agency_project_num or parsed.agency_project_num == "ADOT-UNKNOWN")
                and proj_hint
            ):
                parsed.agency_project_num = proj_hint
            if parsed.agency_project_num in existing_itemized or not parsed.lines:
                skipped += 1
                continue
            if not dry_run:
                ingest_parsed_project(parsed, db_path)
            existing_itemized.add(parsed.agency_project_num)
            ingested += 1
            total_lines += len(parsed.lines)
        except Exception as exc:
            errors.append(f"{pdf_url}: {exc}")

    return {
        "agency": "ADOT",
        "source": "adot_wayback_s3",
        "discovered": len(candidates),
        "ingested": ingested,
        "skipped": skipped,
        "total_bid_lines": total_lines,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# 2. Arizona State Transportation Board Awards PDFs (2010-2026)
# ---------------------------------------------------------------------------


@dataclass
class StbAwardRecord:
    tracs_num: str
    federal_project_num: str = ""
    description: str = ""
    low_bidder: str = ""
    award_amount: float | None = None
    action: str = "AWARD"
    meeting_date: str = ""
    county: str | None = None
    route_or_location: str = ""
    source_url: str = ""


def parse_stb_awards_pdf(
    pdf_source: bytes | str | Path,
    *,
    meeting_date: str = "",
    source_url: str = "",
) -> list[StbAwardRecord]:
    """Parse an Arizona State Transportation Board `Awards-*.pdf` into structured award records."""
    if isinstance(pdf_source, (bytes, bytearray)):
        pdf_ctx = pdfplumber.open(io.BytesIO(bytes(pdf_source)))
    else:
        pdf_ctx = pdfplumber.open(str(pdf_source))

    records: list[StbAwardRecord] = []
    seen_keys: set[tuple[str, str]] = set()

    with pdf_ctx as pdf:
        for page in pdf.pages:
            tables = page.extract_tables() or []
            for table in tables:
                for row in table:
                    if not row or len(row) < 3:
                        continue
                    cells = [re.sub(r"\s+", " ", (c or "").strip()) for c in row]
                    joined = " ".join(cells).upper()
                    if "DESCRIPTION OF WORK" in joined and "LOW BIDDER" in joined:
                        continue

                    desc = cells[0] if len(cells) >= 1 else ""
                    tracs_cell = cells[1] if len(cells) >= 2 else ""
                    bidder_cell = cells[2] if len(cells) >= 3 else ""
                    action_cell = cells[3] if len(cells) >= 4 else " ".join(cells[2:])

                    tracs = _extract_tracs(tracs_cell) or _extract_tracs(desc)
                    if not tracs:
                        continue

                    action_upper = action_cell.upper()
                    if any(
                        skip_kw in action_upper
                        for skip_kw in ("REJECT", "DEFER", "POSTPONE", "WITHDRAW", "NO AWARD")
                    ):
                        continue

                    amount = _parse_money(action_cell)
                    if amount is None and len(cells) >= 3:
                        amount = _parse_money(bidder_cell)
                    if amount is None:
                        continue

                    low_bidder = re.sub(r"\s+", " ", bidder_cell).strip()
                    if not low_bidder or low_bidder.upper().startswith("AWARD"):
                        continue

                    # Extract route/county prefix from tracs_cell before the TRACS token
                    prefix = tracs_cell.split(tracs)[0].strip()
                    county = _county_from_route(prefix)
                    if not county:
                        m_co = re.search(r"\b([A-Za-z ]+?)\s+County\b", desc, re.IGNORECASE)
                        if m_co:
                            county = m_co.group(1).strip().title()

                    fed_lines = [ln.strip() for ln in (row[1] or "").splitlines() if tracs not in ln.upper()]
                    fed_num = fed_lines[-1] if fed_lines else ""

                    key = (tracs, low_bidder.upper())
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)

                    records.append(
                        StbAwardRecord(
                            tracs_num=tracs,
                            federal_project_num=fed_num,
                            description=desc[:600],
                            low_bidder=low_bidder,
                            award_amount=amount,
                            action="AWARD",
                            meeting_date=meeting_date,
                            county=county,
                            route_or_location=prefix[:120],
                            source_url=source_url,
                        )
                    )

    return records


def _extract_stb_meeting_date(pdf_url: str, page_text: str = "") -> str:
    m_fn = re.search(r"Awards[_-](\d{4})(\d{2})(\d{2})", pdf_url, re.IGNORECASE)
    if m_fn:
        try:
            return datetime(int(m_fn.group(1)), int(m_fn.group(2)), int(m_fn.group(3))).strftime(
                "%Y-%m-%d"
            )
        except ValueError:
            pass
    m_txt = re.search(
        r"(January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+\d{1,2},\s+\d{4}",
        page_text,
        re.IGNORECASE,
    )
    if m_txt:
        try:
            return datetime.strptime(m_txt.group(0), "%B %d, %Y").strftime("%Y-%m-%d")
        except ValueError:
            pass
    return _parse_mdy_date(page_text)


def discover_stb_award_pdfs(
    *,
    max_pages: int = 18,
    session: requests.Session | None = None,
    timeout: int = 25,
) -> list[dict[str, str]]:
    """Crawl `aztransportationboard.gov/meeting-documents` pages to collect `Awards-*.pdf` links."""
    sess = _is_valid_session(session)
    award_page_urls: list[str] = []
    seen_pages: set[str] = set()

    for page_idx in range(max_pages):
        url = STB_MEETING_DOCS_URL if page_idx == 0 else f"{STB_MEETING_DOCS_URL}?page={page_idx}"
        resp = sess.get(url, timeout=timeout)
        if not resp.ok:
            break
        soup = BeautifulSoup(resp.text, "html.parser")
        new_on_page = 0
        for a in soup.select("a[href*='meeting-awards']"):
            href = urljoin(STB_MEETING_DOCS_URL, str(a.get("href") or ""))
            if href not in seen_pages:
                seen_pages.add(href)
                award_page_urls.append(href)
                new_on_page += 1
        # Also catch any direct Awards PDFs linked on the index itself
        for a in soup.select("a[href$='.pdf'], a[href*='.pdf?']"):
            href = str(a.get("href") or "")
            if "award" in href.lower() and href not in seen_pages:
                full_pdf = urljoin(STB_MEETING_DOCS_URL, href)
                seen_pages.add(full_pdf)
                award_page_urls.append(full_pdf)
                new_on_page += 1
        if new_on_page == 0:
            break

    pdf_items: list[dict[str, str]] = []
    seen_pdfs: set[str] = set()

    for page_url in award_page_urls:
        if page_url.lower().endswith(".pdf"):
            if page_url not in seen_pdfs:
                seen_pdfs.add(page_url)
                pdf_items.append(
                    {
                        "pdf_url": page_url,
                        "meeting_date": _extract_stb_meeting_date(page_url),
                    }
                )
            continue
        try:
            r = sess.get(page_url, timeout=timeout)
            if not r.ok:
                continue
            soup = BeautifulSoup(r.text, "html.parser")
            page_text = soup.get_text(" ", strip=True)
            for a in soup.select("a[href*='.pdf']"):
                href = urljoin(page_url, str(a.get("href") or ""))
                if "award" not in href.lower() and "award" not in (a.get_text() or "").lower():
                    continue
                if href in seen_pdfs:
                    continue
                seen_pdfs.add(href)
                pdf_items.append(
                    {
                        "pdf_url": href,
                        "meeting_date": _extract_stb_meeting_date(href, page_text),
                    }
                )
        except requests.RequestException:
            continue

    return pdf_items


def sync_adot_stb_awards(
    db_path: Path | str | None = None,
    *,
    max_pages: int = 18,
    limit: int = 250,
    dry_run: bool = False,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Ingest official ADOT State Transportation Board Contract Awards PDFs (2010-2026)."""
    sess = _is_valid_session(session)
    pdfs = discover_stb_award_pdfs(max_pages=max_pages, session=sess)

    with db_session(db_path) as conn:
        seen_sources = {
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT source_url FROM projects WHERE agency = 'ADOT' AND source_url LIKE ?",
                ("%Awards%",),
            ).fetchall()
            if r[0]
        }

    ingested_projects = 0
    ingested_bids = 0
    pdfs_processed = 0
    skipped = 0
    errors: list[str] = []

    for item in pdfs[:limit]:
        pdf_url = item["pdf_url"]
        if pdf_url in seen_sources:
            skipped += 1
            continue
        try:
            resp = sess.get(pdf_url, timeout=30)
            if not resp.ok:
                skipped += 1
                continue
            records = parse_stb_awards_pdf(
                resp.content,
                meeting_date=item.get("meeting_date", ""),
                source_url=pdf_url,
            )
            pdfs_processed += 1
            if not records:
                continue
            if not dry_run:
                with db_session(db_path) as conn:
                    for rec in records:
                        cid = upsert_contractor(conn, rec.low_bidder)
                        title = rec.description[:140] or f"ADOT Project {rec.tracs_num}"
                        pid = upsert_project(
                            conn,
                            agency="ADOT",
                            agency_project_num=rec.tracs_num,
                            title=title,
                            route_or_location=rec.route_or_location or None,
                            county=rec.county,
                            award_date=rec.meeting_date or None,
                            winning_bid_total=rec.award_amount,
                            awarded_contractor_id=cid,
                            source_url=rec.source_url,
                        )
                        if rec.award_amount is not None:
                            upsert_project_bid(
                                conn,
                                project_id=pid,
                                contractor_id=cid,
                                is_engineers_estimate=False,
                                bid_total=rec.award_amount,
                                bid_rank=1,
                                is_winner=True,
                            )
                            ingested_bids += 1
                        # Upgrade anonymous E2C2 placeholder award if present
                        conn.execute(
                            """UPDATE projects
                               SET awarded_contractor_id = ?,
                                   award_date = COALESCE(award_date, ?),
                                   winning_bid_total = COALESCE(winning_bid_total, ?)
                               WHERE project_id = ?
                                 AND (
                                   awarded_contractor_id IS NULL
                                   OR awarded_contractor_id IN (
                                     SELECT contractor_id FROM contractors
                                     WHERE canonical_name LIKE ?
                                   )
                                 )""",
                            (cid, rec.meeting_date or None, rec.award_amount, pid, "ADOT LOW BIDDER%"),
                        )
                        ingested_projects += 1
            else:
                ingested_projects += len(records)
                ingested_bids += len(records)
        except Exception as exc:
            errors.append(f"{pdf_url}: {exc}")

    return {
        "agency": "ADOT",
        "source": "adot_stb_awards",
        "pdfs_discovered": len(pdfs),
        "pdfs_processed": pdfs_processed,
        "ingested_projects": ingested_projects,
        "ingested_project_bids": ingested_bids,
        "skipped": skipped,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# 3. ADOT CNSADS As-Read Historical Bid Results (2001-2026)
# ---------------------------------------------------------------------------


@dataclass
class AsReadBidder:
    contractor_name: str
    bid_amount: float
    address: str = ""
    is_department_estimate: bool = False


@dataclass
class ParsedAsReadProject:
    detail_id: str
    tracs_num: str
    federal_project_num: str = ""
    title: str = ""
    route_or_location: str = ""
    county: str | None = None
    district: str | None = None
    letting_date: str = ""
    source_url: str = ""
    engineers_estimate: float | None = None
    bidders: list[AsReadBidder] = field(default_factory=list)


def parse_as_read_detail_html(
    html: str,
    *,
    detail_id: str = "",
    source_url: str = "",
) -> ParsedAsReadProject | None:
    """Parse an ADOT `/as-read/details/<YYYYNNN>` HTML page into department estimate + bidder totals."""
    if not html or "Application Error Occurred" in html:
        return None
    soup = BeautifulSoup(html, "html.parser")

    table = None
    for tbl in soup.find_all("table"):
        headers = [th.get_text(" ", strip=True).lower() for th in tbl.find_all("th")]
        if "bid amount" in headers and "contractor name" in headers:
            table = tbl
            break
    if table is None:
        return None

    bidders: list[AsReadBidder] = []
    engineers_estimate: float | None = None

    for tr in table.find_all("tr"):
        tds = [td.get_text(" ", strip=True) for td in tr.find_all("td")]
        if len(tds) < 2:
            continue
        amt = _parse_money(tds[0])
        name = re.sub(r"\s+", " ", tds[1]).strip()
        addr = re.sub(r"\s+", " ", tds[2]).strip() if len(tds) >= 3 else ""
        if amt is None or not name:
            continue
        is_dept = name.upper() == "DEPARTMENT" or name.upper().startswith("DEPARTMENT ")
        if is_dept:
            engineers_estimate = amt
        bidders.append(
            AsReadBidder(
                contractor_name=name,
                bid_amount=amt,
                address=addr,
                is_department_estimate=is_dept,
            )
        )

    if not bidders:
        return None

    body_text = soup.body.get_text("\n", strip=True) if soup.body else soup.get_text("\n", strip=True)
    lines = [ln.strip() for ln in body_text.splitlines() if ln.strip()]

    headline = ""
    district: str | None = None
    letting_date = ""
    for i, ln in enumerate(lines):
        if "As-Read Bid Results" in ln and i + 1 < len(lines):
            headline = lines[i + 1]
            break

    for ln in lines:
        if "District" in ln and len(ln) < 60:
            district = ln
        if ln.startswith("Bid Opening:"):
            letting_date = _parse_mdy_date(ln)

    tracs = _extract_tracs(headline) or _extract_tracs(body_text[:1500]) or f"ASREAD-{detail_id}"
    route_or_location = ""
    fed_num = ""
    title = ""
    if tracs and tracs in headline:
        before, after = headline.split(tracs, 1)
        route_or_location = before.strip()
        after_parts = after.strip().split(None, 1)
        if after_parts:
            fed_num = after_parts[0]
            title = after_parts[1].strip() if len(after_parts) > 1 else ""
    title = title or headline[:160] or f"ADOT Project {tracs}"
    county = _county_from_route(route_or_location or headline)

    return ParsedAsReadProject(
        detail_id=detail_id,
        tracs_num=tracs,
        federal_project_num=fed_num,
        title=title[:220],
        route_or_location=route_or_location[:120],
        county=county,
        district=district,
        letting_date=letting_date,
        source_url=source_url,
        engineers_estimate=engineers_estimate,
        bidders=bidders,
    )


def _ingest_as_read_project(parsed: ParsedAsReadProject, conn: Any) -> int:
    real_bidders = sorted(
        [b for b in parsed.bidders if not b.is_department_estimate],
        key=lambda b: b.bid_amount,
    )
    winner = real_bidders[0] if real_bidders else None
    winner_cid = upsert_contractor(conn, winner.contractor_name) if winner else None

    pid = upsert_project(
        conn,
        agency="ADOT",
        agency_project_num=parsed.tracs_num,
        title=parsed.title,
        route_or_location=parsed.route_or_location or None,
        county=parsed.county,
        district=parsed.district,
        letting_date=parsed.letting_date or None,
        engineers_estimate=parsed.engineers_estimate,
        winning_bid_total=winner.bid_amount if winner else None,
        awarded_contractor_id=winner_cid,
        source_url=parsed.source_url,
    )

    if parsed.engineers_estimate is not None:
        upsert_project_bid(
            conn,
            project_id=pid,
            contractor_id=None,
            is_engineers_estimate=True,
            bid_total=parsed.engineers_estimate,
            bid_rank=None,
            is_winner=False,
        )

    for rank, bidder in enumerate(real_bidders, start=1):
        cid = upsert_contractor(conn, bidder.contractor_name)
        upsert_project_bid(
            conn,
            project_id=pid,
            contractor_id=cid,
            is_engineers_estimate=False,
            bid_total=bidder.bid_amount,
            bid_rank=rank,
            is_winner=(rank == 1),
        )

    return len(real_bidders) + (1 if parsed.engineers_estimate is not None else 0)


def sync_adot_as_read_history(
    db_path: Path | str | None = None,
    *,
    start_year: int = 2001,
    end_year: int = 2026,
    max_seq_per_year: int = 250,
    max_consecutive_misses: int = 8,
    limit: int = 2500,
    dry_run: bool = False,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Crawl ADOT `/as-read` and `/as-read/details/<YYYYNNN>` across years into `projects` & `project_bids`."""
    sess = _is_valid_session(session)

    with db_session(db_path) as conn:
        existing_detail_urls = {
            r[0]
            for r in conn.execute(
                "SELECT source_url FROM projects WHERE agency = 'ADOT' AND source_url LIKE ?",
                ("%/as-read/details/%",),
            ).fetchall()
            if r[0]
        }

    candidate_ids: list[str] = []
    seen_ids: set[str] = set()

    # Seed from current /as-read index page first (recent items)
    try:
        idx_resp = sess.get(ADOT_AS_READ_INDEX_URL, timeout=20)
        if idx_resp.ok:
            idx_soup = BeautifulSoup(idx_resp.text, "html.parser")
            for a in idx_soup.select("a[href*='/as-read/details/']"):
                m = re.search(r"/as-read/details/(\d+)", str(a.get("href") or ""))
                if m and m.group(1) not in seen_ids:
                    seen_ids.add(m.group(1))
                    candidate_ids.append(m.group(1))
    except requests.RequestException:
        pass

    ingested_projects = 0
    ingested_bids = 0
    skipped = 0
    checked = 0
    errors: list[str] = []

    def _process_id(did: str) -> bool:
        """Fetch & ingest one detail_id. Returns True if valid page existed, False if missing/error."""
        nonlocal ingested_projects, ingested_bids, skipped, checked
        url = ADOT_AS_READ_DETAIL_URL.format(detail_id=did)
        if url in existing_detail_urls:
            skipped += 1
            return True
        checked += 1
        try:
            resp = sess.get(url, timeout=20)
            if not resp.ok:
                return False
            parsed = parse_as_read_detail_html(resp.text, detail_id=did, source_url=url)
            if parsed is None:
                return False
            if not dry_run:
                with db_session(db_path) as conn:
                    cnt = _ingest_as_read_project(parsed, conn)
                ingested_bids += cnt
            else:
                ingested_bids += len(parsed.bidders)
            existing_detail_urls.add(url)
            ingested_projects += 1
            return True
        except Exception as exc:
            errors.append(f"{did}: {exc}")
            return False

    for did in candidate_ids:
        if checked >= limit:
            break
        _process_id(did)

    # Sequential sweep by year from newest to oldest
    for year in range(end_year, start_year - 1, -1):
        if checked >= limit:
            break
        consecutive_misses = 0
        for seq in range(1, max_seq_per_year + 1):
            if checked >= limit:
                break
            did = f"{year}{seq:03d}"
            if did in seen_ids:
                consecutive_misses = 0
                continue
            seen_ids.add(did)
            found = _process_id(did)
            if found:
                consecutive_misses = 0
            else:
                consecutive_misses += 1
                if consecutive_misses >= max_consecutive_misses:
                    break

    return {
        "agency": "ADOT",
        "source": "adot_as_read_history",
        "checked": checked,
        "ingested_projects": ingested_projects,
        "ingested_project_bids": ingested_bids,
        "skipped": skipped,
        "errors": errors,
    }
