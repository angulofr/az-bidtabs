from pathlib import Path

from src.db import (
    compute_unit_price_stats,
    get_contractor_scorecard,
    get_head_to_head,
    get_overview_stats,
    query_pay_item_observations,
)
from src.seed import seed_database


def test_seed_and_analytics(tmp_path: Path):
    db_path = tmp_path / "test_bidtabs.db"
    assert seed_database(db_path) is True
    # Second call without force should be a no-op
    assert seed_database(db_path) is False

    overview = get_overview_stats(db_path)
    assert overview["projects"] == 210
    assert overview["contractors"] == 16
    assert overview["pay_items"] == 44
    assert overview["bid_lines"] > 6000

    obs = query_pay_item_observations(
        db_path,
        search_query="2030301",
        spec_system="ADOT",
        exclude_engineers_estimate=True,
    )
    assert not obs.empty
    stats = compute_unit_price_stats(obs)
    assert stats["observations"] == len(obs)
    assert stats["p25"] <= stats["median"] <= stats["p75"]
    assert stats["qty_weighted_avg"] > 0

    scorecard = get_contractor_scorecard(db_path)
    assert len(scorecard) == 16
    assert scorecard["wins"].sum() == 210

    c_names = scorecard["contractor_name"].tolist()
    proj_h2h, line_h2h = get_head_to_head(c_names[0], c_names[1], db_path)
    # Deterministic seed should have overlapping projects or at least return valid DataFrames
    assert proj_h2h is not None
    assert line_h2h is not None


def test_postgres_helpers_and_empty_db(tmp_path: Path):
    from src.db import _adapt_sql, init_db, is_postgres

    assert is_postgres("postgresql://user:pass@localhost:5432/db") is True
    assert is_postgres("postgres://user:pass@localhost:5432/db") is True
    assert is_postgres(tmp_path / "local.db") is False
    assert _adapt_sql("SELECT * FROM t WHERE a = ? AND b = ?", True) == "SELECT * FROM t WHERE a = %s AND b = %s"

    empty_db = tmp_path / "empty.db"
    init_db(empty_db)
    ov = get_overview_stats(empty_db)
    assert ov["projects"] == 0
    assert ov["bid_lines"] == 0
    assert get_contractor_scorecard(empty_db).empty
    assert query_pay_item_observations(empty_db).empty


def test_head_to_head_avoids_cartesian_join_on_multi_line_items(tmp_path: Path):
    from src.db import (
        init_db,
        insert_bid_line,
        upsert_contractor,
        upsert_pay_item,
        upsert_project,
    )

    db_path = tmp_path / "h2h_multiline.db"
    init_db(db_path)

    from src.db import db_session

    with db_session(db_path) as conn:
        cid_a = upsert_contractor(conn, "ALPHA PAVING INC")
        cid_b = upsert_contractor(conn, "BETA CIVIL LLC")
        pid = upsert_project(
            conn,
            agency="ADOT",
            agency_project_num="2025115",
            title="ADOT Project 2025115",
            letting_date="2025-11-01",
            engineers_estimate=100000.0,
            winning_bid_total=95000.0,
            awarded_contractor_id=cid_a,
        )
        item_id = upsert_pay_item(
            conn,
            "ADOT",
            "7015052",
            "OBLITERATE PAVEMENT MARKING (STRIPE)",
            "L.FT.",
        )

        # Same item appears 3 times on the project: two distinct quantities + two matching quantities
        lines_a = [(55010.0, 0.33), (730.0, 1.15), (500.0, 10.0), (500.0, 20.0)]
        lines_b = [(55010.0, 0.35), (730.0, 1.25), (500.0, 12.0), (500.0, 22.0)]
        for qty, price in lines_a:
            insert_bid_line(
                conn,
                project_id=pid,
                contractor_id=cid_a,
                is_engineers_estimate=False,
                item_id=item_id,
                quantity=qty,
                unit_price=price,
                bid_rank=1,
                is_winner=True,
            )
        for qty, price in lines_b:
            insert_bid_line(
                conn,
                project_id=pid,
                contractor_id=cid_b,
                is_engineers_estimate=False,
                item_id=item_id,
                quantity=qty,
                unit_price=price,
                bid_rank=2,
                is_winner=False,
            )

    proj_h2h, line_h2h = get_head_to_head("ALPHA PAVING", "BETA CIVIL", db_path)
    assert len(proj_h2h) == 1
    # 1-to-1 matching: 4 lines total, NOT 4x4 = 16 cross-product rows
    assert len(line_h2h) == 4
    pairs = set(zip(line_h2h["quantity"], line_h2h["a_unit_price"], line_h2h["b_unit_price"]))
    assert pairs == {
        (55010.0, 0.33, 0.35),
        (730.0, 1.15, 1.25),
        (500.0, 10.0, 12.0),
        (500.0, 20.0, 22.0),
    }


