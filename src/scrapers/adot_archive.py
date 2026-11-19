"""Historical ADOT archive scrapers.

Covers three verified public ADOT sources beyond the active ~4-month tabulation window:
1. Wayback CDX snapshots of `cnsads.azdot.gov/tabulations` + direct Wayback CDX S3 bucket
   discovery (`azdotproductiondefault` live S3 + legacy `adot-cmss3bp00001` via Wayback).
2. Arizona State Transportation Board (`aztransportationboard.gov/meeting-documents`) Awards PDFs (2010-2026),
   supporting 4-to-11 column multi-row tables and 2010 borderless "OFFICE MEMO" PDFs.
3. ADOT CNSADS As-Read bid results (`cnsads.azdot.gov/as-read/details/<YYYYNNN>`, 2000-2026).
"""

from __future__ import annotations

import io
import re
import time
from concurrent.futures import ThreadPoolExecutor
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
    upsert_project_document,
)
from src.pdf_archive import save_pdf_artifact
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

ADOT_LIVE_S3_CDX_PATTERN = "s3-us-west-2.amazonaws.com/azdotproductiondefault-adotcloud-prod-s3-files/*"
ADOT_LEGACY_S3_CDX_PATTERN = "s3-us-west-2.amazonaws.com/adot-cmss3bp00001/*"

