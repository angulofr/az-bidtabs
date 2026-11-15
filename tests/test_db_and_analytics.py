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


