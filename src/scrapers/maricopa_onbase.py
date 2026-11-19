"""Maricopa County / MCDOT / FCD OnBase PublicAccess & Expiration Report scraper.

Pulls civil/heavy-construction county contracts from:
1. Maricopa County Office of Procurement Services OnBase API (`edms.maricopa.gov/ops/api`)
   via `FullTextSearch` and sub-sharded `CustomQuery/KeywordSearch` (bypassing 250-row caps).
2. Maricopa County `Expiration-Report-PDF` (`maricopa.gov/DocumentCenter/View/6444/Expiration-Report-PDF`),
   capturing contract numbers, descriptions, legal vendor names, and expiration dates.
3. Optional PDF archival via `GET /ops/api/Document/{doc_id}` with page 1-4 dollar extraction.
"""

from __future__ import annotations

import html as html_lib
import io
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import quote

import pdfplumber
import requests

from src.db import (
    db_session,
    upsert_contractor,
    upsert_project,
    upsert_project_bid,
    upsert_project_document,
)
from src.pdf_archive import save_pdf_artifact
from src.scrapers.maricopa_bidsync import is_maricopa_civil_project

ONBASE_FULLTEXT_URL = "https://edms.maricopa.gov/ops/api/DocumentType/FullTextSearch"
ONBASE_KEYWORD_SEARCH_URL = "https://edms.maricopa.gov/ops/api/CustomQuery/KeywordSearch"
ONBASE_DOCUMENT_PDF_URL = "https://edms.maricopa.gov/ops/api/Document/{doc_id}"
ONBASE_VIEWER_URL = "https://edms.maricopa.gov/ops/#!/view/{doc_id}"
MARICOPA_EXPIRATION_PDF_URL = "https://www.maricopa.gov/DocumentCenter/View/6444/Expiration-Report-PDF"

DEFAULT_ONBASE_CIVIL_TERMS: tuple[str, ...] = (
    "MCDOT",
    "Flood Control",
    "asphalt",
    "paving",
    "roadway",
    "drainage",
    "storm drain",
    "culvert",
    "traffic signal",
    "bridge",
    "waterline",
    "sewer",
    "guardrail",
    "striping",
    "paint stripes",
    "grading",
    "intersection",
    "chip seal",
    "crack seal",
    "slurry seal",
    "aggregate",
    "crushed",
    "dust control",
    "civil engineering",
    "ITS",
    "riprap",
)

_CONTRACT_NAME_RE = re.compile(
    r"^OPS\s*-\s*[^-]+-\s*([A-Z0-9]{0,6}\d{5,7}(?:-[A-Z0-9]+)?)\s*-+\s*(.+)$",
    re.IGNORECASE,
)
_DOC_TRAILING_VENDOR_RE = re.compile(
    r"\s+-\s+(?:PRICING|CONTRACT|AWARD|AMENDMENT|MODIFICATION|RENEWAL)\s*-?\s*([A-Z0-9][A-Z0-9 &.,'/-]{2,})?$",
    re.IGNORECASE,
)
_SERIAL_VENDOR_RE = re.compile(
    r"^([A-Z0-9][A-Z0-9 &.,'/-]{3,65}?)\s+(?:FOR|SERIAL)\s+",
    re.IGNORECASE,
)
_DOLLAR_RE = re.compile(r"\$\s*([\d,]{3,}\.\d{2})")
_MDY_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
_EXP_LINE_RE = re.compile(
    r"^(?:MA\d*,D\d+,)?\s*(\d{5,7}(?:-[A-Z0-9]+)?)\s+(.+?)\s+(\d{1,2}/\d{1,2}/\d{4})\b",
    re.IGNORECASE,
)

_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}


@dataclass
class OnBaseContractRecord:
    contract_num: str
    title: str
    agency: str = "MCDOT"
    vendor_name: str = ""
    contract_date: str = ""
    amount: float | None = None
    doc_id: str = ""
    source_url: str = ""
    summary: str = ""