_EXTENDED_TRACS_RE = re.compile(
    r"\b([FHTS][0-9]{3,5}[0-9A-Z]{2,4}|(?:SL|SS|SZ|SX|SF|SR|HX)[0-9]{3,5}[0-9A-Z]{2,4})\b"
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


def _extract_all_tracs(text: str) -> list[str]:
    upper = (text or "").upper()
    seen: list[str] = []
    for regex in (TRACS_RE, _EXTENDED_TRACS_RE):
        for tok in regex.findall(upper):
            if tok not in seen:
                seen.append(tok)
    return seen


def _extract_tracs(text: str) -> str | None:
    hits = _extract_all_tracs(text)
    return hits[0] if hits else None


def _is_valid_session(session: requests.Session | None) -> requests.Session:
    sess = session or requests.Session()
    sess.headers.update(_BROWSER_HEADERS)
    return sess


# ---------------------------------------------------------------------------
# 1. Wayback CDX -> live & archived AWS S3 itemized Bid Tab PDFs
# ---------------------------------------------------------------------------


def _canonical_s3_pdf_url(raw_url: str) -> str:
    clean = raw_url.split("?")[0].strip()
    m = re.match(
        r"^https?://s3[^/]*\.amazonaws\.com/([^/]+)/(.+)$",
        clean,
        re.IGNORECASE,
    )
    if m:
        bucket, key = m.group(1), m.group(2)
        return f"https://{bucket}.s3.us-west-2.amazonaws.com/{key}"
    return clean


def discover_wayback_s3_cdx_pdfs(
    *,
    session: requests.Session | None = None,
    include_legacy_wayback: bool = True,
    timeout: int = 25,
) -> list[dict[str, str]]:
    """Query Wayback CDX directly for ADOT S3 PDF captures (`azdotproductiondefault` & `adot-cmss3bp00001`)."""
    sess = _is_valid_session(session)
    patterns: list[tuple[str, bool]] = [(ADOT_LIVE_S3_CDX_PATTERN, False)]
    if include_legacy_wayback:
        patterns.append((ADOT_LEGACY_S3_CDX_PATTERN, True))

    out: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    for url_pattern, is_legacy in patterns:
        try:
            resp = sess.get(
                WAYBACK_CDX_URL,
                params={
                    "url": url_pattern,
                    "output": "json",
                    "fl": "timestamp,original,statuscode",
                    "filter": "statuscode:200",
                    "collapse": "urlkey",
                    "limit": "1000",
                },
                timeout=timeout,
            )
            if not resp.ok:
                continue
            rows = resp.json()
        except Exception:
            continue

        if not isinstance(rows, list) or len(rows) <= 1:
            continue

        for row in rows[1:]:
            if len(row) < 2:
                continue
            ts = str(row[0])
            orig = str(row[1])
            lower = orig.lower()
            if ".pdf" not in lower:
                continue
            # Skip non-tabulation specifications/notices when filename is clearly not a bid/tab
            if any(
                skip in lower
                for skip in ("/specification", "/special-provision", "addendum", "q&a", "qa_")
            ):
                continue

            if is_legacy:
                dl_url = f"https://web.archive.org/web/{ts}id_/{orig}"
                key_url = dl_url
            else:
                dl_url = _canonical_s3_pdf_url(orig)
                key_url = dl_url

            if key_url in seen_urls:
                continue
            seen_urls.add(key_url)
            tracs_hint = _extract_tracs(orig) or ""
            out.append(
                {
                    "pdf_url": dl_url,
                    "source_url": dl_url if is_legacy else dl_url,
                    "title": Path(orig.split("?")[0]).stem,
                    "project_num": tracs_hint,
                    "snapshot_timestamp": ts,
                    "is_wayback_pdf": "1" if is_legacy else "0",
                }
            )

    return out


def discover_wayback_tabulation_pdfs(
    *,
    session: requests.Session | None = None,
    max_snapshots: int = 80,
    delay_seconds: float = 3.5,
    include_cdx_s3: bool = True,
    include_legacy_wayback: bool = True,
    timeout: int = 25,
) -> list[dict[str, str]]:
    """Collect historical ADOT Bid Tab PDFs from `/tabulations` Wayback snapshots and direct S3 CDX."""
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

    discovered: list[dict[str, str]] = []
    seen_pdf_urls: set[str] = set()

    if isinstance(rows, list) and len(rows) > 1:
        snapshots: list[tuple[str, str]] = [
            (str(row[0]), str(row[1])) for row in rows[1:] if len(row) >= 2
        ]
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
                wb_inner = re.search(
                    r"https://web\.archive\.org/web/\d+(?:id_)?/(https?://.+)", pdf_url
                )
                if wb_inner:
                    pdf_url = wb_inner.group(1)
                if _LEGACY_S3_HOST_FRAGMENT in pdf_url.lower():
                    continue
                canon = _canonical_s3_pdf_url(pdf_url)
                if canon in seen_pdf_urls:
                    continue
                seen_pdf_urls.add(canon)
                discovered.append(
                    {
                        "pdf_url": canon,
                        "source_url": canon,
                        "title": entry.get("title", ""),
                        "project_num": entry.get("project_num", ""),
                        "snapshot_timestamp": ts,
                        "is_wayback_pdf": "0",
                    }
                )

    if include_cdx_s3:
        for item in discover_wayback_s3_cdx_pdfs(
            session=sess,
            include_legacy_wayback=include_legacy_wayback,
            timeout=timeout,
        ):
            pdf_url = item["pdf_url"]
            if pdf_url in seen_pdf_urls:
                continue
            seen_pdf_urls.add(pdf_url)
            discovered.append(item)

    return discovered


def sync_adot_wayback_pdfs(
    db_path: Path | str | None = None,
    *,
    limit: int = 500,
    max_snapshots: int = 60,
    delay_seconds: float = 3.5,
    dry_run: bool = False,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Recover historical itemized ADOT Bid Tab PDFs archived on AWS S3 & Wayback CDX."""
    sess = _is_valid_session(session)
    candidates = discover_wayback_tabulation_pdfs(
        session=sess,
        max_snapshots=max_snapshots,
        delay_seconds=delay_seconds,
    )
    existing_itemized = get_itemized_adot_project_nums(db_path)

    with db_session(db_path) as conn:
        docs_saved_nums = {
            r[0]
            for r in conn.execute(
                """
                SELECT DISTINCT p.agency_project_num
                FROM projects p
                JOIN project_documents pd ON p.project_id = pd.project_id
                WHERE p.agency = 'ADOT' AND pd.sha256 IS NOT NULL
                """
            ).fetchall()
            if r[0]
        }

    ingested = 0
    docs_archived = 0
    skipped = 0
    total_lines = 0
    errors: list[str] = []

    for item in candidates[:limit]:
        proj_hint = item.get("project_num") or ""
        already_itemized = bool(proj_hint and proj_hint in existing_itemized)
        if already_itemized and proj_hint in docs_saved_nums:
            skipped += 1
            continue

        pdf_url = item["pdf_url"]
        if item.get("is_wayback_pdf") == "1" and delay_seconds > 0:
            time.sleep(min(delay_seconds, 1.5))

        try:
            pdf_resp = None
            for attempt in range(2):
                r = sess.get(pdf_url, timeout=30)
                if r.status_code == 429 and attempt == 0:
                    time.sleep(5.0)
                    continue
                pdf_resp = r
                break
            if pdf_resp is None or not pdf_resp.ok:
                skipped += 1
                continue
            if not pdf_resp.content.lstrip()[:8].startswith(b"%PDF"):
                skipped += 1
                continue

            doc_meta = None if dry_run else save_pdf_artifact(
                pdf_resp.content,
                bucket="adot",
                filename_hint=pdf_url.split("/")[-1] or proj_hint or "adot_bidtab",
                source_url=pdf_url,
            )

            if already_itemized:
                if doc_meta and not dry_run:
                    with db_session(db_path) as conn:
                        row = conn.execute(
                            "SELECT project_id, title FROM projects WHERE agency = 'ADOT' AND agency_project_num = ?",
                            (proj_hint,),
                        ).fetchone()
                        if row:
                            upsert_project_document(
                                conn,
                                project_id=int(row[0]),
                                doc_type="bid_tab_pdf",
                                title=str(row[1] or item.get("title") or proj_hint),
                                source_url=pdf_url,
                                archive_url=doc_meta.get("archive_url"),
                                local_path=doc_meta.get("local_path"),
                                bucket=doc_meta.get("bucket"),
                                asset_name=doc_meta.get("asset_name"),
                                sha256=doc_meta.get("sha256"),
                                file_size=doc_meta.get("file_size"),
                            )
                            docs_saved_nums.add(proj_hint)
                            docs_archived += 1
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
                if parsed.agency_project_num in existing_itemized and doc_meta and not dry_run:
                    with db_session(db_path) as conn:
                        row = conn.execute(
                            "SELECT project_id, title FROM projects WHERE agency = 'ADOT' AND agency_project_num = ?",
                            (parsed.agency_project_num,),
                        ).fetchone()
                        if row:
                            upsert_project_document(
                                conn,
                                project_id=int(row[0]),
                                doc_type="bid_tab_pdf",
                                title=str(row[1] or parsed.title or parsed.agency_project_num),
                                source_url=pdf_url,
                                archive_url=doc_meta.get("archive_url"),
                                local_path=doc_meta.get("local_path"),
                                bucket=doc_meta.get("bucket"),
                                asset_name=doc_meta.get("asset_name"),
                                sha256=doc_meta.get("sha256"),
                                file_size=doc_meta.get("file_size"),
                            )
                            docs_saved_nums.add(parsed.agency_project_num)
                            docs_archived += 1
                skipped += 1
                continue

            if not dry_run:
                ingest_parsed_project(parsed, db_path, doc_meta=doc_meta, doc_type="bid_tab_pdf")
                if doc_meta:
                    docs_archived += 1
                    docs_saved_nums.add(parsed.agency_project_num)
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
        "docs_archived": docs_archived,
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


_STB_SKIP_KW = ("REJECT", "DEFER", "POSTPONE", "WITHDRAW", "NO AWARD", "CANCEL")
_STB_CONTRACTOR_SUFFIX_RE = re.compile(
    r"^(?:INC\.?|LLC|CO\.?|CORP\.?|CORPORATION|LP|LLP|PLLC|DBA\s+.+)$",
    re.IGNORECASE,
)


def _extract_stb_meeting_date(pdf_url: str, page_text: str = "") -> str:
    # e.g. Awards-2026101626.pdf -> 2026-10-16
    m_8 = re.search(r"(\d{4})(\d{2})(\d{2})", pdf_url)
    if m_8:
        y, mo, d = int(m_8.group(1)), int(m_8.group(2)), int(m_8.group(3))
        if 2005 <= y <= 2035 and 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    # e.g. 2018-051818_Awards.pdf or 2010-050410_awards.pdf
    m_yr_mmddyy = re.search(r"(20\d{2})[_-](\d{2})(\d{2})(\d{2})", pdf_url)
    if m_yr_mmddyy:
        y, mo, d = int(m_yr_mmddyy.group(1)), int(m_yr_mmddyy.group(2)), int(m_yr_mmddyy.group(3))
        if 2005 <= y <= 2035 and 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}-{mo:02d}-{d:02d}"
    # e.g. 220520-awards.pdf -> YYMMDD, or Awards-021624.pdf -> MMDDYY
    m_6 = re.search(r"(?:^|/|[-_])(\d{2})(\d{2})(\d{2})(?:[-_.]|$)", pdf_url)
    if m_6:
        a, b, c = int(m_6.group(1)), int(m_6.group(2)), int(m_6.group(3))
        # Check MMDDYY first when year is in group 3 (e.g. 021624)
        if 1 <= a <= 12 and 1 <= b <= 31 and 10 <= c <= 35:
            return f"20{c:02d}-{a:02d}-{b:02d}"
        # Check YYMMDD (e.g. 220520)
        if 10 <= a <= 35 and 1 <= b <= 12 and 1 <= c <= 31:
            return f"20{a:02d}-{b:02d}-{c:02d}"
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


def _clean_bidder_cell(text: str) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if not cleaned:
        return ""
    if _STB_SKIP_KW and any(cleaned.upper().startswith(k) for k in ("AWARD", "TRACS", "LOW BIDDER")):
        return ""
    return cleaned


def _parse_stb_table_flexible(
    table: list[list[str | None]],
    *,
    resolved_date: str,
    source_url: str,
    seen_keys: set[tuple[str, str]],
) -> list[StbAwardRecord]:
    """Parse 4-to-11 column or multi-row STB Award tables."""
    records: list[StbAwardRecord] = []
    pending_tracs_cells: list[str] = []
    pending_desc_parts: list[str] = []
    pending_bidder = ""
    pending_amount: float | None = None
    pending_skip = False
    last_record: StbAwardRecord | None = None

    def _emit_if_ready() -> None:
        nonlocal pending_tracs_cells, pending_desc_parts, pending_bidder, pending_amount, pending_skip, last_record
        if pending_skip:
            pending_tracs_cells = []
            pending_desc_parts = []
            pending_bidder = ""
            pending_amount = None
            pending_skip = False
            return
        if not pending_tracs_cells or pending_amount is None or not pending_bidder:
            return
        desc = " ".join(p for p in pending_desc_parts if p).strip()[:600]
        for tc in pending_tracs_cells:
            tracs_list = _extract_all_tracs(tc)
            for tracs in tracs_list:
                prefix = tc.split(tracs)[0].strip()
                county = _county_from_route(prefix)
                if not county:
                    m_co = re.search(r"\b([A-Za-z ]+?)\s+County\b", desc, re.IGNORECASE)
                    if m_co:
                        county = m_co.group(1).strip().title()
                fed_lines = [
                    ln.strip()
                    for ln in tc.splitlines()
                    if ln.strip() and tracs not in ln.upper()
                ]
                fed_num = fed_lines[-1] if fed_lines else ""
                key = (tracs, pending_bidder.upper())
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                rec = StbAwardRecord(
                    tracs_num=tracs,
                    federal_project_num=fed_num,
                    description=desc or f"ADOT Project {tracs}",
                    low_bidder=pending_bidder,
                    award_amount=pending_amount,
                    action="AWARD",
                    meeting_date=resolved_date,
                    county=county,
                    route_or_location=prefix[:120],
                    source_url=source_url,
                )
                records.append(rec)
                last_record = rec
        pending_tracs_cells = []
        pending_desc_parts = []
        pending_bidder = ""
        pending_amount = None
        pending_skip = False

    for row in table:
        if not row:
            continue
        raw_cells = [(c or "").strip() for c in row]
        cells = [re.sub(r"\s+", " ", c) for c in raw_cells]
        joined = " ".join(c for c in cells if c).upper()
        if not joined:
            continue
        if "DESCRIPTION OF WORK" in joined and ("LOW BIDDER" in joined or "TRACS" in joined):
            continue

        tracs_idx: int | None = None
        action_idx: int | None = None

        for i, c in enumerate(cells):
            if tracs_idx is None and _extract_tracs(c):
                tracs_idx = i
            cu = c.upper()
            if action_idx is None and (
                "AWARD" in cu
                or _AWARD_AMOUNT_RE.search(c)
                or any(kw in cu for kw in _STB_SKIP_KW)
            ):
                # Avoid treating description money text before TRACS as board action
                if tracs_idx is None or i >= tracs_idx:
                    if any(kw in cu for kw in _STB_SKIP_KW) or "AWARD" in cu or i >= max(len(cells) - 2, 2):
                        action_idx = i

        row_tracs_raw = raw_cells[tracs_idx] if tracs_idx is not None else ""
        row_action = cells[action_idx] if action_idx is not None else ""
        row_amount = _parse_money(row_action) if row_action else None
        row_skip = any(kw in row_action.upper() for kw in _STB_SKIP_KW)

        # Find contractor candidate between tracs_idx and action_idx (or right before action_idx)
        row_bidder = ""
        if action_idx is not None:
            start_b = (tracs_idx + 1) if tracs_idx is not None else 0
            for j in range(action_idx - 1, start_b - 1, -1):
                cand = _clean_bidder_cell(cells[j])
                if cand and not _AWARD_AMOUNT_RE.search(cand) and not _extract_tracs(cand):
                    row_bidder = cand
                    break
        elif tracs_idx is not None:
            for j in range(tracs_idx + 1, len(cells)):
                cand = _clean_bidder_cell(cells[j])
                if cand and not _AWARD_AMOUNT_RE.search(cand):
                    row_bidder = cand
                    break

        desc_end = tracs_idx if tracs_idx is not None else (action_idx if action_idx is not None else len(cells))
        row_desc = " ".join(c for c in cells[:desc_end] if c).strip()

        # Continuation description-only row right after a completed record
        if tracs_idx is None and action_idx is None and not row_bidder:
            if row_desc:
                if pending_tracs_cells or pending_amount is not None:
                    pending_desc_parts.append(row_desc)
                elif last_record is not None and len(last_record.description) < 550:
                    last_record.description = f"{last_record.description} {row_desc}".strip()[:600]
            continue

        # New complete or partial block
        if (tracs_idx is not None and pending_tracs_cells) or (
            action_idx is not None and (pending_amount is not None or pending_skip)
        ):
            _emit_if_ready()

        if row_desc:
            pending_desc_parts.append(row_desc)
        if row_tracs_raw:
            pending_tracs_cells.append(row_tracs_raw)
        if row_bidder and not pending_bidder:
            pending_bidder = row_bidder
        if row_skip:
            pending_skip = True
        if row_amount is not None:
            pending_amount = row_amount

        _emit_if_ready()

    _emit_if_ready()
    return records


def _parse_stb_plain_text_page(
    text: str,
    *,
    resolved_date: str,
    source_url: str,
    seen_keys: set[tuple[str, str]],
) -> list[StbAwardRecord]:
    """Parse borderless plain-text STB Awards PDFs (e.g. 2010 OFFICE MEMO format)."""
    records: list[StbAwardRecord] = []
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    last_record: StbAwardRecord | None = None
    last_route = ""

    for line in lines:
        upper = line.upper()
        if any(
            hdr in upper
            for hdr in (
                "ARIZONA DEPARTMENT OF TRANSPORTATION",
                "OFFICE MEMO",
                "TRANSPORTATION BOARD",
                "LOW BIDDER AMOUNT",
                "TRACS/PROJECT",
            )
        ):
            continue

        amt_m = _AWARD_AMOUNT_RE.search(line)
        tracs = _extract_tracs(line)
        has_action = bool(amt_m and ("AWARD" in upper or any(k in upper for k in _STB_SKIP_KW)))

        if tracs and has_action and amt_m:
            if any(k in upper for k in _STB_SKIP_KW):
                last_record = None
                continue
            amount = float(amt_m.group(1).replace(",", ""))
            before_tracs, after_tracs = upper.split(tracs, 1)
            # Recover case-preserving slice after tracs
            idx_t = upper.find(tracs)
            raw_after = line[idx_t + len(tracs) :]
            raw_before = line[:idx_t].strip()
            bidder = raw_after[: raw_after.find("$")].strip() if "$" in raw_after else ""
            bidder = re.sub(r"\s+", " ", bidder).strip()
            if not bidder:
                continue
            county = _county_from_route(raw_before or last_route)
            key = (tracs, bidder.upper())
            if key in seen_keys:
                continue
            seen_keys.add(key)
            rec = StbAwardRecord(
                tracs_num=tracs,
                description=f"ADOT Board Award {tracs} ({raw_before})".strip(),
                low_bidder=bidder,
                award_amount=amount,
                action="AWARD",
                meeting_date=resolved_date,
                county=county,
                route_or_location=raw_before[:120],
                source_url=source_url,
            )
            records.append(rec)
            last_record = rec
            last_route = raw_before
            continue

        # Continuation suffix line like "ARRA-IM-017-B(205)A Inc" or "Southwest Asphalt Paving"
        if last_record is not None and not _AWARD_AMOUNT_RE.search(line) and not tracs:
            parts = line.split(None, 1)
            if len(parts) == 2 and (re.match(r"^[A-Z0-9-]+$", parts[0]) or "(" in parts[0]):
                if not last_record.federal_project_num:
                    last_record.federal_project_num = parts[0]
                tail = parts[1].strip()
                if tail and len(tail) < 50:
                    last_record.low_bidder = f"{last_record.low_bidder} {tail}".strip()
            elif _STB_CONTRACTOR_SUFFIX_RE.match(line) or len(line.split()) <= 4:
                last_record.low_bidder = f"{last_record.low_bidder} {line}".strip()
            last_record = None

    return records


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
    resolved_date = meeting_date

    with pdf_ctx as pdf:
        if not resolved_date and pdf.pages:
            first_text = pdf.pages[0].extract_text() or ""
            resolved_date = _extract_stb_meeting_date(source_url, first_text)

        for page in pdf.pages:
            tables = page.extract_tables() or []
            page_records: list[StbAwardRecord] = []
            for table in tables:
                page_records.extend(
                    _parse_stb_table_flexible(
                        table,
                        resolved_date=resolved_date,
                        source_url=source_url,
                        seen_keys=seen_keys,
                    )
                )
            if not page_records:
                txt = page.extract_text() or ""
                if txt.strip():
                    page_records.extend(
                        _parse_stb_plain_text_page(
                            txt,
                            resolved_date=resolved_date,
                            source_url=source_url,
                            seen_keys=seen_keys,
                        )
                    )
            records.extend(page_records)

    return records


def discover_stb_award_pdfs(
    *,
    max_pages: int = 18,
    limit: int | None = None,
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
        for a in soup.select("a[href$='.pdf'], a[href*='.pdf?']"):
            href = str(a.get("href") or "")
            if "award" in href.lower() and href not in seen_pages:
                full_pdf = urljoin(STB_MEETING_DOCS_URL, href)
                seen_pages.add(full_pdf)
                award_page_urls.append(full_pdf)
                new_on_page += 1
        if new_on_page == 0:
            break
        if limit is not None and len(award_page_urls) >= limit:
            break

    pdf_items: list[dict[str, str]] = []
    seen_pdfs: set[str] = set()

    for page_url in award_page_urls:
        if limit is not None and len(pdf_items) >= limit:
            break
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
        seen_docs = {
            str(r[0]).lower()
            for r in conn.execute(
                """
                SELECT DISTINCT source_url
                FROM project_documents
                WHERE doc_type = 'stb_award_pdf' AND sha256 IS NOT NULL
                """
            ).fetchall()
            if r[0]
        }

    ingested_projects = 0
    ingested_bids = 0
    docs_archived = 0
    pdfs_processed = 0
    skipped = 0
    errors: list[str] = []

    if not dry_run:
        with db_session(db_path) as conn:
            for item in pdfs[:limit]:
                pdf_url = item["pdf_url"]
                if pdf_url.lower() in seen_docs:
                    skipped += 1
                    continue
                try:
                    resp = sess.get(pdf_url, timeout=25)
                    if not resp.ok:
                        skipped += 1
                        continue
                    records = parse_stb_awards_pdf(
                        resp.content,
                        meeting_date=item.get("meeting_date", ""),
                        source_url=pdf_url,
                    )
                    pdfs_processed += 1
                    if pdfs_processed % 25 == 0:
                        print(
                            f"[adot_stb] processed={pdfs_processed} projects={ingested_projects} docs={docs_archived}",
                            flush=True,
                        )
                    if not records:
                        continue
                    doc_meta = save_pdf_artifact(
                        resp.content,
                        bucket="stb",
                        filename_hint=pdf_url.split("/")[-1] or f"stb_{item.get('meeting_date', 'awards')}",
                        source_url=pdf_url,
                    )
                    if doc_meta:
                        docs_archived += 1

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
                        if doc_meta:
                            upsert_project_document(
                                conn,
                                project_id=pid,
                                doc_type="stb_award_pdf",
                                title=f"ADOT Board Award {rec.meeting_date or ''}".strip(),
                                source_url=pdf_url,
                                archive_url=doc_meta.get("archive_url"),
                                local_path=doc_meta.get("local_path"),
                                bucket=doc_meta.get("bucket"),
                                asset_name=doc_meta.get("asset_name"),
                                sha256=doc_meta.get("sha256"),
                                file_size=doc_meta.get("file_size"),
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
                    seen_docs.add(pdf_url.lower())
                    conn.commit()
                except Exception as exc:
                    errors.append(f"{pdf_url}: {exc}")
    else:
        for item in pdfs[:limit]:
            pdf_url = item["pdf_url"]
            if pdf_url.lower() in seen_docs:
                skipped += 1
                continue
            try:
                resp = sess.get(pdf_url, timeout=25)
                if not resp.ok:
                    skipped += 1
                    continue
                records = parse_stb_awards_pdf(
                    resp.content,
                    meeting_date=item.get("meeting_date", ""),
                    source_url=pdf_url,
                )
                pdfs_processed += 1
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
        "docs_archived": docs_archived,
        "skipped": skipped,
        "errors": errors,
    }


# ---------------------------------------------------------------------------
# 3. ADOT CNSADS As-Read Historical Bid Results (2000-2026)
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
    secondary_tracs: list[str] = field(default_factory=list)
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

    headline_tracs = _extract_all_tracs(headline)
    if not headline_tracs:
        headline_tracs = _extract_all_tracs(body_text[:1500])
    tracs = headline_tracs[0] if headline_tracs else f"ASREAD-{detail_id}"
    secondary_tracs = [t for t in headline_tracs[1:] if t != tracs]

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
        secondary_tracs=secondary_tracs,
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

    all_tracs = [parsed.tracs_num, *parsed.secondary_tracs]
    written = 0

    for idx, tracs in enumerate(all_tracs):
        pid = upsert_project(
            conn,
            agency="ADOT",
            agency_project_num=tracs,
            title=parsed.title,
            route_or_location=parsed.route_or_location or None,
            county=parsed.county,
            district=parsed.district,
            letting_date=parsed.letting_date or None,
            engineers_estimate=parsed.engineers_estimate if idx == 0 else None,
            winning_bid_total=winner.bid_amount if (winner and idx == 0) else None,
            awarded_contractor_id=winner_cid if idx == 0 else None,
            source_url=parsed.source_url,
        )
        if parsed.source_url:
            upsert_project_document(
                conn,
                project_id=pid,
                doc_type="as_read_bid",
                title=f"ADOT As-Read Bid Results ({parsed.detail_id})",
                source_url=parsed.source_url,
            )
        if idx > 0:
            continue

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
            written += 1

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
            written += 1

    return written


def sync_adot_as_read_history(
    db_path: Path | str | None = None,
    *,
    start_year: int = 2000,
    end_year: int = 2026,
    max_seq_per_year: int = 205,
    max_consecutive_misses: int = 25,
    limit: int = 6000,
    timeout: int = 7,
    max_workers: int = 6,
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

    try:
        idx_resp = sess.get(ADOT_AS_READ_INDEX_URL, timeout=15)
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

    def _fetch_one(did: str) -> tuple[str, ParsedAsReadProject | None, str | None]:
        url = ADOT_AS_READ_DETAIL_URL.format(detail_id=did)
        try:
            local_sess = sess if session is not None else _is_valid_session(None)
            resp = local_sess.get(url, timeout=timeout)
            if not resp.ok:
                return (did, None, None)
            parsed = parse_as_read_detail_html(resp.text, detail_id=did, source_url=url)
            return (did, parsed, None)
        except Exception as exc:
            return (did, None, f"{did}: {exc}")

    def _consume_batch(dids: list[str], conn: Any | None) -> list[bool]:
        nonlocal ingested_projects, ingested_bids, skipped, checked
        to_fetch: list[str] = []
        pre_status: dict[str, bool] = {}
        for did in dids:
            url = ADOT_AS_READ_DETAIL_URL.format(detail_id=did)
            if url in existing_detail_urls:
                skipped += 1
                pre_status[did] = True
            else:
                checked += 1
                to_fetch.append(did)

        fetched: dict[str, ParsedAsReadProject | None] = {}
        if to_fetch:
            if session is None and max_workers > 1 and len(to_fetch) > 1:
                with ThreadPoolExecutor(max_workers=min(max_workers, len(to_fetch))) as pool:
                    results = list(pool.map(_fetch_one, to_fetch))
            else:
                results = [_fetch_one(did) for did in to_fetch]
            for did, parsed, err in results:
                if err and "timed out" not in err.lower():
                    errors.append(err)
                fetched[did] = parsed

        statuses: list[bool] = []
        for did in dids:
            if did in pre_status:
                statuses.append(True)
                continue
            parsed = fetched.get(did)
            if parsed is None:
                statuses.append(False)
                continue
            if not dry_run and conn is not None:
                cnt = _ingest_as_read_project(parsed, conn)
                ingested_bids += cnt
            else:
                ingested_bids += len(parsed.bidders)
            existing_detail_urls.add(parsed.source_url)
            ingested_projects += 1
            statuses.append(True)
        if not dry_run and conn is not None and to_fetch:
            conn.commit()
        return statuses

    with db_session(db_path) as conn:
        active_conn = None if dry_run else conn
        if candidate_ids:
            _consume_batch(candidate_ids[:limit], active_conn)

        chunk_size = max(max_workers, 4) if session is None else 1
        for year in range(end_year, start_year - 1, -1):
            if checked >= limit:
                break
            consecutive_misses = 0
            seq = 1
            while seq <= max_seq_per_year and checked < limit:
                dids: list[str] = []
                while len(dids) < chunk_size and seq <= max_seq_per_year and checked + len(dids) < limit:
                    did = f"{year}{seq:03d}"
                    seq += 1
                    if did in seen_ids:
                        consecutive_misses = 0
                        continue
                    seen_ids.add(did)
                    dids.append(did)
                if not dids:
                    continue
                statuses = _consume_batch(dids, active_conn)
                stop_year = False
                for found in statuses:
                    if found:
                        consecutive_misses = 0
                    else:
                        consecutive_misses += 1
                        if consecutive_misses >= max_consecutive_misses:
                            stop_year = True
                            break
                if stop_year:
                    break
            print(
                f"[adot_as_read] year={year} checked={checked} ingested={ingested_projects} skipped={skipped}",
                flush=True,
            )

    return {
        "agency": "ADOT",
        "source": "adot_as_read_history",
        "checked": checked,
        "ingested_projects": ingested_projects,
        "ingested_project_bids": ingested_bids,
        "skipped": skipped,
        "errors": errors,
    }
