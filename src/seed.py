"""Deterministic Arizona heavy-civil seed dataset (210 projects, 44 pay items, 8,000+ bid lines)."""

from __future__ import annotations

import random
from datetime import date, timedelta
from pathlib import Path

from src.db import (
    DEFAULT_DB_PATH,
    db_session,
    init_db,
    upsert_contractor,
    upsert_pay_item,
    upsert_project,
)

CONTRACTORS = [
    ("Ames Construction, Inc.", "ROC123456", 1200),
    ("Pulice Construction, Inc.", "ROC109821", 450),
    ("Fann Contracting, Inc.", "ROC087432", 320),
    ("Fisher Sand & Gravel Co.", "ROC145678", 550),
    ("Achen-Gardner Construction, LLC", "ROC198234", 280),
    ("Sunland Asphalt & Construction, LLC", "ROC112233", 650),
    ("Markham Contracting Co., Inc.", "ROC134567", 180),
    ("Hunter Contracting Co.", "ROC095412", 260),
    ("Banicki Construction, Inc.", "ROC167890", 140),
    ("Combs Construction Company, Inc.", "ROC178901", 110),
    ("Granite Construction Company", "ROC078123", 1800),
    ("Kiewit Infrastructure West Co.", "ROC065432", 2500),
    ("Rummel Construction, Inc.", "ROC154321", 380),
    ("Buesing Corp.", "ROC187654", 220),
    ("Dibble & Associates Contracting, LLC", "ROC201122", 75),
    ("Cactus Asphalt / Cactus Transport, Inc.", "ROC142299", 190),
]