def _parse_mdy(text: str) -> str:
    m = _MDY_DATE_RE.search(text or "")
    if not m:
        return ""
    try:
        return datetime(int(m.group(3)), int(m.group(1)), int(m.group(2))).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _clean_summary(raw: str) -> str:
    unescaped = html_lib.unescape(raw or "")
    return re.sub(r"\s+", " ", unescaped).strip()


def _classify_maricopa_agency(name: str, summary: str) -> str:
    combined = f"{name} {summary}".upper()
    if "MCDOT" in combined or "DEPARTMENT OF TRANSPORTATION" in combined:
        return "MCDOT"
    if "FLOOD CONTROL" in combined or re.search(r"\bFCD\b", combined):
        return "Maricopa County FCD"
    return "MCDOT" if is_maricopa_civil_project(name) else "Maricopa County"


def _parse_contract_name_and_vendor(doc_name: str, summary: str = "") -> tuple[str, str, str]:
    """Extract `(contract_num, clean_title, vendor_hint)` from an OnBase document name & summary."""
    clean_name = re.sub(r"\s+", " ", (doc_name or "").strip())
    contract_num = ""
    title = clean_name
    vendor = ""

    m = _CONTRACT_NAME_RE.match(clean_name)
    if m:
        contract_num = m.group(1).upper().strip()
        title = m.group(2).strip()
    else:
        m_num = re.search(r"\b([A-Z]{0,6}\d{6}(?:-[A-Z0-9]+)?)\b", clean_name)
        if m_num:
            contract_num = m_num.group(1).upper()

    mv = _DOC_TRAILING_VENDOR_RE.search(title)
    if mv:
        candidate = (mv.group(1) or "").strip()
        if candidate and not any(
            skip in candidate.upper()
            for skip in ("PRICING", "CONTRACT", "AMENDMENT", "RENEWAL", "ATTACHMENT")
        ):
            vendor = candidate
        title = title[: mv.start()].strip()

    if not title or title == "-":
        title = summary[:140].strip() if summary else contract_num

    if not vendor and summary:
        ms = _SERIAL_VENDOR_RE.match(summary)
        if ms:
            cand = ms.group(1).strip()
            if len(cand) >= 4 and "ATTACH" not in cand.upper() and "COPY TO" not in cand.upper():
                vendor = cand

    return contract_num, title, vendor


def fetch_onbase_keyword_index(
    prefixes: tuple[str, ...] = (
        "26*", "25*", "24*", "23*", "22*", "21*", "20*", "19*", "18*", "17*", "16*", "15*"
    ),
    *,
    session: requests.Session | None = None,
    timeout: int = 20,
) -> dict[str, tuple[str, str]]:
    """Query OnBase `KeywordSearch` (QueryID=270), sub-sharding 250-cap prefixes into 4-digit prefixes."""
    sess = session or requests.Session()
    sess.headers.update(_BROWSER_HEADERS)
    by_contract: dict[str, tuple[str, str]] = {}

    def _query_prefix(prefix: str) -> list[dict[str, Any]]:
        try:
            resp = sess.post(
                ONBASE_KEYWORD_SEARCH_URL,
                json={
                    "QueryID": 270,
                    "Keywords": [{"ID": 2916, "Value": prefix}],
                    "QueryLimit": 500,
                },
                timeout=timeout,
            )
            if not resp.ok:
                return []
            return list((resp.json() or {}).get("Data") or [])
        except Exception:
            return []

    def _consume_rows(rows: list[dict[str, Any]]) -> None:
        for row in rows:
            name = str(row.get("Name") or "")
            cnum, title, _ = _parse_contract_name_and_vendor(name)
            if not cnum or not is_maricopa_civil_project(title):
                continue
            cols = row.get("DisplayColumnValues") or []
            vendor = str(cols[1].get("Value") or "").strip() if len(cols) >= 2 else ""
            cdate = _parse_mdy(str(cols[2].get("Value") or "")) if len(cols) >= 3 else ""
            if vendor or cdate:
                prev_v, prev_d = by_contract.get(cnum, ("", ""))
                by_contract[cnum] = (vendor or prev_v, cdate or prev_d)

    for prefix in prefixes:
        rows = _query_prefix(prefix)
        _consume_rows(rows)
        # OnBase hard-caps KeywordSearch at 250 rows; shard saturated 2-digit prefixes into 4-digit ranges
        if len(rows) >= 245 and len(prefix) == 3 and prefix.endswith("*"):
            yr = prefix[:2]
            for mid in ("00", "01", "02"):
                sub_rows = _query_prefix(f"{yr}{mid}*")
                _consume_rows(sub_rows)

    return by_contract


