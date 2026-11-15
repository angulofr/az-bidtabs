"""City of Phoenix OpenGov Procurement scraper for MAG-spec bid tabulations."""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Any

import pandas as pd
import requests
from bs4 import BeautifulSoup

from src.normalize import normalize_item_code, normalize_unit
from src.scrapers.adot import ParsedBidLine, ParsedProjectBidTab

PHOENIX_OPENGOV_PORTAL = "https://procurement.opengov.com/portal/phoenix"
PHOENIX_OPENGOV_API = "https://procurement.opengov.com/api/v1/portals/phoenix/projects"


@dataclass
class OpenGovSolicitation:
    project_num: str
    title: str
    status: str
    due_date: str
    url: str


def fetch_phoenix_opengov_solicitations(
    limit: int = 25,
    *,
    timeout: int = 20,
    session: requests.Session | None = None,
) -> list[OpenGovSolicitation]:
    """Fetch recent solicitations from City of Phoenix OpenGov Procurement portal."""
    sess = session or requests.Session()
    sess.headers.update(
        {"User-Agent": "AZ-BidTabs/1.0 (+https://azbidtabs.example; heavy-civil-intel)"}
    )

    solicitations: list[OpenGovSolicitation] = []
    try:
        resp = sess.get(PHOENIX_OPENGOV_API, params={"limit": limit}, timeout=timeout)
        if resp.ok and "application/json" in resp.headers.get("content-type", ""):
            data = resp.json()
            items = data if isinstance(data, list) else data.get("projects", data.get("data", []))
            for item in items[:limit]:
                solicitations.append(
                    OpenGovSolicitation(
                        project_num=str(item.get("number") or item.get("id") or ""),
                        title=str(item.get("title") or item.get("name") or ""),
                        status=str(item.get("status") or ""),
                        due_date=str(item.get("dueDate") or item.get("closeDate") or ""),
                        url=f"{PHOENIX_OPENGOV_PORTAL}/projects/{item.get('id', '')}",
                    )
                )
            return solicitations
    except requests.RequestException:
        pass

    # HTML fallback
    resp = sess.get(PHOENIX_OPENGOV_PORTAL, timeout=timeout)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    for card in soup.select("a[href*='/projects/']")[:limit]:
        href = card.get("href", "")
        text = card.get_text(" ", strip=True)
        if text:
            solicitations.append(
                OpenGovSolicitation(
                    project_num=href.rstrip("/").split("/")[-1],
                    title=text,
                    status="POSTED",
                    due_date="",
                    url=href if href.startswith("http") else f"https://procurement.opengov.com{href}",
                )
            )
    return solicitations


def parse_opengov_bid_tab_export(
    file_bytes: bytes,
    filename: str,
    *,
    agency: str = "City of Phoenix",
    project_num: str = "PHX-IMPORT",
    title: str = "City of Phoenix MAG Bid Tabulation",
    county: str = "Maricopa",
) -> ParsedProjectBidTab:
    """Parse an OpenGov bid tabulation CSV or Excel export into MAG-spec bid lines."""
    if filename.lower().endswith((".xlsx", ".xls")):
        df = pd.read_excel(io.BytesIO(file_bytes))
    else:
        df = pd.read_csv(io.BytesIO(file_bytes))

    df.columns = [str(c).strip().lower() for c in df.columns]
    parsed = ParsedProjectBidTab(
        agency=agency,
        agency_project_num=project_num,
        title=title,
        county=county,
        spec_system="MAG",
        source_url=PHOENIX_OPENGOV_PORTAL,
    )

    col_code = next((c for c in df.columns if "code" in c or "item" in c), None)
    col_desc = next((c for c in df.columns if "desc" in c or "name" in c), None)
    col_unit = next((c for c in df.columns if "unit" in c and "price" not in c), None)
    col_qty = next((c for c in df.columns if "qty" in c or "quant" in c), None)
    col_bidder = next((c for c in df.columns if "vendor" in c or "bidder" in c or "contractor" in c), None)
    col_price = next((c for c in df.columns if "unit price" in c or "unit_price" in c or "price" in c), None)

    if not (col_code and col_qty and col_price):
        return parsed

    for _, row in df.iterrows():
        code = normalize_item_code(str(row.get(col_code, "")))
        if not code:
            continue
        desc = str(row.get(col_desc, code)) if col_desc else code
        unit = normalize_unit(str(row.get(col_unit, "EACH"))) if col_unit else "EACH"
        qty = float(row.get(col_qty, 0) or 0)
        up = float(row.get(col_price, 0) or 0)
        bidder = str(row.get(col_bidder, "LOW BIDDER")) if col_bidder else "LOW BIDDER"
        is_ee = "ENGINEER" in bidder.upper()
        parsed.lines.append(
            ParsedBidLine(
                item_code=code,
                description=desc,
                unit=unit,
                quantity=qty,
                unit_price=up,
                extended_total=round(qty * up, 2),
                bidder_name=bidder,
                is_engineers_estimate=is_ee,
                bid_rank=None if is_ee else 1,
                is_winner=not is_ee,
            )
        )
    return parsed


def run_phoenix_opengov_sync(
    db_path: str | Any = None,
    *,
    limit: int = 25,
    dry_run: bool = False,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Fetch recent City of Phoenix OpenGov solicitations and record new projects."""
    from src.db import DEFAULT_DB_PATH, db_session, upsert_project

    target_db = db_path or DEFAULT_DB_PATH
    solicitations = fetch_phoenix_opengov_solicitations(limit=limit, session=session)

    with db_session(target_db) as conn:
        existing_nums = {
            r[0]
            for r in conn.execute(
                "SELECT agency_project_num FROM projects WHERE agency = 'City of Phoenix'"
            ).fetchall()
        }
        new_count = 0
        skipped = 0
        for sol in solicitations:
            if not sol.project_num or sol.project_num in existing_nums:
                skipped += 1
                continue
            if not dry_run:
                upsert_project(
                    conn,
                    agency="City of Phoenix",
                    agency_project_num=sol.project_num,
                    title=sol.title or f"City of Phoenix {sol.project_num}",
                    county="Maricopa",
                    letting_date=sol.due_date[:10] if sol.due_date else None,
                    source_url=sol.url,
                )
            new_count += 1

    return {
        "discovered": len(solicitations),
        "ingested": new_count,
        "skipped": skipped,
    }

