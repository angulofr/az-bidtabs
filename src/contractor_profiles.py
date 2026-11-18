"""Publicly estimated employee counts & AZ ROC license profiles for AZ heavy-civil contractors.

Neither the Arizona Department of Revenue nor the Arizona Registrar of Contractors
publishes company employee headcounts publicly. These values represent approximate
company workforce sizes compiled from public corporate disclosures and industry
directories, and are backfilled onto matching normalized contractor names.
"""

from __future__ import annotations

from pathlib import Path

# Canonical normalized contractor name -> (az_roc_license_or_None, approx_employees)
CONTRACTOR_PROFILES: dict[str, tuple[str | None, int]] = {
    # Major ADOT & statewide heavy-civil contractors
    "ACHEN GARDNER CONSTRUCTION": (None, 180),
    "AJP ELECTRIC": (None, 40),
    "AMES CONSTRUCTION": (None, 1500),
    "ARCHER WESTERN CONSTRUCTION": (None, 8000),
    "B AND F CONTRACTING": (None, 380),
    "BLUCOR CONTRACTING": (None, 85),
    "BUESING": (None, 180),
    "CACTUS ASPHALT": (None, 120),
    "CACTUS ASPHALT A DIVISION OF CACTUS TRANSPORT": (None, 120),
    "CACTUS TRANSPORT": (None, 120),
    "CHASSE BUILDING TEAM": (None, 150),
    "CHOLLA PAVEMENT MAINTENANCE": (None, 45),
    "COFFMAN SPECIALTIES": (None, 350),
    "COLLIERS ENGINEERING AND DESIGN": (None, 2200),
    "COMBS CONSTRUCTION": (None, 90),
    "CORE CONSTRUCTION": (None, 1500),
    "CRAFCO": (None, 350),
    "CS CONSTRUCTION": (None, 120),
    "DBA CONSTRUCTION": (None, 140),
    "DCS CONTRACTING": (None, 140),
    "DIBBLE AND ASSOCIATES CONTRACTING": (None, 75),
    "FANN CONTRACTING": (None, 250),
    "FISHER SAND AND GRAVEL": (None, 350),
    "FNF CONSTRUCTION": (None, 500),
    "GRANITE CONSTRUCTION": (None, 5800),
    "HAYDON COMPANIES": (None, 420),
    "HITT CONTRACTING": (None, 1700),
    "HUNTER CONTRACTING": (None, 450),
    "J BANICKI CONSTRUCTION": (None, 120),
    "KIEWIT INFRASTRUCTURE WEST": (None, 2500),
    "KIMLEY HORN AND ASSOCIATES": (None, 11000),
    "M R TANNER DEVELOPMENT AND CONSTRUCTION": (None, 100),
    "MARKHAM CONTRACTING": (None, 250),
    "MGC CONTRACTORS": (None, 250),
    "NESBITT CONTRACTING": (None, 200),
    "PAVECO": (None, 35),
    "PULICE CONSTRUCTION": (None, 350),
    "REGIONAL PAVEMENT MAINTENANCE OF ARIZONA": (None, 35),
    "RUMMEL CONSTRUCTION": (None, 650),
    "SEMA CONSTRUCTION": (None, 450),
    "SOUTHWEST ASPHALT": (None, 150),
    "SOUTHWEST CONCRETE PAVING": (None, 150),
    "STANLEY CONSULTANTS": (None, 1100),
    "SUNDT CONSTRUCTION": (None, 3750),
    "SUNDT RUMMEL A JOINT VENTURE": (None, 650),
    "SUNLAND ASPHALT": (None, 750),
    "SUNLAND ASPHALT AND CONSTRUCTION": (None, 750),
    "VIASUN": (None, 175),
}