def _split_expiration_desc_and_vendor(rest: str) -> tuple[str, str]:
    """Split `<Description> <Vendor Legal Name>` cleanly."""
    cleaned = re.sub(r"\s+", " ", (rest or "").strip())
    # Look for ALL-CAPS vendor start right after mixed-case description or project code (e.g. "Improvements TT0752 LOENBRO, LLC")
    m_caps = re.search(
        r"^(.*?(?:[a-z)]|\b(?:TT|MC|FCD)\d{3,5}))\s+(?!(?:TT|MC|FCD)\d{3,5}\b)([A-Z][A-Z0-9,&.'/-]*(?:\s+[A-Z0-9,&.'/-]+){0,8})$",
        cleaned,
    )
    if m_caps:
        desc, vendor = m_caps.group(1).strip(), m_caps.group(2).strip()
        # Verify the trailing caps block is longer than a 1-word acronym unless it ends with a legal suffix
        if len(vendor) >= 4 and (
            " " in vendor
            or "," in vendor
            or re.search(r"\b(LLC|INC|CORP|LP|CO|LTD)\b\.?$", vendor)
        ):
            return desc, vendor

    m = re.search(
        r"^(.+?)\s+([A-Z][A-Z0-9.&'/-]*(?:,\s*[A-Z]{2,}|\s+[A-Z0-9.&'/-]+){0,6}\s*,?\s*"
        r"(?:LLC|INC\.?|CORP\.?|LP|CO\.?|LTD\.?|COMPANY|CONSTRUCTION|PAVING|ASPHALT|"
        r"ENGINEERING|CONTRACTORS|SUPPLY|SERVICES))$",
        cleaned,
    )
    if m:
        desc, vendor = m.group(1).strip(), m.group(2).strip()
        m_code = re.match(r"^((?:TT|MC|FCD)\d{3,5})\s+(.+)$", vendor, re.IGNORECASE)
        if m_code:
            desc = f"{desc} {m_code.group(1)}".strip()
            vendor = m_code.group(2).strip()
        return desc, vendor
    return cleaned, ""


def _parse_expiration_page_words(
    page: Any,
    vendor_x0: float | None,
    date_x0: float | None,
) -> tuple[list[tuple[str, str, str, str]], float | None, float | None]:
    """Extract `(contract_num, title, vendor, end_date)` rows from an Expiration Report page."""
    text = page.extract_text() or ""
    rows: list[tuple[str, str, str, str]] = []
    for line in text.splitlines():
        m = _EXP_LINE_RE.match(line.strip())
        if not m:
            continue
        cnum = m.group(1).upper()
        title, vendor = _split_expiration_desc_and_vendor(m.group(2))
        end_date = _parse_mdy(m.group(3))
        rows.append((cnum, title, vendor, end_date))
    return rows, vendor_x0, date_x0


