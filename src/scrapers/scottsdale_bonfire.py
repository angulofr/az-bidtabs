"""City of Scottsdale Bonfire Public Portal scraper for past opportunities & contracts."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

from src.db import db_session, upsert_contractor, upsert_project

SCOTTSDALE_BONFIRE_PORTAL = "https://scottsdaleaz.bonfirehub.com/portal/?tab=pastOpportunities"
SCOTTSDALE_PAST_OPPORTUNITIES_URL = (
    "https://scottsdaleaz.bonfirehub.com/PublicPortal/getPastPublicOpportunitiesSectionData"
)
SCOTTSDALE_PUBLIC_CONTRACTS_URL = (
    "https://scottsdaleaz.bonfirehub.com/PublicPortal/getPublicContractsSectionData"
)

_CIVIL_KEYWORDS = (
    "ROAD",
    "STREET",
    "PAV",
    "WATER",
    "SEWER",
    "DRAIN",
    "BRIDGE",
    "TRAFFIC",
    "INTERSECTION",
    "CONSTRUCT",
    "CULVERT",
    "ASPHALT",
    "CONCRETE",
    "SIDEWALK",
    "CURB",
    "SIGNAL",
    "GRADING",
    "UTILITY",
    "STORM",
    "PIPE",
    "HIGHWAY",
    "CHANNEL",
)


@dataclass
class BonfireOpportunity:
    project_id: str
    reference_id: str
    title: str
    close_date: str
    is_public_award: bool
    url: str
    awarded_contractor: str = ""


def _is_civil_project(title: str) -> bool:
    upper = (title or "").upper()
    return any(kw in upper for kw in _CIVIL_KEYWORDS)


def _fetch_contract_vendor_lookup(
    sess: requests.Session,
    *,
    timeout: int = 20,
) -> dict[str, str]:
    """Return lookup of normalized contract prefix/name -> vendor organization name."""
    try:
        resp = sess.get(
            SCOTTSDALE_PUBLIC_CONTRACTS_URL,
            headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=timeout,
        )
        if not resp.ok:
            return {}
        payload = resp.json().get("payload", {})
    except (requests.RequestException, ValueError):
        return {}

    vendors_raw = payload.get("vendors") or []
    vendor_by_id: dict[str, str] = {}
    if isinstance(vendors_raw, dict):
        vendor_iter = vendors_raw.values()
    else:
        vendor_iter = vendors_raw
    for v in vendor_iter:
        vid = str(v.get("VendorID") or "")
        vname = str(v.get("VendorContactOrganizationName") or "").strip()
        if vid and vname:
            vendor_by_id[vid] = vname

    contracts_raw = payload.get("publicContracts") or {}
    contract_iter = contracts_raw.values() if isinstance(contracts_raw, dict) else contracts_raw
    lookup: dict[str, str] = {}
    for c in contract_iter:
        cname = str(c.get("Name") or "").strip().upper()
        vid = str(c.get("VendorID") or "")
        vname = vendor_by_id.get(vid, "")
        if not cname or not vname:
            continue
        lookup[cname] = vname
        # Bonfire contract names often start with "<REF> - <TITLE>"
        prefix = cname.split(" - ", 1)[0].strip()
        if prefix and prefix not in lookup:
            lookup[prefix] = vname
    return lookup


def _match_awarded_contractor(
    ref_id: str,
    title: str,
    contract_lookup: dict[str, str],
) -> str:
    if not contract_lookup:
        return ""
    ref_upper = (ref_id or "").strip().upper()
    title_upper = (title or "").strip().upper()
    if ref_upper and ref_upper in contract_lookup:
        return contract_lookup[ref_upper]
    if title_upper:
        for cname, vname in contract_lookup.items():
            if title_upper in cname or (ref_upper and ref_upper in cname):
                return vname
    return ""


def fetch_scottsdale_bonfire_opportunities(
    limit: int = 25,
    *,
    civil_only: bool = True,
    include_contracts: bool = True,
    timeout: int = 20,
    session: requests.Session | None = None,
) -> list[BonfireOpportunity]:
    """Fetch past opportunities from City of Scottsdale's public Bonfire JSON portal."""
    sess = session or requests.Session()
    sess.headers.update(
        {"User-Agent": "AZ-BidTabs/1.0 (+https://azbidtabs.example; heavy-civil-intel)"}
    )

    resp = sess.get(
        SCOTTSDALE_PAST_OPPORTUNITIES_URL,
        headers={"X-Requested-With": "XMLHttpRequest"},
        timeout=timeout,
    )
    resp.raise_for_status()
    payload = resp.json().get("payload", {})
    projects_raw = payload.get("projects") or {}
    proj_iter = projects_raw.values() if isinstance(projects_raw, dict) else projects_raw

    contract_lookup = (
        _fetch_contract_vendor_lookup(sess, timeout=timeout) if include_contracts else {}
    )

    opportunities: list[BonfireOpportunity] = []
    for item in proj_iter:
        proj_id = str(item.get("ProjectID") or "").strip()
        ref_id = str(item.get("ReferenceID") or proj_id).strip()
        title = str(item.get("ProjectName") or "").strip()
        close_date = str(item.get("DateClose") or "").strip()
        if not title:
            continue
        if civil_only and not _is_civil_project(title):
            continue

        awarded = _match_awarded_contractor(ref_id, title, contract_lookup)
        opportunities.append(
            BonfireOpportunity(
                project_id=proj_id,
                reference_id=ref_id,
                title=title,
                close_date=close_date,
                is_public_award=bool(item.get("IsPublicAward")),
                url=f"https://scottsdaleaz.bonfirehub.com/opportunities/{proj_id}"
                if proj_id
                else SCOTTSDALE_BONFIRE_PORTAL,
                awarded_contractor=awarded,
            )
        )

    opportunities.sort(key=lambda o: o.close_date, reverse=True)
    return opportunities[:limit]


def run_scottsdale_bonfire_sync(
    db_path: str | Any = None,
    *,
    limit: int = 25,
    civil_only: bool = True,
    dry_run: bool = False,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Fetch Scottsdale Bonfire past opportunities and record new projects."""
    opportunities = fetch_scottsdale_bonfire_opportunities(
        limit=limit,
        civil_only=civil_only,
        session=session,
    )

    with db_session(db_path) as conn:
        existing_nums = {
            r[0]
            for r in conn.execute(
                "SELECT agency_project_num FROM projects WHERE agency = 'City of Scottsdale'"
            ).fetchall()
        }
        new_count = 0
        skipped = 0
        for opp in opportunities:
            proj_num = opp.reference_id or opp.project_id
            if not proj_num or proj_num in existing_nums:
                skipped += 1
                continue
            if not dry_run:
                awarded_id = (
                    upsert_contractor(conn, opp.awarded_contractor)
                    if opp.awarded_contractor
                    else None
                )
                upsert_project(
                    conn,
                    agency="City of Scottsdale",
                    agency_project_num=proj_num,
                    title=opp.title,
                    county="Maricopa",
                    letting_date=opp.close_date[:10] if opp.close_date else None,
                    awarded_contractor_id=awarded_id,
                    source_url=opp.url,
                )
                existing_nums.add(proj_num)
            new_count += 1

    return {
        "discovered": len(opportunities),
        "ingested": new_count,
        "skipped": skipped,
    }
