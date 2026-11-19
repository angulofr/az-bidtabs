"""City of Mesa (Socrata Open Data) and City of Chandler (APP / Ivalua) scrapers."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

import requests
from bs4 import BeautifulSoup

from src.db import db_session, upsert_project
from src.scrapers.bonfire import is_civil_project

MESA_SOCRATA_URL = "https://citydata.mesaaz.gov/resource/dfcn-ivuc.json"
APP_AZ_BROWSE_URL = "https://app.az.gov/page.aspx/en/rfp/request_browse_public"

_MESA_EXTRA_CIVIL_KEYWORDS = (
    "CRACK SEAL",
    "SLURRY SEAL",
    "CHIP SEAL",
    "MANHOLE",
    "FIRE HYDRANT",
    "FIBER",
    "PARKING LOT",
    "AGGREGATE",
    "RIPRAP",
    "EXCAVAT",
    "STRIPING",
    "PAVEMENT",
    "UNDERGROUND",
    "CONDUIT",
    "ROUNDABOUT",
)

_CHANDLER_CIVIL_COMMODITIES = (
    "HEAVY CONSTRUCTION",
    "HIGHWAY",
    "ROAD",
    "PAVING",
    "STREET",
    "UTILITY",
    "WATER",
    "SEWER",
    "ENGINEERING",
    "EXCAVATION",
    "CONCRETE",
)


@dataclass
class MunicipalSolicitation:
    agency: str
    project_num: str
    title: str
    letting_date: str
    status: str
    url: str


def _parse_us_date(raw_date: str) -> str:
    text = (raw_date or "").strip()
    if not text:
        return ""
    for fmt in ("%m/%d/%Y %I:%M:%S %p", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return text[:10] if len(text) >= 10 else ""


def fetch_mesa_solicitations(
    limit: int = 25,
    *,
    civil_only: bool = True,
    timeout: int = 20,
    session: requests.Session | None = None,
) -> list[MunicipalSolicitation]:
    """Fetch solicitation records from City of Mesa's Socrata Open Data JSON endpoint."""
    sess = session or requests.Session()
    sess.headers.update(
        {"User-Agent": "AZ-BidTabs/1.0 (+https://azbidtabs.example; heavy-civil-intel)"}
    )

    fetch_limit = max(limit * 8, 2000) if civil_only else limit
    resp = sess.get(
        MESA_SOCRATA_URL,
        params={"$order": "creation_date DESC", "$limit": fetch_limit},
        timeout=timeout,
    )
    resp.raise_for_status()
    rows = resp.json()

    results: list[MunicipalSolicitation] = []
    seen: set[str] = set()
    for row in rows:
        proj_num = str(row.get("contract_no") or row.get("rowid") or "").strip()
        title = str(row.get("contract_description") or "").strip()
        if not proj_num or not title or proj_num in seen:
            continue
        upper_t = title.upper()
        if civil_only and not (
            is_civil_project(title)
            or any(kw in upper_t for kw in _MESA_EXTRA_CIVIL_KEYWORDS)
        ):
            continue
        seen.add(proj_num)
        cdate = str(row.get("creation_date") or "")[:10]
        results.append(
            MunicipalSolicitation(
                agency="City of Mesa",
                project_num=proj_num,
                title=title,
                letting_date=cdate,
                status=str(row.get("contract_status") or ""),
                url=f"{MESA_SOCRATA_URL}?contract_no={proj_num}",
            )
        )
        if len(results) >= limit:
            break
    return results