def fetch_expiration_report_contracts(
    *,
    session: requests.Session | None = None,
    timeout: int = 25,
    archive_pdf: bool = True,
) -> tuple[dict[str, tuple[str, str, str]], dict[str, Any] | None]:
    """Fetch & parse Maricopa `Expiration-Report-PDF` -> `({contract_num: (title, vendor, end_date)}, doc_meta)`."""
    sess = session or requests.Session()
    sess.headers.update(_BROWSER_HEADERS)
    out: dict[str, tuple[str, str, str]] = {}
    doc_meta: dict[str, Any] | None = None

    try:
        resp = sess.get(MARICOPA_EXPIRATION_PDF_URL, timeout=timeout)
        if not resp.ok or not resp.content.lstrip()[:8].startswith(b"%PDF"):
            return out, None
        if archive_pdf:
            doc_meta = save_pdf_artifact(
                resp.content,
                bucket="maricopa",
                filename_hint="Maricopa-Expiration-Report",
                source_url=MARICOPA_EXPIRATION_PDF_URL,
            )

        with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
            vendor_x0: float | None = None
            date_x0: float | None = None
            for page in pdf.pages:
                word_rows, vendor_x0, date_x0 = _parse_expiration_page_words(page, vendor_x0, date_x0)
                if word_rows:
                    for cnum, title, vendor, end_date in word_rows:
                        if is_maricopa_civil_project(title):
                            out[cnum] = (title, vendor, end_date)
                else:
                    text = page.extract_text() or ""
                    for line in text.splitlines():
                        m = _EXP_LINE_RE.match(line.strip())
                        if not m:
                            continue
                        cnum = m.group(1).upper()
                        title, vendor = _split_expiration_desc_and_vendor(m.group(2))
                        dt = _parse_mdy(m.group(3))
                        if is_maricopa_civil_project(title):
                            out[cnum] = (title, vendor, dt)
    except Exception:
        return out, doc_meta

    return out, doc_meta


def _extract_amount_from_pdf_bytes(pdf_bytes: bytes, max_pages: int = 4) -> float | None:
    """Scan first few pages of an OnBase contract PDF for dollar contract/NTE values."""
    if not pdf_bytes or not pdf_bytes.lstrip()[:8].startswith(b"%PDF"):
        return None
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            text_chunks = [
                (page.extract_text() or "") for page in pdf.pages[:max_pages]
            ]
        combined = "\n".join(text_chunks)
        # Prefer explicit contract value / NTE phrases first
        m_nte = re.search(
            r"(?:not[- ]to[- ]exceed|total\s+contract\s+(?:amount|price|value)|"
            r"award(?:ed)?\s+amount|contract\s+sum)\D{0,25}\$\s*([\d,]{4,}\.\d{2})",
            combined,
            re.IGNORECASE,
        )
        if m_nte:
            return float(m_nte.group(1).replace(",", ""))
        amounts = [
            float(val.replace(",", ""))
            for val in _DOLLAR_RE.findall(combined)
        ]
        # Avoid tiny unit prices by requiring >= $10,000 when no explicit label matched
        large = [a for a in amounts if a >= 10000.0]
        return max(large) if large else None
    except Exception:
        return None


