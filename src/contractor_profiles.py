"""Verified AZ ROC licenses, workforce estimates, and corporate metadata for AZ contractors.

Neither the Arizona Department of Revenue nor the Arizona Registrar of Contractors
publishes employee headcounts publicly. Employee counts represent approximate
workforce sizes from public corporate disclosures and industry directories;
AZ ROC license numbers, headquarters, websites, and specialties reflect public
corporate and state registry records.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ContractorProfile:
    az_roc_license: str | None = None
    approx_employees: int | None = None
    hq_location: str | None = None
    website: str | None = None
    specialty: str | None = None


_PROFILES: dict[str, ContractorProfile] = {
    "ACHEN GARDNER CONSTRUCTION": ContractorProfile(
        "ROC261745", 180, "Chandler, AZ", "https://achen.com",
        "Heavy civil contracting, wet utilities, water/wastewater facilities, and roadway infrastructure",
    ),
    "AECOM": ContractorProfile(
        None, 52500, "Dallas, TX", "https://aecom.com",
        "Multinational infrastructure engineering, design, and construction management consulting",
    ),
    "AGS SAFETY AND SUPPLY": ContractorProfile(
        None, 20, "Phoenix, AZ", "https://www.ritzsafety.com",
        "Industrial safety supplies, PPE, and work-zone safety distribution (acquired by Ritz Safety)",
    ),
    "AJP ELECTRIC": ContractorProfile(
        "ROC110352", 40, "Phoenix, AZ", None,
        "Commercial/residential electrical contracting, underground utilities, and roadway electrical/signage",
    ),
    "ALR CONSTRUCTION": ContractorProfile(
        "ROC132561", 175, "Phoenix, AZ", "https://alrconstructionaz.com",
        "Heavy civil site development, grading, paving, concrete, and underground utilities",
    ),
    "AM SIGNAL": ContractorProfile(
        None, 35, "Centennial, CO", "https://www.amsignal.com",
        "Traffic signal control equipment and Intelligent Transportation Systems (ITS) distribution",
    ),
    "AMES CONSTRUCTION": ContractorProfile(
        "ROC074995", 1500, "Burnsville, MN", "https://www.amesconstruction.com",
        "Heavy civil, highway/transportation, earthmoving, mining, and industrial construction",
    ),
    "ARCHER WESTERN CONSTRUCTION": ContractorProfile(
        None, 8000, "Chicago, IL", "https://www.walshgroup.com",
        "Heavy civil transportation, bridges, water/wastewater, and general contracting (Walsh Group)",
    ),
    "ARIZONA MATERIALS": ContractorProfile(
        None, None, "Phoenix, AZ", "https://www.arizonamaterials.com",
        "Ready-mix concrete and construction aggregate supply",
    ),
    "AZTEC ENGINEERING GROUP": ContractorProfile(
        None, 125, "Phoenix, AZ", "https://www.typsa.us",
        "Civil, transportation, bridge, and structural engineering consulting (TYPSA, Inc.)",
    ),
    "B AND F CONTRACTING": ContractorProfile(
        "ROC089744", 380, "Phoenix, AZ", "https://bfcontracting.com",
        "Underground wet/dry utilities, utility rehabilitation, traffic signals, and street lighting",
    ),
    "BLACK AND VEATCH": ContractorProfile(
        None, 10000, "Overland Park, KS", "https://www.bv.com",
        "Global engineering, procurement, and construction (EPC) for water, power, and infrastructure",
    ),
    "BLUCOR CONTRACTING": ContractorProfile(
        "ROC084121", 85, "Mesa, AZ", "https://blucor.com",
        "Heavy civil infrastructure, grading, wet/dry utilities, paving, concrete, and CIPP",
    ),
    "BUESING": ContractorProfile(
        "ROC200645", 180, "Phoenix, AZ", "https://www.buesingcorp.com",
        "Civil site development, earth retention/shoring, foundations, and site concrete",
    ),
    "CACTUS ASPHALT": ContractorProfile(
        "ROC194430", 120, "Tolleson, AZ", "https://cactusasphalt.com",
        "Asphalt paving, roadway milling, chip/crack/seal coating, and pavement preservation",
    ),
    "CEMEX": ContractorProfile(
        None, 40000, "Houston, TX", "https://www.cemexusa.com",
        "Ready-mix concrete, aggregates, and cement production and supply",
    ),
    "CENTERLINE SUPPLY WEST": ContractorProfile(
        None, 200, "Phoenix, AZ", "https://translineinc.com",
        "Traffic control products, roadway signage, and pavement marking supply (Transline Industries)",
    ),
    "CHASSE BUILDING TEAM": ContractorProfile(
        "ROC237943", 150, "Tempe, AZ", "https://chasse.us",
        "Commercial general contracting for education, healthcare, municipal, and multifamily buildings",
    ),
    "CHOLLA PAVEMENT MAINTENANCE": ContractorProfile(
        None, 45, "Phoenix, AZ", None,
        "Asphalt maintenance, sealcoating, striping, and pavement preservation",
    ),
    "COFFMAN SPECIALTIES": ContractorProfile(
        "ROC124827", 350, "San Diego, CA", "https://www.coffmanspecialties.com",
        "Heavy civil highway, airfield, bridge, structural concrete, and concrete paving",
    ),
    "COLLIERS ENGINEERING AND DESIGN": ContractorProfile(
        None, 2200, "Holmdel, NJ", "https://colliersengineering.com",
        "Multidisciplinary civil engineering, transportation, survey, and municipal design",
    ),
    "COMBS CONSTRUCTION": ContractorProfile(
        "ROC100380", 90, "Phoenix, AZ", "https://combsaz.com",
        "Asphalt paving, heavy highway, airport runway, and municipal civil construction",
    ),
    "CORE CONSTRUCTION": ContractorProfile(
        "ROC199920", 1500, "Frisco, TX", "https://coreconstruction.com",
        "Commercial and institutional general contracting (education, healthcare, municipal)",
    ),
    "CRAFCO": ContractorProfile(
        None, 350, "Phoenix, AZ", "https://crafco.com",
        "Pavement preservation materials (crack sealants, mastics, sealcoats) and application equipment",
    ),
    "CS CONSTRUCTION": ContractorProfile(
        "ROC071662", 120, "Phoenix, AZ", "https://www.csconstructionaz.com",
        "Concrete structures, broadband, traffic signals, street lighting, and ITS",
    ),
    "DBA CONSTRUCTION": ContractorProfile(
        "ROC125006", 140, "Phoenix, AZ", "https://www.dbaconstruction.net",
        "Heavy civil earthwork, concrete structures, paving, transportation, and flood control",
    ),
    "DCS CONTRACTING": ContractorProfile(
        "ROC110612", 140, "Chandler, AZ", "https://www.dcscontracting.com",
        "Heavy civil site development, roadway improvements, grading, paving, utilities, and concrete",
    ),
    "DIBBLE AND ASSOCIATES CONTRACTING": ContractorProfile(
        None, 75, "Phoenix, AZ", None,
        "Civil infrastructure & municipal site contracting",
    ),
    "FANN CONTRACTING": ContractorProfile(
        "ROC071520", 250, "Prescott, AZ", "https://fanncontracting.com",
        "Heavy civil highways, bridges, aviation paving, utilities, earthwork, and aggregate supply",
    ),
    "FISHER SAND AND GRAVEL": ContractorProfile(
        None, 350, "Dickinson, ND", "https://www.fisherind.com",
        "Aggregate mining/crushing, sand and gravel, asphalt paving, ready-mix concrete, and earthwork",
    ),
    "FNF CONSTRUCTION": ContractorProfile(
        "ROC069374", 500, "Tempe, AZ", "https://fnfinc.com",
        "Heavy-highway construction, bridges/structural concrete, asphalt paving, and earthwork",
    ),
    "FPS CIVIL": ContractorProfile(
        None, 30, "Phoenix, AZ", "https://fpscivil.com",
        "Civil underground utility construction, excavation, grading, and municipal infrastructure",
    ),
    "GRANITE CONSTRUCTION": ContractorProfile(
        "ROC069529", 5800, "Watsonville, CA", "https://www.graniteconstruction.com",
        "Heavy civil transportation, highways, bridges, water infrastructure, and construction materials",
    ),
    "HANDLEY PRECAST SYSTEMS": ContractorProfile(
        None, None, "Phoenix, AZ", "https://handleyprecast.com",
        "Precast concrete products and utility vault manufacturing",
    ),
    "HAYDON COMPANIES": ContractorProfile(
        "ROC330740", 420, "Tempe, AZ", "https://haydon.com",
        "Heavy civil, transportation, water/wastewater, building, and landscape construction",
    ),
    "HITT CONTRACTING": ContractorProfile(
        None, 1700, "Falls Church, VA", "https://www.hitt.com",
        "Commercial general contracting and critical infrastructure construction",
    ),
    "HUNTER CONTRACTING": ContractorProfile(
        "ROC070961", 450, "Gilbert, AZ", "https://www.huntercontracting.com",
        "Heavy civil, highways/bridges, water/wastewater, underground utilities, and flood control",
    ),
    "INTERMOUNTAIN WEST CIVIL CONST": ContractorProfile(
        "ROC194358", 30, "Mesa, AZ", "https://www.imwcc.com",
        "Heavy civil construction, general contracting, and commercial asphalt/pavement maintenance",
    ),
    "J BANICKI CONSTRUCTION": ContractorProfile(
        "ROC091410", 120, "Phoenix, AZ", "https://www.banicki.com",
        "Heavy civil horizontal infrastructure, paving, grading, wet utilities, and concrete structures",
    ),
    "KIEWIT INFRASTRUCTURE WEST": ContractorProfile(
        None, 2500, "Omaha, NE", "https://www.kiewit.com",
        "Large-scale heavy civil transportation, bridges, transit, water/wastewater, and utilities",
    ),
    "KIMLEY HORN AND ASSOCIATES": ContractorProfile(
        None, 11000, "Raleigh, NC", "https://www.kimley-horn.com",
        "Multidisciplinary civil, transportation, traffic, roadway, water, and land development engineering",
    ),
    "KINKAID CIVIL CONSTRUCTION": ContractorProfile(
        "ROC299009", None, "Mesa, AZ", "https://www.kinkaidcivil.com",
        "Heavy civil construction and underground utility installation",
    ),
    "LINCOLN CONSTRUCTORS": ContractorProfile(
        "ROC310165", None, "Phoenix, AZ", "https://lci-az.com",
        "Heavy civil construction, concrete, materials hauling, and infrastructure support",
    ),
    "M R TANNER DEVELOPMENT AND CONSTRUCTION": ContractorProfile(
        "ROC111576", 100, "Gilbert, AZ", "https://www.mrtanner.com",
        "Road construction, asphalt paving, grading, concrete, aggregate placement, and pavement maintenance",
    ),
    "MARKHAM CONTRACTING": ContractorProfile(
        "ROC072454", 250, "Phoenix, AZ", "https://www.markhamcontracting.com",
        "Heavy civil grading, excavation, asphalt paving, and wet underground utilities",
    ),
    "MGC CONTRACTORS": ContractorProfile(
        "ROC069949", 250, "Phoenix, AZ", "https://mgccontractors.com",
        "Water and wastewater treatment plants, pump stations, pipelines, and wells",
    ),
    "NESBITT CONTRACTING": ContractorProfile(
        "ROC074063", 200, "Tempe, AZ", "https://www.nesbittcontracting.com",
        "Heavy civil roadway construction, asphalt/concrete paving, milling, drainage, and utilities",
    ),
    "NGU CONTRACTING": ContractorProfile(
        None, None, "Glendale, AZ", None,
        "General engineering, earthwork, asphalt paving, and portable aggregate/hot plant operations",
    ),
    "PAVECO": ContractorProfile(
        None, 35, "Phoenix, AZ", "https://pavecoaz.com",
        "Asphalt paving, concrete, earthwork, and transportation infrastructure construction",
    ),
    "PULICE CONSTRUCTION": ContractorProfile(
        "ROC071463", 350, "Scottsdale, AZ", "https://www.pulice.com",
        "Heavy civil highways, bridges, airport infrastructure, concrete paving, and water systems",
    ),
    "REDPOINT CONTRACTING": ContractorProfile(
        "ROC265009", 125, "Phoenix, AZ", "https://redpointcontracting.com",
        "Heavy civil and underground utility construction",
    ),
    "REGIONAL PAVEMENT MAINTENANCE OF ARIZONA": ContractorProfile(
        "ROC267126", 35, "Phoenix, AZ", None,
        "Asphalt paving, seal coating, grading, and pavement maintenance (ceased operations)",
    ),
    "RUMMEL CONSTRUCTION": ContractorProfile(
        "ROC114846", 650, "Scottsdale, AZ", "https://www.rummelconstruction.com",
        "Heavy civil mass grading, excavation, road/highway construction, and utility installation",
    ),
    "S AND S PAVING AND CONSTRUCTION": ContractorProfile(
        None, None, "Phoenix, AZ", "https://sspaving.com",
        "Asphalt paving, pavement maintenance, earthwork, site concrete, and underground utilities",
    ),
    "SDB": ContractorProfile(
        "ROC361604", 600, "Tempe, AZ", "https://sdb.com",
        "Commercial general contracting, civil/site infrastructure, and advanced technology construction",
    ),
    "SEALMASTER AZ": ContractorProfile(
        None, None, "Phoenix, AZ", "https://smarizona.com",
        "Pavement sealers, crack sealants, and asphalt maintenance products manufacturing and supply",
    ),
    "SEMA CONSTRUCTION": ContractorProfile(
        "ROC090039", 450, "Centennial, CO", "https://sema.inc",
        "Heavy civil highways, roads, bridges, railroads, water systems, and earthwork",
    ),
    "SOUTHWEST ASPHALT": ContractorProfile(
        None, 150, "Phoenix, AZ", "https://www.fisherind.com",
        "Asphalt paving and production (division of Fisher Industries)",
    ),
    "SOUTHWEST CONCRETE PAVING": ContractorProfile(
        None, 150, "El Mirage, AZ", "https://swcp.us",
        "Heavy civil concrete paving for highways, bridges, and military airfields",
    ),
    "STANDARD CONSTRUCTION": ContractorProfile(
        "ROC230441", None, "Avondale, AZ", "https://standardconstructionco.com",
        "Public works, street improvements, underground utilities, and drainage",
    ),
    "STANLEY CONSULTANTS": ContractorProfile(
        None, 1100, "Muscatine, IA", "https://www.stanleyconsultants.com",
        "Multidisciplinary engineering, transportation, water/wastewater, energy, and environmental consulting",
    ),
    "STANTEC CONSULTING SERVICES": ContractorProfile(
        None, 33000, "Edmonton, AB, Canada", "https://www.stantec.com",
        "Multidisciplinary civil engineering, transportation, water, and environmental consulting",
    ),
    "SUNDT CONSTRUCTION": ContractorProfile(
        "ROC068012", 3750, "Tempe, AZ", "https://www.sundt.com",
        "Heavy civil transportation, concrete, water/wastewater, commercial building, and industrial",
    ),
    "SUNDT RUMMEL A JOINT VENTURE": ContractorProfile(
        "ROC068012", 650, "Tempe, AZ", "https://www.sundt.com",
        "Joint venture between Sundt Construction and Rummel Construction for major heavy civil projects",
    ),
    "SUNLAND ASPHALT": ContractorProfile(
        "ROC111922", 750, "Phoenix, AZ", "https://sunlandasphalt.com",
        "Asphalt paving, milling, overlays, chip sealing, crack sealing, and pavement maintenance",
    ),
    "T AND T CONSTRUCTION": ContractorProfile(
        "ROC068061", None, "Fountain Hills, AZ", "https://www.ttconstruction.com",
        "Underground wet utilities, water/wastewater pipelines, storm drains, and pump stations",
    ),
    "TEAM FISHEL": ContractorProfile(
        "ROC097026", 2500, "Columbus, OH", "https://www.teamfishel.com",
        "Utility engineering, telecommunications, electric, and underground utility construction",
    ),
    "TRAFFICADE SIGNS AND SALES": ContractorProfile(
        "ROC246031", 300, "Phoenix, AZ", "http://www.trafficade.com",
        "Traffic control services, work-zone safety equipment, and signage fabrication",
    ),
    "VIASUN": ContractorProfile(
        "ROC297383", 175, "Phoenix, AZ", "https://www.viasuncorp.com",
        "Pavement preservation, mill/overlay, civil concrete flatwork, aviation, and utilities",
    ),
    "WESCO DISTRIBUTION": ContractorProfile(
        None, 20000, "Pittsburgh, PA", "https://www.wesco.com",
        "Electrical, communications, and utility supply distribution",
    ),
    "WILLDAN ENGINEERING": ContractorProfile(
        None, 1400, "Anaheim, CA", "https://www.willdan.com",
        "Municipal engineering, civil infrastructure, public works, and energy consulting",
    ),
    "WRIGHT ASPHALT PRODUCTS": ContractorProfile(
        None, 30, "Houston, TX", "https://wrightasphalt.com",
        "Asphalt binders and tire rubber modified asphalt cement (TRMAC) supply",
    ),
    "WSP USA ENVIRONMENT AND INFRASTRUCTURE": ContractorProfile(
        None, 75000, "New York, NY", "https://www.wsp.com",
        "Multidisciplinary engineering, environmental, transportation, and infrastructure consulting",
    ),
    "ZUMAR INDUSTRIES": ContractorProfile(
        None, 70, "Tacoma, WA", "https://www.zumar.com",
        "MUTCD traffic signs, sign posts, bollards, and pavement markings manufacturing and supply",
    ),
}

# Backwards-compatible tuple view
CONTRACTOR_PROFILES: dict[str, tuple[str | None, int | None]] = {
    k: (v.az_roc_license, v.approx_employees) for k, v in _PROFILES.items()
}

# Prefix aliases for common corporate name variations / division suffixes
_PREFIX_ALIASES: tuple[tuple[str, str], ...] = (
    ("ACHEN GARDNER", "ACHEN GARDNER CONSTRUCTION"),
    ("AECOM", "AECOM"),
    ("AMES CONSTRUCTION", "AMES CONSTRUCTION"),
    ("ARCHER WESTERN", "ARCHER WESTERN CONSTRUCTION"),
    ("AZTEC ENGINEERING", "AZTEC ENGINEERING GROUP"),
    ("B AND F CONTRACTING", "B AND F CONTRACTING"),
    ("BANICKI", "J BANICKI CONSTRUCTION"),
    ("J BANICKI", "J BANICKI CONSTRUCTION"),
    ("BLACK AND VEATCH", "BLACK AND VEATCH"),
    ("BLUCOR", "BLUCOR CONTRACTING"),
    ("BUESING", "BUESING"),
    ("CACTUS ASPHALT", "CACTUS ASPHALT"),
    ("CACTUS TRANSPORT", "CACTUS ASPHALT"),
    ("CEMEX", "CEMEX"),
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
    ("FPS CIVIL", "FPS CIVIL"),
    ("GRANITE CONSTRUCTION", "GRANITE CONSTRUCTION"),
    ("HAYDON", "HAYDON COMPANIES"),
    ("HUNTER CONTRACTING", "HUNTER CONTRACTING"),
    ("INTERMOUNTAIN WEST CIVIL", "INTERMOUNTAIN WEST CIVIL CONST"),
    ("KIEWIT", "KIEWIT INFRASTRUCTURE WEST"),
    ("KIMLEY HORN", "KIMLEY HORN AND ASSOCIATES"),
    ("KINKAID CIVIL", "KINKAID CIVIL CONSTRUCTION"),
    ("LINCOLN CONSTRUCTORS", "LINCOLN CONSTRUCTORS"),
    ("M R TANNER", "M R TANNER DEVELOPMENT AND CONSTRUCTION"),
    ("MARKHAM CONTRACTING", "MARKHAM CONTRACTING"),
    ("MGC CONTRACTORS", "MGC CONTRACTORS"),
    ("NESBITT CONTRACTING", "NESBITT CONTRACTING"),
    ("PULICE CONSTRUCTION", "PULICE CONSTRUCTION"),
    ("REDPOINT CONTRACTING", "REDPOINT CONTRACTING"),
    ("REGIONAL PAVEMENT", "REGIONAL PAVEMENT MAINTENANCE OF ARIZONA"),
    ("RUMMEL CONSTRUCTION", "RUMMEL CONSTRUCTION"),
    ("SDB", "SDB"),
    ("SEMA CONSTRUCTION", "SEMA CONSTRUCTION"),
    ("SOUTHWEST ASPHALT", "SOUTHWEST ASPHALT"),
    ("SOUTHWEST CONCRETE PAVING", "SOUTHWEST CONCRETE PAVING"),
    ("STANDARD CONSTRUCTION", "STANDARD CONSTRUCTION"),
    ("STANLEY CONSULTANTS", "STANLEY CONSULTANTS"),
    ("STANTEC", "STANTEC CONSULTING SERVICES"),
    ("SUNDT CONSTRUCTION", "SUNDT CONSTRUCTION"),
    ("SUNLAND ASPHALT", "SUNLAND ASPHALT"),
    ("TEAM FISHEL", "TEAM FISHEL"),
    ("TRAFFICADE", "TRAFFICADE SIGNS AND SALES"),
    ("VIASUN", "VIASUN"),
    ("WESCO", "WESCO DISTRIBUTION"),
    ("WILLDAN", "WILLDAN ENGINEERING"),
    ("WSP USA", "WSP USA ENVIRONMENT AND INFRASTRUCTURE"),
)


def _resolve_profile(canonical_name: str) -> ContractorProfile | None:
    key = (canonical_name or "").upper().strip()
    if not key:
        return None
    if key in _PROFILES:
        return _PROFILES[key]
    for prefix, target in _PREFIX_ALIASES:
        if key.startswith(prefix):
            return _PROFILES[target]
    return None


def lookup_contractor_profile(canonical_name: str) -> tuple[str | None, int | None]:
    """Return (az_roc_license, approx_employees) for a canonical contractor name."""
    prof = _resolve_profile(canonical_name)
    if prof is None:
        return (None, None)
    return (prof.az_roc_license, prof.approx_employees)


def get_contractor_metadata(canonical_name: str) -> dict[str, Any]:
    """Return rich public profile metadata dict for a canonical contractor name."""
    prof = _resolve_profile(canonical_name)
    if prof is None:
        return {}
    return {
        "az_roc_license": prof.az_roc_license,
        "approx_employees": prof.approx_employees,
        "hq_location": prof.hq_location,
        "website": prof.website,
        "specialty": prof.specialty,
    }


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
