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