def fetch_onbase_civil_contracts(
    *,
    search_terms: tuple[str, ...] = DEFAULT_ONBASE_CIVIL_TERMS,
    query_limit: int = 250,
    include_expiration_pdf: bool = False,
    session: requests.Session | None = None,
    timeout: int = 20,
) -> tuple[list[OnBaseContractRecord], dict[str, Any] | None]:
    """Query OnBase `FullTextSearch` + sharded `KeywordSearch` + optional `Expiration-Report-PDF`."""
    sess = session or requests.Session()
    sess.headers.update(_BROWSER_HEADERS)

    keyword_meta = fetch_onbase_keyword_index(session=sess, timeout=timeout)
    exp_meta: dict[str, tuple[str, str, str]] = {}
    exp_doc_meta: dict[str, Any] | None = None
    if include_expiration_pdf:
        exp_meta, exp_doc_meta = fetch_expiration_report_contracts(session=sess, timeout=timeout)

    by_contract: dict[str, OnBaseContractRecord] = {}

    for term in search_terms:
        try:
            resp = sess.post(
                ONBASE_FULLTEXT_URL,
                json={"SearchText": term, "DocTypeID": -1, "QueryLimit": query_limit},
                timeout=timeout,
            )
            if not resp.ok:
                continue
            rows = (resp.json() or {}).get("Data") or []
        except Exception:
            continue

        for row in rows:
            doc_id = str(row.get("ID") or "")
            raw_name = str(row.get("Name") or "")
            summary = _clean_summary(str(row.get("Summary") or ""))
            cnum, title, vendor_hint = _parse_contract_name_and_vendor(raw_name, summary)
            if not cnum or not title:
                continue
            # Accept either title or summary MCDOT/FCD indicator as long as non-civil exclusions hold
            has_civil_agency = any(k in f"{raw_name} {summary}".upper() for k in ("MCDOT", "FLOOD CONTROL"))
            if not is_maricopa_civil_project(title) and not (has_civil_agency and is_maricopa_civil_project(f"{title} road")):
                continue

            kw_vendor, kw_date = keyword_meta.get(cnum, ("", ""))
            exp_title, exp_vendor, exp_date = exp_meta.get(cnum, ("", "", ""))
            vendor = kw_vendor or exp_vendor or vendor_hint
            cdate = kw_date or exp_date or _parse_mdy(summary)

            amt_match = _DOLLAR_RE.search(summary)
            amount = float(amt_match.group(1).replace(",", "")) if amt_match else None

            agency = _classify_maricopa_agency(raw_name, summary)
            source_url = (
                ONBASE_DOCUMENT_PDF_URL.format(doc_id=quote(doc_id, safe=""))
                if doc_id
                else ""
            )

            existing = by_contract.get(cnum)
            if existing is None:
                by_contract[cnum] = OnBaseContractRecord(
                    contract_num=cnum,
                    title=(title or exp_title)[:220],
                    agency=agency,
                    vendor_name=vendor,
                    contract_date=cdate,
                    amount=amount,
                    doc_id=doc_id,
                    source_url=source_url,
                    summary=summary[:400],
                )
            else:
                if not existing.vendor_name and vendor:
                    existing.vendor_name = vendor
                if not existing.contract_date and cdate:
                    existing.contract_date = cdate
                if existing.amount is None and amount is not None:
                    existing.amount = amount
                if ("County Contracts" in raw_name or "County Contracts" not in existing.summary) and doc_id:
                    existing.doc_id = existing.doc_id or doc_id

    # Also add any civil contracts found only in Expiration-Report-PDF
    for cnum, (exp_title, exp_vendor, exp_date) in exp_meta.items():
        if cnum not in by_contract:
            by_contract[cnum] = OnBaseContractRecord(
                contract_num=cnum,
                title=exp_title[:220],
                agency=_classify_maricopa_agency(exp_title, ""),
                vendor_name=exp_vendor,
                contract_date=exp_date,
                source_url=MARICOPA_EXPIRATION_PDF_URL,
            )

    records = sorted(by_contract.values(), key=lambda r: (r.contract_date, r.contract_num), reverse=True)
    return records, exp_doc_meta


