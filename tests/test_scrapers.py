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


def test_batch_import_folder(tmp_path: Path):
    from scripts.import_folder import import_folder

    folder = tmp_path / "exports"
    folder.mkdir()
    (folder / "proj_a.csv").write_text(
        "Item Code,Description,Unit,Quantity,Vendor,Unit Price\n"
        "321-1,ASPHALT CONCRETE PAVEMENT,TON,1000,Ames Construction Inc,120.00\n",
        encoding="utf-8",
    )
    (folder / "proj_b.csv").write_text(
        "Item Code,Description,Unit,Quantity,Vendor,Unit Price\n"
        "340-1,CONCRETE CURB AND GUTTER,LF,500,Pulice Construction Inc,34.50\n",
        encoding="utf-8",
    )

    db_path = tmp_path / "batch.db"
    report = import_folder(folder, db_target=db_path, agency="City of Phoenix")
    assert report["files_found"] == 2
    assert report["ingested_projects"] == 2
    assert report["total_bid_lines"] == 2
    assert report["errors"] == []
    assert get_overview_stats(db_path)["projects"] == 2


def test_adot_e2c2_csv_ingest(tmp_path: Path):
    from src.scrapers.adot import ingest_e2c2_csv, is_e2c2_csv

    sample_e2c2 = (
        '\ufefftextbox24,textbox22\n'
        '"November 15, 2026","Parameter(s) supplied: Test"\n\n'
        'DistName,Bid_Date,Item_Number,Description,Measure,Tracs_Number,Route,projname,length,Quantity,State,Bidder_1,Bidder_2,Bidder_3\n'
        'CENTRAL DISTRICT,02/13/2026,2030301,ROADWAY EXCAVATION,CU.YD.,H687001C,303 MA 99.0,MC 85 - VAN BUREN ST,6.4,"115,421.00",20.00,12.80,12.00,.00\n'
        'SOUTHCENT DISTRICT,02/06/2026,2020031,REMOVAL OF PCC PAVEMENT,SQ.YD.,F047501C,101 MA 0.0,SR101L & I-10,2.0,"76,347.00",20.00,19.25,16.55,.00\n'
    ).encode("utf-8")

    assert is_e2c2_csv(sample_e2c2) is True
    db_path = tmp_path / "e2c2.db"
    init_db(db_path)
    res = ingest_e2c2_csv(sample_e2c2, db_path)
    assert res["projects"] == 2
    assert res["pay_items"] == 2
    assert res["bid_lines"] == 6  # 2 projects * (EE + Bidder_1 + Bidder_2)
    assert get_overview_stats(db_path)["projects"] == 2



