"""Generalized Bonfire Public Portal scraper for Arizona municipalities.

Supports unauthenticated JSON endpoints exposed by Bonfire municipal portals:
- City of Scottsdale (`scottsdaleaz.bonfirehub.com`)
- City of Goodyear (`goodyearaz.bonfirehub.com`)
- Town of Paradise Valley (`paradisevalleyaz.bonfirehub.com`)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import requests

from src.db import db_session, upsert_contractor, upsert_project


@dataclass(frozen=True)
class BonfirePortalConfig:
    key: str
    agency: str
    base_url: str
    county: str = "Maricopa"

    @property
    def portal_url(self) -> str:
        return f"{self.base_url}/portal/?tab=pastOpportunities"

    @property
    def past_opportunities_url(self) -> str:
        return f"{self.base_url}/PublicPortal/getPastPublicOpportunitiesSectionData"

    @property
    def public_contracts_url(self) -> str:
        return f"{self.base_url}/PublicPortal/getPublicContractsSectionData"


BONFIRE_PORTALS: dict[str, BonfirePortalConfig] = {
    "scottsdale": BonfirePortalConfig(
        key="scottsdale",
        agency="City of Scottsdale",
        base_url="https://scottsdaleaz.bonfirehub.com",
        county="Maricopa",
    ),
    "goodyear": BonfirePortalConfig(
        key="goodyear",
        agency="City of Goodyear",
        base_url="https://goodyearaz.bonfirehub.com",
        county="Maricopa",
    ),
    "paradise_valley": BonfirePortalConfig(
        key="paradise_valley",
        agency="Town of Paradise Valley",
        base_url="https://paradisevalleyaz.bonfirehub.com",
        county="Maricopa",
    ),
}

_CIVIL_KEYWORDS = (
    "ROAD",
    "STREET",
    "PARKWAY",
    "AVENUE",
    "BOULEVARD",
    "BLVD",
    "MEDIAN",
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
    "RECLAMATION",
    "RESERVOIR",
    "CMAR",
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
    agency: str = ""


def _is_civil_project(title: str) -> bool:
    upper = (title or "").upper()
    return any(kw in upper for kw in _CIVIL_KEYWORDS)


def _get_portal_config(portal_key: str) -> BonfirePortalConfig:
    if portal_key not in BONFIRE_PORTALS:
        known = ", ".join(sorted(BONFIRE_PORTALS))
        raise ValueError(f"Unknown Bonfire portal {portal_key!r}. Expected one of: {known}")
    return BONFIRE_PORTALS[portal_key]


def _fetch_contract_vendor_lookup(
    sess: requests.Session,
    public_contracts_url: str,
    *,
    timeout: int = 20,
) -> dict[str, str]:
    """Return lookup of normalized contract prefix/name -> vendor organization name."""
    try:
        resp = sess.get(
            public_contracts_url,
            headers={"X-Requested-With": "XMLHttpRequest"},
            timeout=timeout,
        )
        if not resp.ok:
            return {}
        payload = resp.json().get("payload", {})
    except (requests.RequestException, ValueError):
        return {}

    vendors_raw = payload.get("vendors") or []
    vendor_iter = vendors_raw.values() if isinstance(vendors_raw, dict) else vendors_raw
    vendor_by_id: dict[str, str] = {}
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


def fetch_bonfire_opportunities(
    portal_key: str,
    limit: int = 25,
    *,
    civil_only: bool = True,
    include_contracts: bool = True,
    timeout: int = 20,
    session: requests.Session | None = None,
) -> list[BonfireOpportunity]:
    """Fetch past opportunities from a configured Arizona Bonfire public portal."""
    cfg = _get_portal_config(portal_key)
    sess = session or requests.Session()
    sess.headers.update(
        {"User-Agent": "AZ-BidTabs/1.0 (+https://azbidtabs.example; heavy-civil-intel)"}
    )

    resp = sess.get(
        cfg.past_opportunities_url,
        headers={"X-Requested-With": "XMLHttpRequest"},
        timeout=timeout,
    )
    resp.raise_for_status()
    payload = resp.json().get("payload", {})
    projects_raw = payload.get("projects") or {}
    proj_iter = projects_raw.values() if isinstance(projects_raw, dict) else projects_raw

    contract_lookup = (
        _fetch_contract_vendor_lookup(sess, cfg.public_contracts_url, timeout=timeout)
        if include_contracts
        else {}
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
                url=f"{cfg.base_url}/opportunities/{proj_id}" if proj_id else cfg.portal_url,
                awarded_contractor=awarded,
                agency=cfg.agency,
            )
        )

    opportunities.sort(key=lambda o: o.close_date, reverse=True)
    return opportunities[:limit]


def run_bonfire_sync(
    portal_key: str,
    db_path: str | Any = None,
    *,
    limit: int = 25,
    civil_only: bool = True,
    dry_run: bool = False,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Fetch Bonfire past opportunities for `portal_key` and record new projects."""
    cfg = _get_portal_config(portal_key)
    opportunities = fetch_bonfire_opportunities(
        portal_key,
        limit=limit,
        civil_only=civil_only,
        session=session,
    )

    with db_session(db_path) as conn:
        existing_nums = {
            r[0]
            for r in conn.execute(
                "SELECT agency_project_num FROM projects WHERE agency = ?",
                (cfg.agency,),
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
                    agency=cfg.agency,
                    agency_project_num=proj_num,
                    title=opp.title,
                    county=cfg.county,
                    letting_date=opp.close_date[:10] if opp.close_date else None,
                    awarded_contractor_id=awarded_id,
                    source_url=opp.url,
                )
                existing_nums.add(proj_num)
            new_count += 1

    return {
        "agency": cfg.agency,
        "discovered": len(opportunities),
        "ingested": new_count,
        "skipped": skipped,
    }


def run_all_bonfire_syncs(
    db_path: str | Any = None,
    *,
    limit: int = 25,
    civil_only: bool = True,
    dry_run: bool = False,
    session: requests.Session | None = None,
) -> dict[str, dict[str, Any]]:
    """Sync all configured Arizona Bonfire municipal portals."""
    results: dict[str, dict[str, Any]] = {}
    for key in BONFIRE_PORTALS:
        try:
            results[key] = run_bonfire_sync(
                key,
                db_path,
                limit=limit,
                civil_only=civil_only,
                dry_run=dry_run,
                session=session,
            )
        except Exception as exc:
            results[key] = {"agency": BONFIRE_PORTALS[key].agency, "error": str(exc)}
    return results