def test_get_item_code_details_all_contractors(tmp_path: Path):
    from src.db import (
        db_session,
        get_item_code_details,
        init_db,
        insert_bid_line,
        upsert_contractor,
        upsert_pay_item,
        upsert_project,
    )

    db_path = tmp_path / "item_detail.db"
    init_db(db_path)

    with db_session(db_path) as conn:
        cid_a = upsert_contractor(conn, "ALPHA PAVING INC", approx_employees=120)
        cid_b = upsert_contractor(conn, "BETA CIVIL LLC", approx_employees=85)
        cid_c = upsert_contractor(conn, "GAMMA STRIPING CORP", approx_employees=40)
        pid = upsert_project(
            conn,
            agency="ADOT",
            agency_project_num="2025115",
            title="ADOT Project 2025115",
            letting_date="2025-11-01",
            engineers_estimate=50000.0,
            winning_bid_total=45000.0,
            awarded_contractor_id=cid_a,
        )
        item_target = upsert_pay_item(
            conn,
            "ADOT",
            "7015052",
            "OBLITERATE PAVEMENT MARKING (STRIPE)",
            "L.FT.",
        )
        item_other = upsert_pay_item(
            conn,
            "ADOT",
            "70150520",
            "OTHER SIMILAR PREFIX CODE",
            "L.FT.",
        )
        # Department estimate + 3 contractors on 7015052
        insert_bid_line(
            conn,
            project_id=pid,
            contractor_id=None,
            is_engineers_estimate=True,
            item_id=item_target,
            quantity=10000.0,
            unit_price=0.50,
        )
        for cid, rank, price, winner in [
            (cid_a, 1, 0.33, True),
            (cid_b, 2, 0.40, False),
            (cid_c, 3, 0.55, False),
        ]:
            insert_bid_line(
                conn,
                project_id=pid,
                contractor_id=cid,
                is_engineers_estimate=False,
                item_id=item_target,
                quantity=10000.0,
                unit_price=price,
                bid_rank=rank,
                is_winner=winner,
            )
        # Unrelated prefix code should NOT match exact item_code lookup
        insert_bid_line(
            conn,
            project_id=pid,
            contractor_id=cid_a,
            is_engineers_estimate=False,
            item_id=item_other,
            quantity=500.0,
            unit_price=9.99,
            bid_rank=1,
            is_winner=True,
        )

    summary_df, lines_df = get_item_code_details("7015052", db_path, spec_system="ADOT")
    assert len(summary_df) == 3
    assert set(summary_df["contractor_name"]) == {"ALPHA PAVING", "BETA CIVIL", "GAMMA STRIPING"}
    assert len(lines_df) == 4  # 3 contractors + 1 Department Estimate
    assert set(lines_df["item_code"]) == {"7015052"}
    assert "DEPARTMENT ESTIMATE" in set(lines_df["bidder_name"])