def run_maricopa_onbase_sync(
    db_path: Any = None,
    *,
    limit: int = 500,
    include_expiration_pdf: bool = False,
    fetch_contract_pdfs: bool = False,
    pdf_limit: int = 120,
    dry_run: bool = False,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Ingest civil Maricopa County / MCDOT / FCD contracts from OnBase PublicAccess & Expiration Report."""
    sess = session or requests.Session()
    sess.headers.update(_BROWSER_HEADERS)
    records, exp_doc_meta = fetch_onbase_civil_contracts(
        include_expiration_pdf=include_expiration_pdf,
        session=sess,
    )

    with db_session(db_path) as conn:
        existing_nums = {
            r[0]
            for r in conn.execute(
                "SELECT agency_project_num FROM projects WHERE agency IN ('MCDOT', 'Maricopa County', 'Maricopa County FCD')"
            ).fetchall()
        }
        docs_saved_nums = {
            r[0]
            for r in conn.execute(
                """
                SELECT DISTINCT p.agency_project_num
                FROM projects p
                JOIN project_documents pd ON p.project_id = pd.project_id
                WHERE p.agency IN ('MCDOT', 'Maricopa County', 'Maricopa County FCD')
                  AND pd.sha256 IS NOT NULL
                """
            ).fetchall()
            if r[0]
        }

    ingested = 0
    docs_archived = 0
    pdfs_fetched = 0
    skipped = 0
    with_vendor = 0
    with_amount = 0
    errors: list[str] = []

    for rec in records[:limit]:
        already_in_db = rec.contract_num in existing_nums
        needs_pdf = (
            fetch_contract_pdfs
            and not dry_run
            and pdfs_fetched < pdf_limit
            and rec.doc_id
            and rec.contract_num not in docs_saved_nums
        )
        if already_in_db and not needs_pdf:
            skipped += 1
            continue

        doc_meta: dict[str, Any] | None = None
        if needs_pdf:
            pdf_url = ONBASE_DOCUMENT_PDF_URL.format(doc_id=quote(rec.doc_id, safe=""))
            try:
                pr = sess.get(pdf_url, timeout=25)
                pdfs_fetched += 1
                if pr.ok and pr.content.lstrip()[:8].startswith(b"%PDF"):
                    doc_meta = save_pdf_artifact(
                        pr.content,
                        bucket="maricopa",
                        filename_hint=f"onbase_{rec.contract_num}",
                        source_url=pdf_url,
                    )
                    if rec.amount is None:
                        parsed_amt = _extract_amount_from_pdf_bytes(pr.content)
                        if parsed_amt is not None:
                            rec.amount = parsed_amt
                    if doc_meta:
                        docs_archived += 1
                        docs_saved_nums.add(rec.contract_num)
            except Exception:
                pass

        try:
            if not dry_run:
                with db_session(db_path) as conn:
                    cid = upsert_contractor(conn, rec.vendor_name) if rec.vendor_name else None
                    pid = upsert_project(
                        conn,
                        agency=rec.agency,
                        agency_project_num=rec.contract_num,
                        title=rec.title,
                        county="Maricopa",
                        award_date=rec.contract_date or None,
                        letting_date=rec.contract_date or None,
                        winning_bid_total=rec.amount,
                        awarded_contractor_id=cid,
                        source_url=rec.source_url or MARICOPA_EXPIRATION_PDF_URL,
                    )
                    if doc_meta:
                        upsert_project_document(
                            conn,
                            project_id=pid,
                            doc_type="contract_pdf",
                            title=f"Maricopa Contract {rec.contract_num}",
                            source_url=doc_meta["source_url"],
                            archive_url=doc_meta.get("archive_url"),
                            local_path=doc_meta.get("local_path"),
                            bucket=doc_meta.get("bucket"),
                            asset_name=doc_meta.get("asset_name"),
                            sha256=doc_meta.get("sha256"),
                            file_size=doc_meta.get("file_size"),
                        )
                    elif rec.source_url == MARICOPA_EXPIRATION_PDF_URL and exp_doc_meta:
                        upsert_project_document(
                            conn,
                            project_id=pid,
                            doc_type="expiration_report_pdf",
                            title="Maricopa County Contract Expiration Report",
                            source_url=MARICOPA_EXPIRATION_PDF_URL,
                            archive_url=exp_doc_meta.get("archive_url"),
                            local_path=exp_doc_meta.get("local_path"),
                            bucket=exp_doc_meta.get("bucket"),
                            asset_name=exp_doc_meta.get("asset_name"),
                            sha256=exp_doc_meta.get("sha256"),
                            file_size=exp_doc_meta.get("file_size"),
                        )
                    elif rec.source_url:
                        upsert_project_document(
                            conn,
                            project_id=pid,
                            doc_type="onbase_record",
                            title=f"Maricopa OnBase {rec.contract_num}",
                            source_url=rec.source_url,
                        )
                    if rec.amount is not None and cid is not None:
                        upsert_project_bid(
                            conn,
                            project_id=pid,
                            contractor_id=cid,
                            is_engineers_estimate=False,
                            bid_total=rec.amount,
                            bid_rank=1,
                            is_winner=True,
                        )
            if not already_in_db:
                existing_nums.add(rec.contract_num)
                ingested += 1
            if rec.vendor_name:
                with_vendor += 1
            if rec.amount is not None:
                with_amount += 1
        except Exception as exc:
            errors.append(f"{rec.contract_num}: {exc}")

    return {
        "agency": "MCDOT",
        "source": "maricopa_onbase",
        "discovered": len(records),
        "ingested": ingested,
        "docs_archived": docs_archived,
        "skipped": skipped,
        "with_vendor": with_vendor,
        "with_amount": with_amount,
        "errors": errors,
    }
