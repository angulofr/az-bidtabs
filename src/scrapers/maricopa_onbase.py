"""Maricopa County / MCDOT / FCD OnBase PublicAccess & Expiration Report scraper.

Pulls civil/heavy-construction county contracts from:
1. Maricopa County Office of Procurement Services OnBase API (`edms.maricopa.gov/ops/api`)
   via `FullTextSearch` and `CustomQuery/KeywordSearch`.
2. Optional enrichment from `maricopa.gov/DocumentCenter/View/6444/Expiration-Report-PDF`
   when reachable within a short timeout.
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
)
from src.scrapers.maricopa_bidsync import is_maricopa_civil_project

ONBASE_FULLTEXT_URL = "https://edms.maricopa.gov/ops/api/DocumentType/FullTextSearch"
ONBASE_KEYWORD_SEARCH_URL = "https://edms.maricopa.gov/ops/api/CustomQuery/KeywordSearch"
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
    "grading",
    "intersection",
    "chip seal",
)

_CONTRACT_NAME_RE = re.compile(
    r"^OPS\s*-\s*[^-]+-\s*(\d{5,7}(?:-[A-Z0-9]+)?)\s*-\s*(.+)$",
    re.IGNORECASE,
)
_DOC_TRAILING_VENDOR_RE = re.compile(
    r"\s+-\s+(?:PRICING|CONTRACT|AWARD|AMENDMENT|MODIFICATION|RENEWAL)\s*-\s*([A-Z0-9][A-Z0-9 &.,'/-]{2,})$",
    re.IGNORECASE,
)
_SERIAL_VENDOR_RE = re.compile(
    r"^([A-Z0-9][A-Z0-9 &.,'/-]{3,65}?)\s+(?:FOR|SERIAL)\s+",
    re.IGNORECASE,
)
_DOLLAR_RE = re.compile(r"\$\s*([\d,]{3,}\.\d{2})")
_MDY_DATE_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")

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
        m_num = re.search(r"\b(\d{6}(?:-[A-Z0-9]+)?)\b", clean_name)
        if m_num:
            contract_num = m_num.group(1).upper()

    mv = _DOC_TRAILING_VENDOR_RE.search(title)
    if mv:
        candidate = mv.group(1).strip()
        if not any(
            skip in candidate.upper()
            for skip in ("PRICING", "CONTRACT", "AMENDMENT", "RENEWAL", "ATTACHMENT")
        ):
            vendor = candidate
        title = title[: mv.start()].strip()

    if not vendor and summary:
        ms = _SERIAL_VENDOR_RE.match(summary)
        if ms:
            cand = ms.group(1).strip()
            if len(cand) >= 4 and "ATTACH" not in cand.upper() and "COPY TO" not in cand.upper():
                vendor = cand

    return contract_num, title, vendor


def fetch_onbase_keyword_index(
    prefixes: tuple[str, ...] = ("26*", "25*", "24*", "23*", "22*", "21*", "20*", "19*", "18*"),
    *,
    session: requests.Session | None = None,
    timeout: int = 20,
) -> dict[str, tuple[str, str]]:
    """Query OnBase `KeywordSearch` (QueryID=270) to map contract numbers -> `(vendor, date)`."""
    sess = session or requests.Session()
    sess.headers.update(_BROWSER_HEADERS)
    by_contract: dict[str, tuple[str, str]] = {}

    for prefix in prefixes:
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
                continue
            rows = (resp.json() or {}).get("Data") or []
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
        except Exception:
            continue

    return by_contract


def fetch_expiration_report_contracts(
    *,
    session: requests.Session | None = None,
    timeout: int = 18,
) -> dict[str, tuple[str, str, str]]:
    """Best-effort parse of Maricopa `Expiration-Report-PDF` -> `{contract_num: (title, vendor, end_date)}`."""
    sess = session or requests.Session()
    sess.headers.update(_BROWSER_HEADERS)
    out: dict[str, tuple[str, str, str]] = {}
    try:
        resp = sess.get(MARICOPA_EXPIRATION_PDF_URL, timeout=timeout)
        if not resp.ok or not resp.content.startswith(b"%PDF"):
            return out
        with pdfplumber.open(io.BytesIO(resp.content)) as pdf:
            for page in pdf.pages:
                text = page.extract_text() or ""
                for line in text.splitlines():
                    m = re.match(
                        r"^\s*(\d{5,7}(?:-[A-Z0-9]+)?)\s+(.+?)\s+(\d{1,2}/\d{1,2}/\d{4})\b",
                        line.strip(),
                    )
                    if not m:
                        continue
                    cnum = m.group(1).upper()
                    rest = m.group(2).strip()
                    dt = _parse_mdy(m.group(3))
                    if is_maricopa_civil_project(rest):
                        out[cnum] = (rest, "", dt)
    except Exception:
        return out
    return out


def fetch_onbase_civil_contracts(
    *,
    search_terms: tuple[str, ...] = DEFAULT_ONBASE_CIVIL_TERMS,
    query_limit: int = 250,
    include_expiration_pdf: bool = False,
    session: requests.Session | None = None,
    timeout: int = 20,
) -> list[OnBaseContractRecord]:
    """Query OnBase `FullTextSearch` + `KeywordSearch` and return deduplicated civil contract records."""
    sess = session or requests.Session()
    sess.headers.update(_BROWSER_HEADERS)

    keyword_meta = fetch_onbase_keyword_index(session=sess, timeout=timeout)
    exp_meta = (
        fetch_expiration_report_contracts(session=sess, timeout=15)
        if include_expiration_pdf
        else {}
    )

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
            if not is_maricopa_civil_project(title):
                continue

            kw_vendor, kw_date = keyword_meta.get(cnum, ("", ""))
            exp_title, exp_vendor, exp_date = exp_meta.get(cnum, ("", "", ""))
            vendor = kw_vendor or vendor_hint or exp_vendor
            cdate = kw_date or exp_date or _parse_mdy(summary)

            amt_match = _DOLLAR_RE.search(summary)
            amount = float(amt_match.group(1).replace(",", "")) if amt_match else None

            agency = _classify_maricopa_agency(raw_name, summary)
            source_url = ONBASE_VIEWER_URL.format(doc_id=quote(doc_id, safe="")) if doc_id else ""

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

    return sorted(by_contract.values(), key=lambda r: (r.contract_date, r.contract_num), reverse=True)


def run_maricopa_onbase_sync(
    db_path: Any = None,
    *,
    limit: int = 500,
    include_expiration_pdf: bool = False,
    dry_run: bool = False,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Ingest civil Maricopa County / MCDOT / FCD contracts from OnBase PublicAccess."""
    records = fetch_onbase_civil_contracts(
        include_expiration_pdf=include_expiration_pdf,
        session=session,
    )

    with db_session(db_path) as conn:
        existing_nums = {
            r[0]
            for r in conn.execute(
                "SELECT agency_project_num FROM projects WHERE agency IN ('MCDOT', 'Maricopa County', 'Maricopa County FCD')"
            ).fetchall()
        }

    ingested = 0
    skipped = 0
    with_vendor = 0
    with_amount = 0
    errors: list[str] = []

    for rec in records[:limit]:
        if rec.contract_num in existing_nums:
            skipped += 1
            continue
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
        "skipped": skipped,
        "with_vendor": with_vendor,
        "with_amount": with_amount,
        "errors": errors,
    }