def test_get_head_to_head_opponents_filters_non_overlapping(tmp_path: Path):
    from src.db import (
        db_session,
        get_head_to_head_opponents,
        init_db,
        insert_bid_line,
        upsert_contractor,
        upsert_pay_item,
        upsert_project,
    )

    db_path = tmp_path / "h2h_opponents.db"
    init_db(db_path)

    with db_session(db_path) as conn:
        cid_a = upsert_contractor(conn, "ALPHA PAVING INC")
        cid_b = upsert_contractor(conn, "BETA CIVIL LLC")
        cid_c = upsert_contractor(conn, "GAMMA EXCAVATING CORP")
        cid_d = upsert_contractor(conn, "DELTA SOLO LLC")

        p1 = upsert_project(conn, agency="ADOT", agency_project_num="P1", title="Proj 1")
        p2 = upsert_project(conn, agency="ADOT", agency_project_num="P2", title="Proj 2")
        p3 = upsert_project(conn, agency="ADOT", agency_project_num="P3", title="Proj 3")
        p4 = upsert_project(conn, agency="ADOT", agency_project_num="P4", title="Proj 4")
        item_id = upsert_pay_item(conn, "ADOT", "2030301", "ROADWAY EXCAVATION", "CU.YD.")

        # P1: A and B; P2: A and B; P3: A and C; P4: D alone (+ Engineer's Estimate)
        for pid, cid in [(p1, cid_a), (p1, cid_b), (p2, cid_a), (p2, cid_b), (p3, cid_a), (p3, cid_c), (p4, cid_d)]:
            insert_bid_line(
                conn,
                project_id=pid,
                contractor_id=cid,
                is_engineers_estimate=False,
                item_id=item_id,
                quantity=100.0,
                unit_price=15.0,
            )
        insert_bid_line(
            conn,
            project_id=p4,
            contractor_id=None,
            is_engineers_estimate=True,
            item_id=item_id,
            quantity=100.0,
            unit_price=14.0,
        )

    opponents = get_head_to_head_opponents(db_path)
    assert opponents == {
        "ALPHA PAVING": [("BETA CIVIL", 2), ("GAMMA EXCAVATING", 1)],
        "BETA CIVIL": [("ALPHA PAVING", 2)],
        "GAMMA EXCAVATING": [("ALPHA PAVING", 1)],
    }
    assert "DELTA SOLO" not in opponents


def test_contractor_profile_enrichment_and_backfill(tmp_path: Path):
    from src.contractor_profiles import (
        backfill_contractor_profiles,
        get_contractor_metadata,
        lookup_contractor_profile,
    )
    from src.db import db_session, init_db, upsert_contractor

    assert lookup_contractor_profile("FANN CONTRACTING") == ("ROC071520", 250)
    assert lookup_contractor_profile("CACTUS ASPHALT A DIVISION OF CACTUS TRANSPORT") == ("ROC194430", 120)
    assert lookup_contractor_profile("UNKNOWN SOLO CONTRACTOR") == (None, None)
    meta = get_contractor_metadata("FANN CONTRACTING")
    assert meta["hq_location"] == "Prescott, AZ"
    assert meta["website"] == "https://fanncontracting.com"
    assert meta["specialty"]

    db_path = tmp_path / "enrich.db"
    init_db(db_path)

    with db_session(db_path) as conn:
        # Simulate pre-existing legacy row with NULL approx_employees and az_roc_license
        conn.execute(
            "INSERT INTO contractors (canonical_name, az_roc_license, approx_employees) VALUES (?, ?, ?)",
            ("NESBITT CONTRACTING", None, None),
        )
        # New upsert automatically populates profile estimate + ROC
        cid_new = upsert_contractor(conn, "Pulice Construction, Inc.")
        roc_new, emp_new = conn.execute(
            "SELECT az_roc_license, approx_employees FROM contractors WHERE contractor_id = ?",
            (cid_new,),
        ).fetchone()
    assert roc_new == "ROC071463"
    assert emp_new == 350

    updated = backfill_contractor_profiles(db_path)
    assert updated == 1
    with db_session(db_path) as conn:
        roc_bf, emp_backfilled = conn.execute(
            "SELECT az_roc_license, approx_employees FROM contractors WHERE canonical_name = 'NESBITT CONTRACTING'"
        ).fetchone()
    assert roc_bf == "ROC074063"
    assert emp_backfilled == 200