# Prefix aliases for common corporate name variations / division suffixes
_PREFIX_ALIASES: tuple[tuple[str, str], ...] = (
    ("AMES CONSTRUCTION", "AMES CONSTRUCTION"),
    ("ARCHER WESTERN", "ARCHER WESTERN CONSTRUCTION"),
    ("BANICKI", "J BANICKI CONSTRUCTION"),
    ("J BANICKI", "J BANICKI CONSTRUCTION"),
    ("BUESING", "BUESING"),
    ("CACTUS ASPHALT", "CACTUS ASPHALT"),
    ("CACTUS TRANSPORT", "CACTUS TRANSPORT"),
    ("CHASSE BUILDING", "CHASSE BUILDING TEAM"),
    ("COFFMAN SPECIALTIES", "COFFMAN SPECIALTIES"),
    ("COMBS CONSTRUCTION", "COMBS CONSTRUCTION"),
    ("CORE CONSTRUCTION", "CORE CONSTRUCTION"),
    ("CS CONSTRUCTION", "CS CONSTRUCTION"),
    ("DBA CONSTRUCTION", "DBA CONSTRUCTION"),
    ("DCS CONTRACTING", "DCS CONTRACTING"),
    ("FANN CONTRACTING", "FANN CONTRACTING"),
    ("FISHER SAND AND GRAVEL", "FISHER SAND AND GRAVEL"),
    ("FNF CONSTRUCTION", "FNF CONSTRUCTION"),
    ("GRANITE CONSTRUCTION", "GRANITE CONSTRUCTION"),
    ("HAYDON", "HAYDON COMPANIES"),
    ("HUNTER CONTRACTING", "HUNTER CONTRACTING"),
    ("KIEWIT", "KIEWIT INFRASTRUCTURE WEST"),
    ("KIMLEY HORN", "KIMLEY HORN AND ASSOCIATES"),
    ("M R TANNER", "M R TANNER DEVELOPMENT AND CONSTRUCTION"),
    ("MARKHAM CONTRACTING", "MARKHAM CONTRACTING"),
    ("MGC CONTRACTORS", "MGC CONTRACTORS"),
    ("NESBITT CONTRACTING", "NESBITT CONTRACTING"),
    ("PULICE CONSTRUCTION", "PULICE CONSTRUCTION"),
    ("REGIONAL PAVEMENT", "REGIONAL PAVEMENT MAINTENANCE OF ARIZONA"),
    ("RUMMEL CONSTRUCTION", "RUMMEL CONSTRUCTION"),
    ("SEMA CONSTRUCTION", "SEMA CONSTRUCTION"),
    ("SOUTHWEST ASPHALT", "SOUTHWEST ASPHALT"),
    ("SOUTHWEST CONCRETE PAVING", "SOUTHWEST CONCRETE PAVING"),
    ("STANLEY CONSULTANTS", "STANLEY CONSULTANTS"),
    ("SUNDT CONSTRUCTION", "SUNDT CONSTRUCTION"),
    ("SUNLAND ASPHALT", "SUNLAND ASPHALT"),
    ("VIASUN", "VIASUN"),
)


def lookup_contractor_profile(canonical_name: str) -> tuple[str | None, int | None]:
    """Return (az_roc_license, approx_employees) for a canonical contractor name."""
    key = (canonical_name or "").upper().strip()
    if not key:
        return (None, None)
    if key in CONTRACTOR_PROFILES:
        return CONTRACTOR_PROFILES[key]
    for prefix, target in _PREFIX_ALIASES:
        if key.startswith(prefix):
            return CONTRACTOR_PROFILES[target]
    return (None, None)


def backfill_contractor_profiles(db_path: Path | str | None = None) -> int:
    """Populate missing approx_employees / az_roc_license on existing contractors rows."""
    from src.db import db_session

    updated = 0
    with db_session(db_path) as conn:
        rows = conn.execute(
            """
            SELECT contractor_id, canonical_name, az_roc_license, approx_employees
            FROM contractors
            WHERE approx_employees IS NULL OR az_roc_license IS NULL
            """
        ).fetchall()
        for cid, cname, existing_roc, existing_emp in rows:
            prof_roc, prof_emp = lookup_contractor_profile(str(cname or ""))
            new_roc = existing_roc or prof_roc
            new_emp = existing_emp if existing_emp is not None else prof_emp
            if new_roc != existing_roc or new_emp != existing_emp:
                conn.execute(
                    """
                    UPDATE contractors
                    SET az_roc_license = ?, approx_employees = ?
                    WHERE contractor_id = ?
                    """,
                    (new_roc, int(new_emp) if new_emp is not None else None, int(cid)),
                )
                updated += 1
    return updated