# 44 authentic ADOT & MAG pay items
# (spec_system, item_code, description, unit, category, base_unit_price)
PAY_ITEMS = [
    # ADOT (24 items)
    ("ADOT", "2010011", "CLEARING AND GRUBBING", "ACRE", "Earthwork & Demo", 4200.00),
    ("ADOT", "2020020", "REMOVAL OF ASPHALTIC CONCRETE PAVEMENT", "SQ.YD.", "Earthwork & Demo", 8.50),
    ("ADOT", "2020041", "REMOVAL OF CONCRETE CURB AND GUTTER", "L.FT.", "Earthwork & Demo", 12.00),
    ("ADOT", "2030301", "ROADWAY EXCAVATION", "CU.YD.", "Earthwork & Demo", 18.00),
    ("ADOT", "2030401", "DRAINAGE EXCAVATION", "CU.YD.", "Earthwork & Demo", 24.50),
    ("ADOT", "2030501", "STRUCTURAL BACKFILL", "CU.YD.", "Earthwork & Demo", 42.00),
    ("ADOT", "3030022", "AGGREGATE BASE, CLASS 2", "CU.YD.", "Base & Subbase", 48.00),
    ("ADOT", "4040072", "ASPHALT BINDER (PG 76-22 TR+)", "TON", "Paving", 780.00),
    ("ADOT", "4090003", "ASPHALTIC CONCRETE (MISC. STRUCTURAL)", "TON", "Paving", 132.00),
    ("ADOT", "4160004", "ASPHALTIC CONCRETE (END PRODUCT) (3/4 INCH MIX)", "TON", "Paving", 118.00),
    ("ADOT", "4160031", "ASPHALTIC CONCRETE FRICTION COURSE (ACFC)", "TON", "Paving", 148.00),
    ("ADOT", "5010011", "PIPE, CORRUGATED METAL, 24 INCH", "L.FT.", "Drainage & Utilities", 115.00),
    ("ADOT", "5010025", "PIPE, REINFORCED CONCRETE, 36 INCH CLASS III", "L.FT.", "Drainage & Utilities", 215.00),
    ("ADOT", "5050200", "CATCH BASIN (MAG TYPE / ADOT C-15.10)", "EACH", "Drainage & Utilities", 6800.00),
    ("ADOT", "6010002", "STRUCTURAL CONCRETE (CLASS S) (F'C = 3,500 PSI)", "CU.YD.", "Structures", 890.00),
    ("ADOT", "6010005", "STRUCTURAL CONCRETE (CLASS S) (F'C = 4,500 PSI)", "CU.YD.", "Structures", 1040.00),
    ("ADOT", "6050002", "REINFORCING STEEL", "LB", "Structures", 1.85),
    ("ADOT", "6070038", "DRILLED SHAFT FOUNDATION (48 INCH)", "L.FT.", "Structures", 620.00),
    ("ADOT", "7016030", "BARRICADE (TYPE III)", "EACH", "Traffic Control", 240.00),
    ("ADOT", "7016050", "TRAFFIC CONTROL (FLAGGING SERVICES)", "HOUR", "Traffic Control", 85.00),
    ("ADOT", "7080201", "WATERBORNE PAVEMENT MARKING (WHITE, 4 INCH)", "L.FT.", "Traffic Control", 0.48),
    ("ADOT", "7310195", "TRAFFIC SIGNAL POLE (TYPE Q)", "EACH", "Signals & Lighting", 18500.00),
    ("ADOT", "9050005", "GUARD RAIL, W-BEAM, SINGLE FACE", "L.FT.", "Roadside Safety", 44.00),
    ("ADOT", "9240010", "MOBILIZATION", "L.SUM", "General", 285000.00),
    # MAG (20 items)
    ("MAG", "105-1", "MOBILIZATION / DEMOBILIZATION", "L.SUM", "General", 145000.00),
    ("MAG", "205-1", "ROADWAY & SITE GRADING EXCAVATION", "CU.YD.", "Earthwork & Demo", 17.50),
    ("MAG", "301-1", "SUBGRADE PREPARATION", "SQ.YD.", "Base & Subbase", 4.25),
    ("MAG", "310-1", "AGGREGATE BASE COURSE (MAG ABC)", "TON", "Base & Subbase", 34.00),
    ("MAG", "321-1", "ASPHALT CONCRETE PAVEMENT (MAG 1/2 INCH MIX)", "TON", "Paving", 124.00),
    ("MAG", "321-2", "ASPHALT CONCRETE PAVEMENT (MAG 3/4 INCH BASE)", "TON", "Paving", 114.00),
    ("MAG", "336-1", "PAVEMENT MILLING (2 INCH DEPTH)", "SQ.YD.", "Paving", 5.20),
    ("MAG", "340-1", "CONCRETE CURB AND GUTTER (MAG DET. 220-1)", "L.FT.", "Concrete Flatwork", 34.00),
    ("MAG", "340-2", "CONCRETE SIDEWALK (4 INCH THICK)", "SQ.FT.", "Concrete Flatwork", 9.75),
    ("MAG", "340-3", "CONCRETE DRIVEWAY / VALLEY GUTTER", "SQ.FT.", "Concrete Flatwork", 16.50),
    ("MAG", "345-1", "ADJUST MANHOLE FRAME & COVER TO GRADE", "EACH", "Drainage & Utilities", 1350.00),
    ("MAG", "401-1", "TRAFFIC CONTROL & BARRICADING", "L.SUM", "Traffic Control", 72000.00),
    ("MAG", "610-1", "WATERLINE, 12 INCH DIP, CLASS 350", "L.FT.", "Drainage & Utilities", 165.00),
    ("MAG", "610-2", "WATERLINE, 8 INCH DIP, CLASS 350", "L.FT.", "Drainage & Utilities", 128.00),
    ("MAG", "611-1", "FIRE HYDRANT ASSEMBLY (MAG DET. 360)", "EACH", "Drainage & Utilities", 7900.00),
    ("MAG", "615-1", "SANITARY SEWER PIPE, 8 INCH PVC SDR-35", "L.FT.", "Drainage & Utilities", 112.00),
    ("MAG", "615-2", "SANITARY SEWER MANHOLE (4 FT DIA.)", "EACH", "Drainage & Utilities", 8400.00),
    ("MAG", "618-1", "STORM DRAIN PIPE, 24 INCH RCP CLASS III", "L.FT.", "Drainage & Utilities", 148.00),
    ("MAG", "618-2", "STORM DRAIN PIPE, 36 INCH RCP CLASS III", "L.FT.", "Drainage & Utilities", 235.00),
    ("MAG", "710-1", "ROADWAY EXCAVATION AND EMBANKMENT", "CU.YD.", "Earthwork & Demo", 19.50),
]

ADOT_TEMPLATES = [
    ("I-10", "Maricopa", "Central", ["Broadway Curve TI", "35th Ave to 7th St", "Chandler Blvd to Ray Rd", "Pecos Rd TI", "Verrado Way to Miller Rd", "Baseline Rd Overpass"]),
    ("Loop 101", "Maricopa", "Central", ["75th Ave to I-17 Widening", "Shea Blvd to Princess Dr", "I-17 to Pima Rd", "Thomas Rd to McKellips Rd", "Grand Ave TI"]),
    ("Loop 202", "Maricopa", "Central", ["Santan Fwy: Val Vista to Gilbert", "South Mountain Corridor", "Red Mountain: Dobson to Gilbert", "Estrella Pkwy TI"]),
    ("Loop 303", "Maricopa", "Central", ["MC-85 to Van Buren St", "I-10 TI System Interchange", "El Mirage Rd to Happy Valley"]),
    ("US 60", "Maricopa", "Central", ["Superstition Fwy Pavement Rehab", "Crismon Rd to Meridian Rd", "Wickenburg Passing Lanes", "Mill Ave Bridge Deck"]),
    ("SR 24", "Pinal", "Southcentral", ["Ellsworth Rd to Ironwood Dr", "Ironwood Dr to Meridian Rd", "Gantzel Rd Extension"]),
    ("I-17", "Yavapai", "Northcentral", ["Anthem Way to Sunset Point", "Black Canyon City Bridges", "Cordes Junction TI", "McGuireville Rest Area"]),
    ("I-10", "Pima", "Southcentral", ["Ina Rd to Ruthrauff Rd", "Prince Rd to 29th St", "Cortaro Rd Interchange", "Houghton Rd to Valencia Rd"]),
    ("I-40", "Coconino", "Northcentral", ["Flagstaff Urban Corridor", "Winona to Two Guns Rehab", "Belmont TI Bridge"]),
    ("SR 347", "Pinal", "Southcentral", ["Maricopa-Casa Grande Hwy", "Riggs Rd Overpass"]),
]

