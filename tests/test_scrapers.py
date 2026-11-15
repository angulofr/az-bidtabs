from pathlib import Path

from src.db import get_overview_stats, init_db
from src.scrapers.adot import ingest_parsed_project
from src.scrapers.phoenix_opengov import parse_opengov_bid_tab_export


def test_opengov_csv_parse_and_ingest(tmp_path: Path):
    db_path = tmp_path / "test_ingest.db"
    init_db(db_path)

    csv_bytes = (
        "Item Code,Description,Unit,Quantity,Vendor,Unit Price\n"
        "321-1,ASPHALT CONCRETE PAVEMENT,TON,2500,Sunland Asphalt LLC,121.50\n"
        "340-1,CONCRETE CURB AND GUTTER,LF,1800,Sunland Asphalt LLC,33.00\n"
    ).encode("utf-8")

    parsed = parse_opengov_bid_tab_export(
        csv_bytes,
        "phoenix_tab.csv",
        project_num="PHX-TEST-01",
        title="Test Phoenix MAG Project",
    )
    assert parsed.spec_system == "MAG"
    assert len(parsed.lines) == 2
    assert parsed.lines[1].unit == "L.FT."

    pid = ingest_parsed_project(parsed, db_path)
    assert pid > 0
    stats = get_overview_stats(db_path)
    assert stats["projects"] == 1
    assert stats["bid_lines"] == 2