def fetch_chandler_solicitations(
    limit: int = 25,
    *,
    civil_only: bool = True,
    max_pages: int = 12,
    timeout: int = 25,
    session: requests.Session | None = None,
) -> list[MunicipalSolicitation]:
    """Fetch City of Chandler public solicitations from Arizona Procurement Portal (APP), paginating cleanly."""
    sess = session or requests.Session()
    sess.headers.update(
        {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    )

    init_resp = sess.get(APP_AZ_BROWSE_URL, timeout=timeout)
    init_resp.raise_for_status()
    init_soup = BeautifulSoup(init_resp.text, "html.parser")

    def _collect_form_data(page_soup: BeautifulSoup) -> dict[str, str]:
        d: dict[str, str] = {}
        for inp in page_soup.select("form input"):
            name = inp.get("name")
            if name and inp.get("type") in (None, "hidden", "text"):
                d[name] = inp.get("value") or ""
        return d

    data = _collect_form_data(init_soup)
    data["body:x:selOrgaLevelOrgaNode_78E9FF04_1"] = "act;CYC"
    data["body_x_selOrgaLevelOrgaNode_78E9FF04_1_text"] = "City of Chandler"
    data["body:x:prxFilterBar:x:cmdSearchBtn"] = "Search"

    post_resp = sess.post(APP_AZ_BROWSE_URL, data=data, timeout=timeout)
    post_resp.raise_for_status()
    soup = BeautifulSoup(post_resp.text, "html.parser")

    results: list[MunicipalSolicitation] = []
    seen: set[str] = set()

    for page_idx in range(max_pages):
        grid = soup.find("table", {"id": "body_x_grid_grd"})
        if grid is None:
            break

        data_rows = [tr for tr in grid.select("tr") if len(tr.find_all("td")) >= 8]
        if not data_rows:
            break

        new_codes_on_page = 0
        for tr in data_rows:
            tds = tr.find_all("td")
            code = tds[1].get_text(" ", strip=True)
            label = tds[2].get_text(" ", strip=True)
            pub_begin = _parse_us_date(tds[3].get_text(" ", strip=True))
            commodity = tds[4].get_text(" ", strip=True)
            status = tds[7].get_text(" ", strip=True)
            if not code or not label or code in seen:
                continue
            new_codes_on_page += 1
            seen.add(code)
            if civil_only:
                comm_upper = commodity.upper()
                if not (
                    is_civil_project(label)
                    or any(kw in comm_upper for kw in _CHANDLER_CIVIL_COMMODITIES)
                ):
                    continue
            link = tds[2].find("a", href=True) or tds[0].find("a", href=True)
            href = str(link.get("href")) if link else ""
            full_url = (
                href
                if href.startswith("http")
                else f"https://app.az.gov{href}" if href else APP_AZ_BROWSE_URL
            )
            results.append(
                MunicipalSolicitation(
                    agency="City of Chandler",
                    project_num=code,
                    title=label,
                    letting_date=pub_begin,
                    status=status,
                    url=full_url,
                )
            )
            if len(results) >= limit:
                return results

        # Stop if fewer than full 15-row page returned or no new rows seen
        if len(data_rows) < 15 or new_codes_on_page == 0 or page_idx + 1 >= max_pages:
            break

        next_idx = page_idx + 1
        pager_data = _collect_form_data(soup)
        pager_data["body:x:selOrgaLevelOrgaNode_78E9FF04_1"] = "act;CYC"
        pager_data["body_x_selOrgaLevelOrgaNode_78E9FF04_1_text"] = "City of Chandler"
        pager_data["hdnCurrentPageIndexbody_x_grid_grd"] = str(next_idx)
        pager_data["__EVENTTARGET"] = "body:x:grid:grd"
        pager_data["__EVENTARGUMENT"] = f"Page|{next_idx}"
        try:
            next_resp = sess.post(APP_AZ_BROWSE_URL, data=pager_data, timeout=timeout)
            if not next_resp.ok:
                break
            soup = BeautifulSoup(next_resp.text, "html.parser")
        except requests.RequestException:
            break

    return results


def _sync_metadata_projects(
    agency: str,
    solicitations: list[MunicipalSolicitation],
    db_path: str | Any = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    with db_session(db_path) as conn:
        existing_nums = {
            r[0]
            for r in conn.execute(
                "SELECT agency_project_num FROM projects WHERE agency = ?",
                (agency,),
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
                    agency=agency,
                    agency_project_num=sol.project_num,
                    title=sol.title,
                    county="Maricopa",
                    letting_date=sol.letting_date or None,
                    source_url=sol.url,
                )
                existing_nums.add(sol.project_num)
            new_count += 1

    return {
        "agency": agency,
        "discovered": len(solicitations),
        "ingested": new_count,
        "skipped": skipped,
    }


def run_mesa_sync(
    db_path: str | Any = None,
    *,
    limit: int = 25,
    civil_only: bool = True,
    dry_run: bool = False,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Sync City of Mesa civil solicitations from Socrata into the database."""
    sols = fetch_mesa_solicitations(limit=limit, civil_only=civil_only, session=session)
    return _sync_metadata_projects("City of Mesa", sols, db_path, dry_run=dry_run)


def run_chandler_sync(
    db_path: str | Any = None,
    *,
    limit: int = 25,
    civil_only: bool = True,
    dry_run: bool = False,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Sync City of Chandler civil solicitations from app.az.gov into the database."""
    sols = fetch_chandler_solicitations(limit=limit, civil_only=civil_only, session=session)
    return _sync_metadata_projects("City of Chandler", sols, db_path, dry_run=dry_run)