MAG_AGENCIES = [
    ("City of Phoenix", "Maricopa", "Central", ["Camelback Rd", "Deer Valley Rd", "7th Ave", "Buckeye Rd", "Bell Rd", "24th St", "Happy Valley Rd", "McDowell Rd"]),
    ("MCDOT", "Maricopa", "Central", ["Gilbert Rd", "McQueen Rd", "Olive Ave", "El Mirage Rd", "Riggs Rd", "Dove Valley Rd", "Northern Pkwy"]),
    ("City of Mesa", "Maricopa", "East", ["Signal Butte Rd", "Elliot Rd", "University Dr", "Broadway Rd", "Power Rd", "Southern Ave"]),
    ("City of Scottsdale", "Maricopa", "Northeast", ["Shea Blvd", "Frank Lloyd Wright Blvd", "Scottsdale Rd", "Hayden Rd", "Pima Rd"]),
    ("City of Chandler", "Maricopa", "Southeast", ["Lindsay Rd", "Ocotillo Rd", "Arizona Ave", "Chandler Heights Rd", "Alma School Rd"]),
    ("City of Goodyear", "Maricopa", "West", ["MC-85", "Estrella Pkwy", "Sarival Ave", "Yuma Rd", "Bullard Ave"]),
    ("Pima County", "Pima", "Tucson", ["Valencia Rd", "Ina Rd", "Houghton Rd", "Sunset Rd", "Cortaro Farms Rd", "Silverbell Rd"]),
]

MAG_PROJECT_TYPES = [
    "Complete Streets & Pavement Rehab",
    "Water Transmission Main Replacement",
    "Arterial Widening & Storm Drain",
    "Intersection & Traffic Signal Improvements",
    "Data Center Utility & Roadway Corridor",
    "Bridge Approaches & Channel Lining",
    "Sewer Trunk Line & Street Reconstruction",
]


