"""Backwards-compatible City of Scottsdale wrapper over `src.scrapers.bonfire`."""

from __future__ import annotations

from typing import Any

import requests

from src.scrapers.bonfire import (
    BONFIRE_PORTALS,
    BonfireOpportunity,
    _is_civil_project,
    fetch_bonfire_opportunities,
    run_bonfire_sync,
)

_SCOTTSDALE = BONFIRE_PORTALS["scottsdale"]
SCOTTSDALE_BONFIRE_PORTAL = _SCOTTSDALE.portal_url
SCOTTSDALE_PAST_OPPORTUNITIES_URL = _SCOTTSDALE.past_opportunities_url
SCOTTSDALE_PUBLIC_CONTRACTS_URL = _SCOTTSDALE.public_contracts_url


def fetch_scottsdale_bonfire_opportunities(
    limit: int = 25,
    *,
    civil_only: bool = True,
    include_contracts: bool = True,
    timeout: int = 20,
    session: requests.Session | None = None,
) -> list[BonfireOpportunity]:
    """Fetch past opportunities from City of Scottsdale's public Bonfire JSON portal."""
    return fetch_bonfire_opportunities(
        "scottsdale",
        limit=limit,
        civil_only=civil_only,
        include_contracts=include_contracts,
        timeout=timeout,
        session=session,
    )


def run_scottsdale_bonfire_sync(
    db_path: str | Any = None,
    *,
    limit: int = 25,
    civil_only: bool = True,
    dry_run: bool = False,
    session: requests.Session | None = None,
) -> dict[str, Any]:
    """Fetch Scottsdale Bonfire past opportunities and record new projects."""
    res = run_bonfire_sync(
        "scottsdale",
        db_path,
        limit=limit,
        civil_only=civil_only,
        dry_run=dry_run,
        session=session,
    )
    return {
        "discovered": res["discovered"],
        "ingested": res["ingested"],
        "skipped": res["skipped"],
    }


__all__ = [
    "BONFIRE_PORTALS",
    "BonfireOpportunity",
    "SCOTTSDALE_BONFIRE_PORTAL",
    "SCOTTSDALE_PAST_OPPORTUNITIES_URL",
    "SCOTTSDALE_PUBLIC_CONTRACTS_URL",
    "_is_civil_project",
    "fetch_scottsdale_bonfire_opportunities",
    "run_scottsdale_bonfire_sync",
]
