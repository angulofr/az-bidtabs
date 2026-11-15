"""Deterministic Arizona heavy-civil seed dataset for AZ BidTabs MVP."""

from __future__ import annotations

import random
from pathlib import Path

from src.db import (
    DEFAULT_DB_PATH,
    db_session,
    init_db,
    insert_bid_line,
    upsert_contractor,
    upsert_pay_item,
    upsert_project,
)

CONTRACTORS = [
    ("Ames Construction, Inc.", "ROC123456"),
    ("Pulice Construction, Inc.", "ROC109821"),
    ("Fann Contracting, Inc.", "ROC087432"),
    ("Fisher Sand & Gravel Co.", "ROC145678"),
    ("Achen-Gardner Construction, LLC", "ROC198234"),
    ("Sunland Asphalt & Construction, LLC", "ROC112233"),
    ("Markham Contracting Co., Inc.", "ROC134567"),
    ("Hunter Contracting Co.", "ROC095412"),
    ("Banicki Construction, Inc.", "ROC167890"),
    ("Combs Construction Company, Inc.", "ROC178901"),
]

# (spec_system, item_code, description, unit, category, base_unit_price)
PAY_ITEMS = [
    ("ADOT", "2020020", "REMOVAL OF ASPHALTIC CONCRETE PAVEMENT", "SQ.YD.", "Earthwork & Demo", 8.50),
    ("ADOT", "2030301", "ROADWAY EXCAVATION", "CU.YD.", "Earthwork & Demo", 18.00),
    ("ADOT", "2030401", "DRAINAGE EXCAVATION", "CU.YD.", "Earthwork & Demo", 24.50),
    ("ADOT", "3030022", "AGGREGATE BASE, CLASS 2", "CU.YD.", "Base & Subbase", 48.00),
    ("ADOT", "4090003", "ASPHALTIC CONCRETE (MISC. STRUCTURAL)", "TON", "Paving", 132.00),
    ("ADOT", "4160004", "ASPHALTIC CONCRETE (END PRODUCT) (3/4 INCH MIX)", "TON", "Paving", 118.00),
    ("ADOT", "5010011", "PIPE, CORRUGATED METAL, 24 INCH", "L.FT.", "Drainage & Utilities", 115.00),
    ("ADOT", "6010002", "STRUCTURAL CONCRETE (CLASS S) (F'C = 3,500 PSI)", "CU.YD.", "Structures", 890.00),
    ("ADOT", "6050002", "REINFORCING STEEL", "LB", "Structures", 1.85),
    ("ADOT", "7016030", "BARRICADE (TYPE III)", "EACH", "Traffic Control", 240.00),
    ("ADOT", "9240010", "MOBILIZATION", "L.SUM", "General", 285000.00),
    ("MAG", "310-1", "SUBGRADE PREPARATION", "SQ.YD.", "Base & Subbase", 4.25),
    ("MAG", "321-1", "ASPHALT CONCRETE PAVEMENT (MAG 1/2 INCH MIX)", "TON", "Paving", 124.00),
    ("MAG", "340-1", "CONCRETE CURB AND GUTTER (MAG DET. 220-1)", "L.FT.", "Concrete Flatwork", 34.00),
    ("MAG", "340-2", "CONCRETE SIDEWALK (4 INCH THICK)", "SQ.FT.", "Concrete Flatwork", 9.75),
    ("MAG", "610-1", "WATERLINE, 12 INCH DIP, CLASS 350", "L.FT.", "Drainage & Utilities", 165.00),
    ("MAG", "615-1", "SANITARY SEWER PIPE, 8 INCH PVC SDR-35", "L.FT.", "Drainage & Utilities", 112.00),
    ("MAG", "618-1", "STORM DRAIN PIPE, 24 INCH RCP CLASS III", "L.FT.", "Drainage & Utilities", 148.00),
    ("MAG", "710-1", "ROADWAY EXCAVATION AND EMBANKMENT", "CU.YD.", "Earthwork & Demo", 19.50),
    ("MAG", "105-1", "MOBILIZATION / DEMOBILIZATION", "L.SUM", "General", 145000.00),
]