def seed_database(db_path: Path | str = DEFAULT_DB_PATH, *, force: bool = False) -> bool:
    """Seed SQLite database with 210 realistic Arizona heavy-civil projects (2024–2026)."""
    init_db(db_path)
    with db_session(db_path) as conn:
        existing = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        if existing >= 100 and not force:
            # Backfill approx_employees if migrating from an older schema
            for name, roc, emp in CONTRACTORS:
                upsert_contractor(conn, name, roc, emp)
            return False

        if force or existing > 0:
            conn.execute("DELETE FROM bid_lines")
            conn.execute("DELETE FROM projects")
            conn.execute("DELETE FROM pay_items")
            conn.execute("DELETE FROM contractors")

        rng = random.Random(2026)

        contractor_ids = [
            upsert_contractor(conn, name, roc, emp) for name, roc, emp in CONTRACTORS
        ]
        item_ids = [
            upsert_pay_item(conn, spec, code, desc, unit, cat)
            for spec, code, desc, unit, cat, _ in PAY_ITEMS
        ]

        adot_indices = [i for i, p in enumerate(PAY_ITEMS) if p[0] == "ADOT"]
        mag_indices = [i for i, p in enumerate(PAY_ITEMS) if p[0] == "MAG"]

        contractor_bias = {cid: rng.uniform(0.91, 1.08) for cid in contractor_ids}

        # Generate 210 projects: 90 ADOT + 120 Municipal/MAG across 2024-01-15 to 2026-11-05
        start_date = date(2024, 1, 15)
        total_days = (date(2026, 11, 5) - start_date).days

        bid_line_rows: list[tuple] = []

        for p_num in range(1, 211):
            offset_days = int((p_num / 210.0) * total_days) + rng.randint(-10, 10)
            offset_days = max(0, min(total_days, offset_days))
            let_dt = start_date + timedelta(days=offset_days)
            awd_dt = let_dt + timedelta(days=rng.randint(14, 28))
            # ~3.5% annual escalation from 2024 to 2026
            inflation = 1.0 + 0.035 * (offset_days / 365.25)

            if p_num <= 90:
                spec = "ADOT"
                agency = "ADOT"
                route, county, district, corridors = rng.choice(ADOT_TEMPLATES)
                corridor = rng.choice(corridors)
                proj_code = f"F{380 + p_num:03d}01C"
                title = f"{route}: {corridor}"
                # Always include Mobilization (23) + Roadway Exc (3) + AC (9) + 6-9 other ADOT items
                core = [3, 9, 23]
                pool = [i for i in adot_indices if i not in core]
                chosen_indices = core + rng.sample(pool, k=rng.randint(6, 9))
                source_url = "https://azdot.gov/business/contracts-and-specifications/bid-tabulations"
            else:
                spec = "MAG"
                agency, county, district, streets = rng.choice(MAG_AGENCIES)
                street = rng.choice(streets)
                ptype = rng.choice(MAG_PROJECT_TYPES)
                prefix = agency.split()[-1][:3].upper()
                proj_code = f"{prefix}-{let_dt.year % 100:02d}-{p_num:03d}"
                title = f"{street}: {ptype}"
                route = street
                core = [24, 28, 31]  # MAG Mob, AC 1/2", Curb & Gutter
                pool = [i for i in mag_indices if i not in core]
                chosen_indices = core + rng.sample(pool, k=rng.randint(6, 9))
                source_url = "https://procurement.opengov.com/portal/phoenix"

            scale = rng.uniform(0.35, 3.1)
            proj_items = []
            ee_total = 0.0

            for idx in chosen_indices:
                _, _, _, unit, _, base_price = PAY_ITEMS[idx]
                if unit == "L.SUM":
                    qty = 1.0
                    ee_up = round(base_price * scale * inflation * rng.uniform(0.85, 1.2), 2)
                else:
                    raw_qty = {
                        "SQ.YD.": rng.uniform(3000, 55000) * scale,
                        "SQ.FT.": rng.uniform(4500, 42000) * scale,
                        "CU.YD.": rng.uniform(800, 45000) * scale,
                        "TON": rng.uniform(1200, 32000) * scale,
                        "L.FT.": rng.uniform(400, 12000) * scale,
                        "LB": rng.uniform(18000, 320000) * scale,
                        "EACH": rng.uniform(8, 95) * scale,
                        "ACRE": rng.uniform(4, 65) * scale,
                        "HOUR": rng.uniform(120, 1800) * scale,
                    }.get(unit, 1000 * scale)
                    qty = max(1.0, round(raw_qty, 0))
                    scale_discount = max(0.75, 1.10 - 0.12 * min(scale, 2.6))
                    ee_up = round(
                        base_price * scale_discount * inflation * rng.uniform(0.92, 1.08),
                        2,
                    )

                ext = round(qty * ee_up, 2)
                ee_total += ext
                proj_items.append((item_ids[idx], qty, ee_up, ext))

            num_bidders = rng.choice([4, 5, 6])
            bidders = rng.sample(contractor_ids, k=num_bidders)

            bidder_submissions = []
            for cid in bidders:
                bias = contractor_bias[cid]
                proj_noise = rng.uniform(0.93, 1.09)
                lines = []
                total = 0.0
                for item_id, qty, ee_up, _ in proj_items:
                    item_noise = rng.uniform(0.84, 1.18)
                    up = round(ee_up * bias * proj_noise * item_noise, 2)
                    ext = round(qty * up, 2)
                    total += ext
                    lines.append((item_id, qty, up, ext))
                bidder_submissions.append((cid, round(total, 2), lines))

            bidder_submissions.sort(key=lambda x: x[1])
            winner_cid, winning_total, _ = bidder_submissions[0]

            project_id = upsert_project(
                conn,
                agency=agency,
                agency_project_num=proj_code,
                title=title,
                route_or_location=route,
                county=county,
                district=district,
                letting_date=let_dt.isoformat(),
                award_date=awd_dt.isoformat(),
                engineers_estimate=round(ee_total, 2),
                winning_bid_total=winning_total,
                awarded_contractor_id=winner_cid,
                source_url=source_url,
            )

            for item_id, qty, ee_up, ext in proj_items:
                bid_line_rows.append(
                    (project_id, None, 1, item_id, qty, ee_up, ext, None, 0)
                )

            for rank_idx, (cid, _, lines) in enumerate(bidder_submissions, start=1):
                is_win = 1 if rank_idx == 1 else 0
                for item_id, qty, up, ext in lines:
                    bid_line_rows.append(
                        (project_id, cid, 0, item_id, qty, up, ext, rank_idx, is_win)
                    )

        conn.executemany(
            """
            INSERT INTO bid_lines (
                project_id, contractor_id, is_engineers_estimate, item_id,
                quantity, unit_price, extended_total, bid_rank, is_winner
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            bid_line_rows,
        )

    return True
