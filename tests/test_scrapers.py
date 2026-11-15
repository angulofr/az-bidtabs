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


def test_adot_bid_results_pdf_format(tmp_path: Path):
    from src.scrapers.adot import ParsedProjectBidTab, _parse_adot_bid_results_format, ingest_parsed_project

    lines = [
        "Printed: 9/22/2026 Page 1 of 15",
        "BID RESULTS FOR 2026005",
        "Bid Opening Date : 8/21/2026, Prequalification Required, Engineer Specialist : Rene Teran",
        "The proposed project is located in Cochise County beginning at MP 377.0 (San Simon) and extends east along Interstate 10 to MP 391.23 (New Mexico State Line). The construction is being administered by the ADOT Southeast District.",
        "+% $47,458,932.00 DEPARTMENT ESTIMATE $47,458,932.00",
        "1 -22.2% $36,933,626.20 FNF CONSTRUCTION, INC. $36,933,626.20",
        "2 -17.3% $39,255,777.77 FANN CONTRACTING, INC $39,255,777.77",
        "*** Bid Tabs for Proposal Pamphlet 2026005 ***",
        "Item Number Unit of Measure Item Description Quantity Unit Price Extended Amount Compared To Dept Estimate",
        "2020009 CU.YD. REMOVAL OF STRUCTURAL CONCRETE",
        "Bid Rank Department Estimate 1.000 $500.000 $500.00",
        "1 FNF CONSTRUCTION, INC. $30,000.000 $30,000.00 +5900.0%",
        "2 FANN CONTRACTING, INC $3,500.000 $3,500.00 +600.0%",
        "2020081 SQ.YD. REMOVE BITUMINOUS PAVEMENT (MILLING) (1\")",
        "Bid Rank Department Estimate 256,100.000 $2.000 $512,200.00",
        "1 FNF CONSTRUCTION, INC. $1.100 $281,710.00 -45.0%",
        "2 FANN CONTRACTING, INC $1.750 $448,175.00 -12.5%",
    ]

    parsed = ParsedProjectBidTab(
        agency="ADOT",
        agency_project_num="F069101C",
        title="Interstate 10 — Cochise County (2026005)",
        county="Cochise",
        district="Southeast",
        letting_date="2026-08-21",
    )
    ok = _parse_adot_bid_results_format(lines, parsed)
    assert ok is True
    assert parsed.engineers_estimate == 47458932.00
    assert parsed.winning_bid_total == 36933626.20
    assert parsed.awarded_contractor == "FNF CONSTRUCTION, INC."
    assert len(parsed.lines) == 6
    assert parsed.lines[1].bidder_name == "FNF CONSTRUCTION, INC."
    assert parsed.lines[1].is_winner is True
    assert parsed.lines[4].quantity == 256100.0
    assert parsed.lines[4].unit_price == 1.10

    db_path = tmp_path / "adot_pdf.db"
    init_db(db_path)
    ingest_parsed_project(parsed, db_path)
    # Re-ingest should remain idempotent
    ingest_parsed_project(parsed, db_path)
    ov = get_overview_stats(db_path)
    assert ov["projects"] == 1
    assert ov["bid_lines"] == 6
    assert ov["contractors"] == 2

    # Uploading E2C2 CSV containing the same TRACS should NOT overwrite named contractor PDF data
    from src.scrapers.adot import ingest_e2c2_csv

    e2c2_overlap = (
        "DistName,Bid_Date,Item_Number,Description,Measure,Tracs_Number,Route,projname,length,Quantity,State,Bidder_1,Bidder_2,Bidder_3\n"
        "SOUTHEAST DISTRICT,08/21/2026,2020009,REMOVAL OF STRUCTURAL CONCRETE,CU.YD.,F069101C,I-10,I-10 COCHISE,5.0,1.00,500.00,30000.00,3500.00,.00\n"
    ).encode("utf-8")
    res_e2c2 = ingest_e2c2_csv(e2c2_overlap, db_path)
    assert res_e2c2["projects"] == 0
    assert get_overview_stats(db_path)["bid_lines"] == 6