PROJECTS = [
    # ADOT Projects
    ("ADOT", "F045101C", "I-10: Broadway Curve TI Reconstruction Phase 2", "I-10 MP 152-155", "Maricopa", "Central", "2026-08-14", "2026-09-02", "ADOT", [0, 1, 2, 3, 5, 7, 8, 9, 10]),
    ("ADOT", "F051201C", "Loop 101 (Agua Fria): 75th Ave to I-17 Widening", "SR 101L MP 14-22", "Maricopa", "Central", "2026-09-11", "2026-09-28", "ADOT", [0, 1, 3, 4, 5, 6, 9, 10]),
    ("ADOT", "F038901C", "SR 24: Ellsworth Rd to Ironwood Dr Interim", "SR 24 MP 2-6", "Maricopa", "Central", "2026-06-19", "2026-07-08", "ADOT", [1, 2, 3, 5, 6, 7, 10]),
    ("ADOT", "F061001C", "I-17: Anthem Way to Sunset Point Flex Lanes", "I-17 MP 229-244", "Yavapai", "Northcentral", "2026-05-22", "2026-06-11", "ADOT", [0, 1, 3, 4, 5, 7, 8, 10]),
    ("ADOT", "F049801C", "I-10: Ina Rd to Ruthrauff Rd Corridor", "I-10 MP 248-252", "Pima", "Southcentral", "2026-07-24", "2026-08-12", "ADOT", [0, 1, 2, 3, 5, 7, 9, 10]),
    ("ADOT", "F057701C", "US 60: Superstition Fwy Pavement Rehab", "US 60 MP 180-191", "Maricopa", "Central", "2026-10-09", "2026-10-25", "ADOT", [0, 3, 4, 5, 9, 10]),
    # Municipal / MAG Projects
    ("City of Phoenix", "ST85100422", "Camelback Rd Complete Streets: 24th St to 44th St", "Camelback Rd", "Maricopa", "Central", "2026-09-18", "2026-10-05", "MAG", [11, 12, 13, 14, 15, 17, 18, 19]),
    ("City of Phoenix", "WS85500119", "Deer Valley Water Transmission Main Replacement", "19th Ave & Deer Valley", "Maricopa", "North", "2026-08-07", "2026-08-26", "MAG", [11, 12, 13, 15, 16, 18, 19]),
    ("MCDOT", "TT0645", "Gilbert Rd Bridge & Approaches at Salt River", "Gilbert Rd", "Maricopa", "East", "2026-07-10", "2026-07-29", "MAG", [11, 12, 13, 17, 18, 19]),
    ("City of Mesa", "CP1128", "Signal Butte Rd Widening: Elliot to Germann", "Signal Butte Rd", "Maricopa", "East", "2026-10-02", "2026-10-20", "MAG", [11, 12, 13, 14, 15, 17, 18, 19]),
    ("City of Scottsdale", "S2409", "Shea Blvd & 90th St Intersection Improvements", "Shea Blvd", "Maricopa", "Northeast", "2026-06-26", "2026-07-14", "MAG", [11, 12, 13, 14, 17, 18, 19]),
    ("City of Chandler", "ST2405", "Lindsay Rd Improvements: Ocotillo to Hunt Hwy", "Lindsay Rd", "Maricopa", "Southeast", "2026-08-28", "2026-09-15", "MAG", [11, 12, 13, 14, 15, 16, 18, 19]),
    ("City of Goodyear", "ENG-26-014", "MC-85 & Estrella Pkwy Data Center Utility Corridor", "MC-85 / Estrella Pkwy", "Maricopa", "West", "2026-09-25", "2026-10-12", "MAG", [11, 12, 15, 16, 17, 18, 19]),
    ("Pima County", "4DOT-INA-26", "Valencia Rd Extension: Wade Rd to Ajo Hwy", "Valencia Rd", "Pima", "Tucson", "2026-07-31", "2026-08-19", "MAG", [11, 12, 13, 14, 17, 18, 19]),
]


