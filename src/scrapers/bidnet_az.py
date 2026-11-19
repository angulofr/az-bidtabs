"""BidNet Direct Arizona scraper (Maricopa County / MCDOT / FCD, Pima County, City of Avondale).

Captures closed & awarded civil solicitations cleanly from unauthenticated BidNet Direct
public buyer portals (`bidnetdirect.com/arizona/...`, no Cloudflare/WAF).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

from src.db import db_session, upsert_project, upsert_project_document
from src.scrapers.maricopa_bidsync import is_maricopa_civil_project

BIDNET_BASE_URL = "https://www.bidnetdirect.com"

_DATE_MDY_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")
_SOL_NUM_TOKEN_RE = re.compile(r"^([A-Z]{0,5}\d{4,9}(?:[-/][A-Z0-9-]+)?)$", re.IGNORECASE)


@dataclass(frozen=True)
class BidNetPortalConfig:
    key: str
    slug: str
    agency: str
    county: str = "Maricopa"


BIDNET_AZ_PORTALS: dict[str, BidNetPortalConfig] = {
    "maricopa_county": BidNetPortalConfig(
        key="maricopa_county",
        slug="maricopacounty",
        agency="MCDOT",
        county="Maricopa",
    ),
    "maricopa_fcd": BidNetPortalConfig(
        key="maricopa_fcd",
        slug="mcfloodcontroldistrict",
        agency="Maricopa County FCD",
        county="Maricopa",
    ),
    "pima_county": BidNetPortalConfig(
        key="pima_county",
        slug="pimacounty",
        agency="Pima County",
        county="Pima",
    ),
    "avondale": BidNetPortalConfig(
        key="avondale",
        slug="cityofavondale",
        agency="City of Avondale",
        county="Maricopa",
    ),
}


@dataclass
class BidNetSolicitation:
    portal_key: str
    agency: str
    county: str
    project_num: str
    title: str
    letting_date: str
    status: str
    url: str


def _parse_bidnet_date(text: str) -> str:
    matches = _DATE_MDY_RE.findall(text or "")
    if not matches:
        return ""
    # Prefer closing / award date (usually the last MM/DD/YYYY in the card text)
    m, d, y = matches[-1]
    try:
        return datetime(int(y), int(m), int(d)).strftime("%Y-%m-%d")
    except ValueError:
        return ""


def _extract_bidnet_cards(
    html: str,
    cfg: BidNetPortalConfig,
    *,
    status_label: str,
    civil_only: bool = True,
) -> list[BidNetSolicitation]:
    soup = BeautifulSoup(html, "html.parser")
    items: list[BidNetSolicitation] = []
    seen_urls: set[str] = set()

    for link in soup.select("a.solicitation-link, a[href*='/solicitations/']"):
        href = str(link.get("href") or "")
        if "/open-bids" == href or "/closed-bids" == href or "/awarded-bids" == href:
            continue
        if not re.search(r"/solicitations/.+/\d{5,}", href):
            continue
        full_url = urljoin(BIDNET_BASE_URL, href)
        if full_url in seen_urls:
            continue
        seen_urls.add(full_url)

        title = re.sub(r"\s+", " ", link.get_text(" ", strip=True)).strip()
        if not title:
            continue

        row_el = link.find_parent(["tr", "div"])
        for _ in range(4):
            if row_el and row_el.parent and len(row_el.get_text(" ", strip=True)) < len(title) + 25:
                row_el = row_el.parent

        row_text = row_el.get_text(" | ", strip=True) if row_el else title
        parts = [p.strip() for p in row_text.split("|") if p.strip()]

        proj_num = ""
        for p in parts:
            if p != title and _SOL_NUM_TOKEN_RE.match(p) and len(p) <= 28:
                proj_num = p.upper()
                break
        if not proj_num:
            m_id = re.search(r"/(\d{7,})(?:\?|$)", full_url)
            proj_num = m_id.group(1) if m_id else ""
        if not proj_num:
            continue

        if civil_only and not is_maricopa_civil_project(title):
            continue

        agency = cfg.agency
        upper_row = row_text.upper()
        if cfg.key == "maricopa_county":
            if "FLOOD CONTROL" in upper_row:
                agency = "Maricopa County FCD"
            elif "MCDOT" in upper_row or is_maricopa_civil_project(title):
                agency = "MCDOT"

        items.append(
            BidNetSolicitation(
                portal_key=cfg.key,
                agency=agency,
                county=cfg.county,
                project_num=proj_num,
                title=title[:220],
                letting_date=_parse_bidnet_date(row_text),
                status=status_label,
                url=full_url,
            )
        )

    return items


def fetch_bidnet_portal(
    portal_key: str,
    *,
    limit: int = 250,
    max_pages_per_tab: int = 16,
    civil_only: bool = True,
    timeout: int = 20,
    session: requests.Session | None = None,
) -> list[BidNetSolicitation]:
    """Fetch closed & awarded civil solicitations from an Arizona BidNet Direct portal."""
    if portal_key not in BIDNET_AZ_PORTALS:
        raise ValueError(f"Unknown BidNet portal key: {portal_key}")
    cfg = BIDNET_AZ_PORTALS[portal_key]
    sess = session or requests.Session()
    sess.headers.update(
        {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    )

    tabs = (
        ("awarded", f"{BIDNET_BASE_URL}/arizona/{cfg.slug}/solicitations/awarded-bids"),
        ("closed", f"{BIDNET_BASE_URL}/arizona/{cfg.slug}/solicitations/closed-bids"),
    )

    results: list[BidNetSolicitation] = []
    seen_keys: set[str] = set()

    for status_label, base_tab_url in tabs:
        for page_num in range(1, max_pages_per_tab + 1):
            if len(results) >= limit:
                break
            url = (
                f"{base_tab_url}?selectedContent=BUYER"
                if page_num == 1
                else f"{base_tab_url}?pageNumber={page_num}&selectedContent=BUYER"
            )
            try:
                resp = sess.get(url, timeout=timeout)
                if not resp.ok:
                    break
            except requests.RequestException:
                break

            batch = _extract_bidnet_cards(
                resp.text,
                cfg,
                status_label=status_label,
                civil_only=civil_only,
            )
            if not batch:
                # Note: page may have all non-civil rows, check if any solicitation links remain
                soup = BeautifulSoup(resp.text, "html.parser")
                if not soup.select("a.solicitation-link"):
                    break

            for sol in batch:
                key = f"{sol.agency}:{sol.project_num}"
                if key in seen_keys:
                    continue
                seen_keys.add(key)
                results.append(sol)
                if len(results) >= limit:
                    break

            soup = BeautifulSoup(resp.text, "html.parser")
            has_next = any(
                f"pageNumber={page_num + 1}" in str(a.get("href") or "")
                for a in soup.select("a[href*='pageNumber=']")
            )
            if not has_next:
                break

    return results


def run_bidnet_portal_sync(
    portal_key: str,
    db_path: Any = None,
    *,
    limit: int = 250,
    civil_only: bool = True,
    dry_run: bool = False,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    cfg = BIDNET_AZ_PORTALS[portal_key]
    items = fetch_bidnet_portal(
        portal_key,
        limit=limit,
        civil_only=civil_only,
        session=session,
    )

    with db_session(db_path) as conn:
        existing_pairs = {
            (str(r[0]), str(r[1]))
            for r in conn.execute(
                "SELECT agency, agency_project_num FROM projects"
            ).fetchall()
        }

    ingested = 0
    skipped = 0
    for sol in items:
        if (sol.agency, sol.project_num) in existing_pairs:
            skipped += 1
            continue
        if not dry_run:
            with db_session(db_path) as conn:
                pid = upsert_project(
                    conn,
                    agency=sol.agency,
                    agency_project_num=sol.project_num,
                    title=sol.title,
                    county=sol.county,
                    letting_date=sol.letting_date or None,
                    source_url=sol.url,
                )
                upsert_project_document(
                    conn,
                    project_id=pid,
                    doc_type="bidnet_solicitation",
                    title=f"{sol.agency} {sol.project_num} ({sol.status})",
                    source_url=sol.url,
                )
        existing_pairs.add((sol.agency, sol.project_num))
        ingested += 1

    return {
        "agency": cfg.agency,
        "portal": portal_key,
        "discovered": len(items),
        "ingested": ingested,
        "skipped": skipped,
    }


def run_all_bidnet_syncs(
    db_path: Any = None,
    *,
    limit: int = 250,
    civil_only: bool = True,
    dry_run: bool = False,
    session: requests.Session | None = None,
) -> dict[str, dict[str, Any]]:
    results: dict[str, dict[str, Any]] = {}
    for key in BIDNET_AZ_PORTALS:
        try:
            results[key] = run_bidnet_portal_sync(
                key,
                db_path,
                limit=limit,
                civil_only=civil_only,
                dry_run=dry_run,
                session=session,
            )
        except Exception as exc:
            results[key] = {"agency": BIDNET_AZ_PORTALS[key].agency, "error": str(exc)}
    return results