def test_overview_directory_queries(tmp_path: Path):
    from src.db import (
        db_session,
        get_all_contractors,
        get_all_pay_items,
        get_all_projects,
        init_db,
        insert_bid_line,
        upsert_contractor,
        upsert_pay_item,
        upsert_project,
    )

    db_path = tmp_path / "directories.db"
    init_db(db_path)

    with db_session(db_path) as conn:
        cid_a = upsert_contractor(conn, "Ames Construction, Inc.")
        cid_b = upsert_contractor(conn, "City Vendor Only LLC")
        p1 = upsert_project(
            conn,
            agency="ADOT",
            agency_project_num="F001",
            title="I-10 Widening",
            county="Maricopa",
            letting_date="2026-09-01",
            winning_bid_total=2500000.0,
            awarded_contractor_id=cid_a,
        )
        upsert_project(
            conn,
            agency="City of Mesa",
            agency_project_num="M001",
            title="Mesa Waterline Metadata",
            county="Maricopa",
            letting_date="2026-10-01",
            awarded_contractor_id=cid_b,
        )
        item_adot = upsert_pay_item(conn, "ADOT", "2030301", "ROADWAY EXCAVATION", "CU.YD.")
        upsert_pay_item(conn, "MAG", "321-1", "ASPHALT PAVEMENT", "TON")
        insert_bid_line(
            conn,
            project_id=p1,
            contractor_id=cid_a,
            is_engineers_estimate=False,
            item_id=item_adot,
            quantity=1000.0,
            unit_price=22.5,
            bid_rank=1,
            is_winner=True,
        )

    all_projs = get_all_projects(db_path)
    assert len(all_projs) == 2
    awarded_projs = get_all_projects(db_path, awarded_only=True)
    assert len(awarded_projs) == 1
    assert awarded_projs.iloc[0]["agency_project_num"] == "F001"

    all_contractors = get_all_contractors(db_path)
    assert len(all_contractors) == 2
    assert set(all_contractors["contractor_name"]) == {"AMES CONSTRUCTION", "CITY VENDOR ONLY"}
    ames_row = all_contractors[all_contractors["contractor_name"] == "AMES CONSTRUCTION"].iloc[0]
    assert int(ames_row["approx_employees"]) == 1500
    assert int(ames_row["projects_awarded"]) == 1

    all_items = get_all_pay_items(db_path)
    assert len(all_items) == 2
    mag_items = get_all_pay_items(db_path, spec_system="MAG")
    assert len(mag_items) == 1
    assert mag_items.iloc[0]["item_code"] == "321-1"


def test_get_contractor_details_wins_and_bids(tmp_path: Path):
    from src.db import (
        db_session,
        get_contractor_details,
        init_db,
        insert_bid_line,
        upsert_contractor,
        upsert_pay_item,
        upsert_project,
    )

    db_path = tmp_path / "contractor_details.db"
    init_db(db_path)

    with db_session(db_path) as conn:
        cid_a = upsert_contractor(conn, "Pulice Construction, Inc.")
        cid_b = upsert_contractor(conn, "Fann Contracting, Inc.")
        p_win_itemized = upsert_project(
            conn,
            agency="ADOT",
            agency_project_num="F100",
            title="Loop 101 Bridge",
            county="Maricopa",
            letting_date="2026-08-01",
            engineers_estimate=100000.0,
            winning_bid_total=90000.0,
            awarded_contractor_id=cid_a,
        )
        p_loss = upsert_project(
            conn,
            agency="MCDOT",
            agency_project_num="M200",
            title="MCDOT Paving",
            county="Maricopa",
            letting_date="2026-09-01",
            engineers_estimate=50000.0,
            winning_bid_total=48000.0,
            awarded_contractor_id=cid_b,
        )
        # Metadata-only Bonfire win (no bid_lines)
        upsert_project(
            conn,
            agency="City of Scottsdale",
            agency_project_num="S300",
            title="Scottsdale Roadwork",
            county="Maricopa",
            letting_date="2026-10-01",
            awarded_contractor_id=cid_a,
        )
        item_id = upsert_pay_item(conn, "ADOT", "2030301", "ROADWAY EXCAVATION", "CU.YD.")
        insert_bid_line(
            conn,
            project_id=p_win_itemized,
            contractor_id=cid_a,
            is_engineers_estimate=False,
            item_id=item_id,
            quantity=1000.0,
            unit_price=90.0,
            bid_rank=1,
            is_winner=True,
        )
        insert_bid_line(
            conn,
            project_id=p_loss,
            contractor_id=cid_a,
            is_engineers_estimate=False,
            item_id=item_id,
            quantity=1000.0,
            unit_price=55.0,
            bid_rank=2,
            is_winner=False,
        )

    details = get_contractor_details("Pulice Construction, Inc.", db_path)
    assert details["contractor"]["contractor_name"] == "PULICE CONSTRUCTION"
    assert details["summary"]["projects_won"] == 2
    assert details["summary"]["total_won_volume"] == 90000.0
    assert details["summary"]["total_bids_submitted"] == 2
    assert details["summary"]["win_rate_pct"] == 50.0
    assert len(details["won_projects_df"]) == 2
    assert len(details["all_bids_df"]) == 2
    assert len(details["top_items_df"]) == 1