def seed_database(db_path: Path | str = DEFAULT_DB_PATH, *, force: bool = False) -> bool:
    """Seed SQLite database with realistic Arizona heavy-civil bid tabs if empty.

    Returns True if seeding was performed, False if DB already had projects.
    """
    init_db(db_path)
    with db_session(db_path) as conn:
        existing = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        if existing > 0 and not force:
            return False

        if force:
            conn.execute("DELETE FROM bid_lines")
            conn.execute("DELETE FROM projects")
            conn.execute("DELETE FROM pay_items")
            conn.execute("DELETE FROM contractors")

        rng = random.Random(42)

        contractor_ids = [upsert_contractor(conn, name, roc) for name, roc in CONTRACTORS]
        item_ids = [
            upsert_pay_item(conn, spec, code, desc, unit, cat)
            for spec, code, desc, unit, cat, _ in PAY_ITEMS
        ]

        # Give each contractor a slight pricing bias so competitor intel is meaningful
        contractor_bias = {
            cid: rng.uniform(0.92, 1.07) for cid in contractor_ids
        }

        for p_idx, (agency, proj_num, title, route, county, district, let_dt, awd_dt, spec, item_indices) in enumerate(PROJECTS):
            # Scale factor for project size (0.4x to 2.8x)
            scale = rng.uniform(0.45, 2.75)

            # Determine quantities for each item in this project
            proj_items = []
            ee_total = 0.0
            for idx in item_indices:
                _, _, _, unit, _, base_price = PAY_ITEMS[idx]
                if unit == "L.SUM":
                    qty = 1.0
                    ee_up = round(base_price * scale * rng.uniform(0.9, 1.15), 2)
                else:
                    raw_qty = {
                        "SQ.YD.": rng.uniform(4000, 48000) * scale,
                        "SQ.FT.": rng.uniform(6000, 35000) * scale,
                        "CU.YD.": rng.uniform(1200, 38000) * scale,
                        "TON": rng.uniform(1800, 26000) * scale,
                        "L.FT.": rng.uniform(650, 9500) * scale,
                        "LB": rng.uniform(25000, 280000) * scale,
                        "EACH": rng.uniform(12, 85) * scale,
                    }.get(unit, 1000 * scale)
                    qty = round(raw_qty, 0)
                    # Economy of scale: larger quantities have slightly lower unit prices
                    scale_discount = max(0.78, 1.08 - 0.12 * min(scale, 2.5))
                    ee_up = round(base_price * scale_discount * rng.uniform(0.94, 1.06), 2)

                ext = round(qty * ee_up, 2)
                ee_total += ext
                proj_items.append((item_ids[idx], qty, ee_up, ext))

            # Pick 4-5 bidders for this project
            num_bidders = rng.choice([4, 5])
            bidders = rng.sample(contractor_ids, k=num_bidders)

            bidder_submissions = []
            for cid in bidders:
                bias = contractor_bias[cid]
                proj_noise = rng.uniform(0.94, 1.08)
                lines = []
                total = 0.0
                for item_id, qty, ee_up, _ in proj_items:
                    item_noise = rng.uniform(0.86, 1.16)
                    up = round(ee_up * bias * proj_noise * item_noise, 2)
                    ext = round(qty * up, 2)
                    total += ext
                    lines.append((item_id, qty, up, ext))
                bidder_submissions.append((cid, round(total, 2), lines))

            # Rank bidders by total bid
            bidder_submissions.sort(key=lambda x: x[1])
            winner_cid, winning_total, _ = bidder_submissions[0]

            source_url = (
                "https://azdot.gov/business/contracts-and-specifications/bid-tabulations"
                if agency == "ADOT"
                else "https://procurement.opengov.com/portal/phoenix"
            )

            project_id = upsert_project(
                conn,
                agency=agency,
                agency_project_num=proj_num,
                title=title,
                route_or_location=route,
                county=county,
                district=district,
                letting_date=let_dt,
                award_date=awd_dt,
                engineers_estimate=round(ee_total, 2),
                winning_bid_total=winning_total,
                awarded_contractor_id=winner_cid,
                source_url=source_url,
            )

            # Insert Engineer's Estimate lines
            for item_id, qty, ee_up, ext in proj_items:
                insert_bid_line(
                    conn,
                    project_id=project_id,
                    contractor_id=None,
                    is_engineers_estimate=True,
                    item_id=item_id,
                    quantity=qty,
                    unit_price=ee_up,
                    extended_total=ext,
                    bid_rank=None,
                    is_winner=False,
                )

            # Insert Bidder lines
            for rank_idx, (cid, _, lines) in enumerate(bidder_submissions, start=1):
                is_win = rank_idx == 1
                for item_id, qty, up, ext in lines:
                    insert_bid_line(
                        conn,
                        project_id=project_id,
                        contractor_id=cid,
                        is_engineers_estimate=False,
                        item_id=item_id,
                        quantity=qty,
                        unit_price=up,
                        extended_total=ext,
                        bid_rank=rank_idx,
                        is_winner=is_win,
                    )

    return True
