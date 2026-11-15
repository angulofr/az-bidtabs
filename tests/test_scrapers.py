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


def test_adot_pdf_discovery_and_dry_run_sync(tmp_path: Path):
    from unittest.mock import MagicMock

    from src.scrapers.adot import discover_adot_bid_tab_pdfs, run_adot_archive_sync

    html = """
    <html><body>
      <a href="/files/bidtabs/F051201C_bidtab.pdf">TRACS F051201C - Loop 101 Widening</a>
      <a href="https://azdot.gov/files/H890101C.pdf">H890101C - I-10 Corridor</a>
      <a href="/about">About ADOT</a>
    </body></html>
    """
    mock_resp = MagicMock()
    mock_resp.text = html
    mock_resp.raise_for_status = MagicMock()

    mock_sess = MagicMock()
    mock_sess.headers = {}
    mock_sess.get.return_value = mock_resp

    links = discover_adot_bid_tab_pdfs(session=mock_sess)
    assert len(links) == 2
    assert links[0]["project_num"] == "F051201C"
    assert links[0]["url"] == "https://azdot.gov/files/bidtabs/F051201C_bidtab.pdf"

    db_path = tmp_path / "test_adot_sync.db"
    init_db(db_path)
    res = run_adot_archive_sync(db_path, dry_run=True, session=mock_sess)
    assert res["discovered"] == 2
    assert res["ingested"] == 2
    assert res["skipped"] == 0

