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


def test_sync_adot_browser_tracs_helper():
    from scripts.sync_adot_browser import extract_tracs_from_text

    assert extract_tracs_from_text("073 GI 333 F069101C Bid Tabs.pdf") == "F069101C"
    assert extract_tracs_from_text("https://azdot.gov/H687001C_BidTabs.pdf") == "H687001C"
    assert extract_tracs_from_text("Unrelated Document.pdf") is None


def test_adot_pdf_parser_handles_hyphen_slash_units_and_prevents_leakage():
    from src.scrapers.adot import ParsedProjectBidTab, _parse_adot_bid_results_format

    lines = [
        "BID RESULTS FOR 2025115",
        "*** Bid Tabs for Proposal Pamphlet 2025115 ***",
        "7015052 L.FT. OBLITERATE PAVEMENT MARKING (STRIPE)",
        "Bid Rank Department Estimate 55,010.000 $0.450 $24,754.50",
        "1 CONTRACTOR A INC $0.330 $18,153.30 -26.7%",
        "2 CONTRACTOR B LLC $0.380 $20,903.80 -15.6%",
        "7016030 EACH-DAY BARRICADE (TYPE 1, TYPE 2, VERT.PANEL, TUBULAR MARKER)",
        "Bid Rank Department Estimate 47,710.000 $0.500 $23,855.00",
        "1 CONTRACTOR A INC $0.350 $16,698.50 -30.0%",
        "2 CONTRACTOR B LLC $25.000 $1,192,750.00 +4900.0%",
        "7016067 EACH-DAY CHANGEABLE MESSAGE BOARD (CONTRACTOR FURNISHED)",
        "Bid Rank Department Estimate 730.000 $50.000 $36,500.00",
        "1 CONTRACTOR A INC $46.650 $34,054.50 -6.7%",
        "2 CONTRACTOR B LLC $1,792.000 $1,308,160.00 +3484.0%",
        "7016020 L.FT.-DAY TEMPORARY CONCRETE BARRIER (IN USE)",
        "Bid Rank Department Estimate 1,000.000 $0.250 $250.00",
        "1 CONTRACTOR A INC $0.200 $200.00 -20.0%",
        "9240130 EACH/DAY MISCELLANEOUS WORK",
        "Bid Rank Department Estimate 100.000 $15.000 $1,500.00",
        "1 CONTRACTOR A INC $12.000 $1,200.00 -20.0%",
        "6050002 LB. REINFORCING STEEL",
        "Bid Rank Department Estimate 5,000.000 $2.000 $10,000.00",
        "1 CONTRACTOR A INC $1.850 $9,250.00 -7.5%",
        "2090005 M.GAL. FURNISH WATER",
        "Bid Rank Department Estimate 250.000 $40.000 $10,000.00",
        "1 CONTRACTOR A INC $35.000 $8,750.00 -12.5%",
        "9240110 CU.FT. MISCELLANEOUS WORK",
        "Bid Rank Department Estimate 40.000 $10.000 $400.00",
        "1 CONTRACTOR A INC $9.000 $360.00 -10.0%",
        # Simulate an unparseable item header before another Department Estimate: must NOT leak onto 9240110
        "UNEXPECTED HEADER WITHOUT CODE",
        "Bid Rank Department Estimate 999.000 $99.000 $98,901.00",
        "1 CONTRACTOR A INC $88.000 $87,912.00 -11.1%",
    ]

    parsed = ParsedProjectBidTab(agency="ADOT", agency_project_num="2025115", title="Test 2025115")
    ok = _parse_adot_bid_results_format(lines, parsed)
    assert ok is True

    by_code = {}
    for ln in parsed.lines:
        by_code.setdefault(ln.item_code, []).append(ln)

    assert set(by_code.keys()) == {
        "7015052",
        "7016030",
        "7016067",
        "7016020",
        "9240130",
        "6050002",
        "2090005",
        "9240110",
    }
    # 7015052 must have ONLY its own 3 lines (EE + 2 bidders), none of the EACH-DAY items
    assert len(by_code["7015052"]) == 3
    assert by_code["7016030"][0].unit == "EACH-DAY"
    assert by_code["7016067"][2].unit_price == 1792.00
    assert by_code["7016020"][0].unit == "L.FT.-DAY"
    assert by_code["9240130"][0].unit == "EACH-DAY"
    assert by_code["6050002"][0].unit == "LB"
    assert by_code["2090005"][0].unit == "M.GAL"
    assert by_code["9240110"][0].unit == "CU.FT."
    assert len(by_code["9240110"]) == 2


def test_sync_adot_browser_upgrades_e2c2_only_projects(tmp_path: Path):
    from scripts.sync_adot_browser import _get_already_pdf_archived_adot_nums
    from src.db import init_db
    from src.scrapers.adot import (
        ParsedBidLine,
        ParsedProjectBidTab,
        ingest_e2c2_csv,
        ingest_parsed_project,
    )

    db_path = tmp_path / "upgrade_e2c2.db"
    init_db(db_path)

    # 1. E2C2-only project (anonymous bidder) should NOT block PDF upgrade
    e2c2_csv = (
        "DistName,Bid_Date,Item_Number,Description,Measure,Tracs_Number,Route,projname,length,Quantity,State,Bidder_1,Bidder_2,Bidder_3\n"
        "CENTRAL DISTRICT,02/13/2026,2030301,ROADWAY EXCAVATION,CU.YD.,H687001C,SR303,MC85,2.0,1000.0,20.0,15.0,16.0,0.0\n"
    ).encode("utf-8")
    ingest_e2c2_csv(e2c2_csv, db_path)
    assert "H687001C" not in _get_already_pdf_archived_adot_nums(db_path)

    # 2. Named PDF project SHOULD be marked archived and skipped in future syncs
    parsed_pdf = ParsedProjectBidTab(
        agency="ADOT",
        agency_project_num="F069101C",
        title="I-10 Cochise",
        awarded_contractor="FNF CONSTRUCTION, INC.",
        source_url="https://azdot.gov/business/contracts-and-specifications/bid-tabulations",
        lines=[
            ParsedBidLine(
                item_code="2020009",
                description="REMOVAL OF STRUCTURAL CONCRETE",
                unit="CU.YD.",
                quantity=1.0,
                unit_price=30000.0,
                extended_total=30000.0,
                bidder_name="FNF CONSTRUCTION, INC.",
                bid_rank=1,
                is_winner=True,
            )
        ],
    )
    ingest_parsed_project(parsed_pdf, db_path)
    assert "F069101C" in _get_already_pdf_archived_adot_nums(db_path)


def test_phoenix_opengov_falls_back_to_legacy_awards(tmp_path: Path):
    from unittest.mock import MagicMock

    from src.db import db_session, init_db
    from src.scrapers.phoenix_opengov import (
        PHOENIX_LEGACY_PORTAL,
        PHOENIX_OPENGOV_API,
        PHOENIX_OPENGOV_PORTAL,
        fetch_phoenix_opengov_solicitations,
        run_phoenix_opengov_sync,
    )

    api_resp = MagicMock(
        ok=True,
        status_code=200,
        headers={"content-type": "text/html; charset=utf-8"},
        text="<!doctype html><html><body>SPA</body></html>",
    )
    cf_resp = MagicMock(
        ok=False,
        status_code=403,
        headers={"content-type": "text/html"},
        text="<title>Just a moment...</title><script src='https://challenges.cloudflare.com'></script>",
    )
    legacy_html = """
    <html><body><table>
      <tr><th>Number</th><th>Title</th><th>Buyer</th><th>Status</th></tr>
      <tr>
        <td><a href="/Solicitations/Details/1741">6000001568</a></td>
        <td>CRISI RAILROAD IMPROVEMENTS - ST85100452</td>
        <td>Kandi Kawolsky</td>
        <td>Award Notice</td>
      </tr>
      <tr>
        <td><a href="/Solicitations/Details/1841">6000001632</a></td>
        <td>2024 SS4A RSAP SPEED LIMIT STUDY - ST89320174</td>
        <td>Tonja Lepur</td>
        <td>Award Notice</td>
      </tr>
    </table></body></html>
    """
    legacy_resp = MagicMock(
        ok=True,
        status_code=200,
        headers={"content-type": "text/html"},
        text=legacy_html,
    )
    legacy_resp.raise_for_status = MagicMock()

    def fake_get(url, **kwargs):
        if url == PHOENIX_OPENGOV_API:
            return api_resp
        if url == PHOENIX_OPENGOV_PORTAL:
            return cf_resp
        if url.startswith(PHOENIX_LEGACY_PORTAL):
            return legacy_resp
        raise AssertionError(f"Unexpected URL: {url}")

    mock_sess = MagicMock()
    mock_sess.headers = {}
    mock_sess.get.side_effect = fake_get

    sols = fetch_phoenix_opengov_solicitations(limit=10, session=mock_sess)
    assert len(sols) == 2
    assert sols[0].project_num == "6000001568"
    assert sols[0].url == "https://solicitations.phoenix.gov/Solicitations/Details/1741"

    db_path = tmp_path / "phx_fallback.db"
    init_db(db_path)
    res = run_phoenix_opengov_sync(db_path, limit=10, session=mock_sess)
    assert res == {"discovered": 2, "ingested": 2, "skipped": 0}
    with db_session(db_path) as conn:
        count = conn.execute(
            "SELECT COUNT(*) FROM projects WHERE agency = 'City of Phoenix'"
        ).fetchone()[0]
    assert count == 2


def test_scottsdale_bonfire_fetch_and_sync(tmp_path: Path):
    from unittest.mock import MagicMock

    from src.db import db_session, init_db
    from src.scrapers.scottsdale_bonfire import (
        SCOTTSDALE_PAST_OPPORTUNITIES_URL,
        SCOTTSDALE_PUBLIC_CONTRACTS_URL,
        fetch_scottsdale_bonfire_opportunities,
        run_scottsdale_bonfire_sync,
    )

    past_payload = {
        "payload": {
            "projects": {
                "81776": {
                    "ProjectID": "81776",
                    "ReferenceID": "IFB-122022-041",
                    "ProjectName": "MILLER ROAD: PINNACLE PEAK RD TO HAPPY VALLEY RD",
                    "DateClose": "2023-01-12 20:00:00",
                    "IsPublicAward": True,
                },
                "64275": {
                    "ProjectID": "64275",
                    "ReferenceID": "IFB-032022-001",
                    "ProjectName": "SWIMMING POOL CHEMICALS",
                    "DateClose": "2022-04-26 20:00:00",
                    "IsPublicAward": True,
                },
            }
        }
    }
    contracts_payload = {
        "payload": {
            "vendors": [
                {
                    "VendorID": "9001",
                    "VendorContactOrganizationName": "Pulice Construction, Inc.",
                }
            ],
            "publicContracts": {
                "1": {
                    "ContractID": "1",
                    "VendorID": "9001",
                    "Name": "IFB-122022-041 - MILLER ROAD: PINNACLE PEAK RD TO HAPPY VALLEY RD",
                }
            },
        }
    }

    past_resp = MagicMock(ok=True)
    past_resp.raise_for_status = MagicMock()
    past_resp.json.return_value = past_payload

    contracts_resp = MagicMock(ok=True)
    contracts_resp.json.return_value = contracts_payload

    def fake_get(url, **kwargs):
        if url == SCOTTSDALE_PAST_OPPORTUNITIES_URL:
            return past_resp
        if url == SCOTTSDALE_PUBLIC_CONTRACTS_URL:
            return contracts_resp
        raise AssertionError(f"Unexpected URL: {url}")

    mock_sess = MagicMock()
    mock_sess.headers = {}
    mock_sess.get.side_effect = fake_get

    opps = fetch_scottsdale_bonfire_opportunities(limit=10, civil_only=True, session=mock_sess)
    assert len(opps) == 1
    assert opps[0].reference_id == "IFB-122022-041"
    assert opps[0].awarded_contractor == "Pulice Construction, Inc."

    db_path = tmp_path / "scottsdale.db"
    init_db(db_path)
    res1 = run_scottsdale_bonfire_sync(db_path, limit=10, session=mock_sess)
    assert res1 == {"discovered": 1, "ingested": 1, "skipped": 0}
    res2 = run_scottsdale_bonfire_sync(db_path, limit=10, session=mock_sess)
    assert res2 == {"discovered": 1, "ingested": 0, "skipped": 1}

    with db_session(db_path) as conn:
        row = conn.execute(
            """
            SELECT p.agency_project_num, p.letting_date, c.canonical_name
            FROM projects p
            LEFT JOIN contractors c ON p.awarded_contractor_id = c.contractor_id
            WHERE p.agency = 'City of Scottsdale'
            """
        ).fetchone()
    assert tuple(row) == ("IFB-122022-041", "2023-01-12", "PULICE CONSTRUCTION")


def test_bonfire_multi_portal_goodyear_and_paradise_valley(tmp_path: Path):
    from unittest.mock import MagicMock

    from src.db import db_session, init_db
    from src.scrapers.bonfire import BONFIRE_PORTALS, run_all_bonfire_syncs

    def _resp(payload):
        r = MagicMock(ok=True)
        r.raise_for_status = MagicMock()
        r.json.return_value = {"payload": payload}
        return r

    routes = {
        BONFIRE_PORTALS["scottsdale"].past_opportunities_url: _resp({"projects": {}}),
        BONFIRE_PORTALS["scottsdale"].public_contracts_url: _resp({"vendors": [], "publicContracts": {}}),
        BONFIRE_PORTALS["goodyear"].past_opportunities_url: _resp(
            {
                "projects": {
                    "1": {
                        "ProjectID": "101",
                        "ReferenceID": "23-0055",
                        "ProjectName": "CMAR - Estrella Parkway from Vineyard to MC85",
                        "DateClose": "2022-12-05 15:00:00",
                        "IsPublicAward": True,
                    },
                    "2": {
                        "ProjectID": "102",
                        "ReferenceID": "RFP 23-0123",
                        "ProjectName": "Auditing Services",
                        "DateClose": "2023-04-04 15:00:00",
                        "IsPublicAward": True,
                    },
                }
            }
        ),
        BONFIRE_PORTALS["goodyear"].public_contracts_url: _resp(
            {
                "vendors": [{"VendorID": "7", "VendorContactOrganizationName": "Ames Construction, Inc."}],
                "publicContracts": {
                    "1": {"ContractID": "1", "VendorID": "7", "Name": "23-0055 - Estrella Parkway"}
                },
            }
        ),
        BONFIRE_PORTALS["paradise_valley"].past_opportunities_url: _resp(
            {
                "projects": {
                    "3": {
                        "ProjectID": "201",
                        "ReferenceID": "25-001-ENG CMAR",
                        "ProjectName": "Roadway and Drainage Improvements, Mockingbird Lane",
                        "DateClose": "2024-07-17 14:00:00",
                        "IsPublicAward": True,
                    }
                }
            }
        ),
        BONFIRE_PORTALS["paradise_valley"].public_contracts_url: _resp({"vendors": [], "publicContracts": {}}),
        BONFIRE_PORTALS["tempe"].past_opportunities_url: _resp(
            {
                "projects": {
                    "4": {
                        "ProjectID": "301",
                        "ReferenceID": "26-101",
                        "ProjectName": "Apache Blvd Streetscape & Storm Drain Improvements",
                        "DateClose": "2026-02-01 15:00:00",
                        "IsPublicAward": True,
                    }
                }
            }
        ),
        BONFIRE_PORTALS["tempe"].public_contracts_url: _resp({"vendors": [], "publicContracts": {}}),
        BONFIRE_PORTALS["peoria"].past_opportunities_url: _resp({"projects": {}}),
        BONFIRE_PORTALS["peoria"].public_contracts_url: _resp({"vendors": [], "publicContracts": {}}),
        BONFIRE_PORTALS["buckeye"].past_opportunities_url: _resp({"projects": {}}),
        BONFIRE_PORTALS["buckeye"].public_contracts_url: _resp({"vendors": [], "publicContracts": {}}),
    }

    mock_sess = MagicMock()
    mock_sess.headers = {}
    mock_sess.get.side_effect = lambda url, **kw: routes[url]

    db_path = tmp_path / "bonfire_all.db"
    init_db(db_path)
    res = run_all_bonfire_syncs(db_path, limit=10, session=mock_sess)

    assert res["scottsdale"]["ingested"] == 0
    assert res["goodyear"]["discovered"] == 1
    assert res["goodyear"]["ingested"] == 1
    assert res["paradise_valley"]["discovered"] == 1
    assert res["paradise_valley"]["ingested"] == 1
    assert res["tempe"]["discovered"] == 1
    assert res["tempe"]["ingested"] == 1
    assert res["peoria"]["ingested"] == 0
    assert res["buckeye"]["ingested"] == 0

    with db_session(db_path) as conn:
        rows = conn.execute(
            """
            SELECT p.agency, p.agency_project_num, c.canonical_name
            FROM projects p
            LEFT JOIN contractors c ON p.awarded_contractor_id = c.contractor_id
            ORDER BY p.agency
            """
        ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("City of Goodyear", "23-0055", "AMES CONSTRUCTION"),
        ("City of Tempe", "26-101", None),
        ("Town of Paradise Valley", "25-001-ENG CMAR", None),
    ]


def test_maricopa_bidsync_parse_and_sync(tmp_path: Path):
    from unittest.mock import MagicMock

    from src.db import db_session, init_db
    from src.scrapers.maricopa_bidsync import (
        BIDSYNC_MARICOPA_SEARCH_URL,
        BIDSYNC_SEARCH_POST_URL,
        run_maricopa_bidsync_sync,
    )

    search_get_html = """
    <html><body>
      <form id="elasticBidSearchForm">
        <input type="hidden" name="javax.faces.ViewState" value="vs123" />
        <input type="text" name="elasticBidSearchForm:bidSearchParametersComponent:j_idt323" value="" />
      </form>
    </body></html>
    """
    search_post_html = """
    <html><body><table>
      <tr>
        <td>220014-DBB</td>
        <td>Lower Buckeye Road - 71st Av to 67th Av</td>
        <td>Maricopa County</td>
        <td>Arizona</td>
        <td>Oct 19, 2021 2:00:00 PM MST</td>
        <td>Bid has ended</td>
        <td><a href="https://www.bidsync.com/DPX?ac=awgrouplist&auc=2082474&supsrch=">Tab</a></td>
      </tr>
    </table></body></html>
    """
    tab_html = """
    <html><body>
      <table>
        <tr><td>Awarded</td><td></td><td>Supplier</td><td></td></tr>
        <tr>
          <td><img alt="Awarded" /></td><td><img alt="Click to open supplier bid information" /></td>
          <td></td><td>Nesbitt Contracting Co., Inc.</td>
        </tr>
        <tr><td></td><td>Default Lot</td><td>Unit Price</td><td>Qty</td><td>Price</td></tr>
        <tr><td></td><td>220014-DBB--01-06 - Mobilization/Demobilization</td><td>$90,000.00</td><td>1</td><td>$90,000.00</td></tr>
        <tr><td></td><td>220014-DBB--01-07 - Roadway Excavation</td><td>$30.00</td><td>7003</td><td>$210,090.00</td></tr>
        <tr><td>TOTAL PRICE:</td><td>$300,090.00</td></tr>
        <tr>
          <td><img alt="Click to open supplier bid information" /></td>
          <td></td><td></td><td>Combs Construction Company, Inc.</td>
        </tr>
        <tr><td></td><td>Default Lot</td><td>Unit Price</td><td>Qty</td><td>Price</td></tr>
        <tr><td></td><td>220014-DBB--01-06 - Mobilization/Demobilization</td><td>$95,000.00</td><td>1</td><td>$95,000.00</td></tr>
        <tr><td></td><td>220014-DBB--01-07 - Roadway Excavation</td><td>$32.00</td><td>7003</td><td>$224,096.00</td></tr>
        <tr><td>TOTAL PRICE:</td><td>$319,096.00</td></tr>
      </table>
    </body></html>
    """

    get_resp = MagicMock(ok=True, text=search_get_html, url=BIDSYNC_MARICOPA_SEARCH_URL)
    get_resp.raise_for_status = MagicMock()
    post_resp = MagicMock(ok=True, text=search_post_html, url=BIDSYNC_SEARCH_POST_URL)
    post_resp.raise_for_status = MagicMock()
    tab_resp = MagicMock(
        ok=True,
        text=tab_html,
        url="https://www.bidsync.com/DPX?ac=awgroupreview&auc=2082474&supsrch=&expandall=Y",
    )

    mock_sess = MagicMock()
    mock_sess.headers = {}
    mock_sess.get.side_effect = lambda url, **kw: (
        get_resp if url == BIDSYNC_MARICOPA_SEARCH_URL else tab_resp
    )
    mock_sess.post.return_value = post_resp

    db_path = tmp_path / "mcdot_bidsync.db"
    init_db(db_path)
    res = run_maricopa_bidsync_sync(db_path, limit=5, session=mock_sess)
    assert res["ingested"] == 1
    assert res["total_bid_lines"] == 4

    with db_session(db_path) as conn:
        proj = conn.execute(
            "SELECT agency, agency_project_num, letting_date, winning_bid_total FROM projects"
        ).fetchone()
        lines = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN is_winner = 1 THEN 1 ELSE 0 END) FROM bid_lines"
        ).fetchone()
    assert tuple(proj) == ("MCDOT", "220014-DBB", "2021-10-19", 300090.0)
    assert tuple(lines) == (4, 2)


def test_mesa_and_chandler_metadata_sync(tmp_path: Path):
    from unittest.mock import MagicMock

    from src.db import db_session, init_db
    from src.scrapers.mesa_chandler import (
        APP_AZ_BROWSE_URL,
        MESA_SOCRATA_URL,
        run_chandler_sync,
        run_mesa_sync,
    )

    mesa_resp = MagicMock(ok=True)
    mesa_resp.raise_for_status = MagicMock()
    mesa_resp.json.return_value = [
        {
            "contract_no": "2027062",
            "contract_description": "Streetlight Maint., Installations, Removals",
            "contract_status": "Initiated",
            "creation_date": "2026-10-28T00:00:00.000",
        },
        {
            "contract_no": "2027066",
            "contract_description": "Bee and Other Stinging Insect Removal Services",
            "contract_status": "Initiated",
            "creation_date": "2026-10-28T00:00:00.000",
        },
    ]

    app_get_html = """
    <html><body><form>
      <input type="hidden" name="__VIEWSTATE" value="abc" />
      <input type="hidden" name="body:x:selOrgaLevelOrgaNode_78E9FF04_1" value="" />
    </form></body></html>
    """
    app_post_html = """
    <html><body><table id="body_x_grid_grd">
      <tr>
        <td><a href="/page.aspx/en/bpm/process_manage_extranet/991">Edit</a></td>
        <td>BPM007852</td>
        <td>Invitation to Bid - City of Chandler Runway 4 Improvements</td>
        <td>10/28/2026 8:00:00 AM</td>
        <td>Heavy construction services</td>
        <td>City of Chandler</td>
        <td></td>
        <td>Open for Bidding</td>
      </tr>
      <tr>
        <td></td>
        <td>BPM007818</td>
        <td>Debt collection services</td>
        <td>10/02/2026 1:45:00 PM</td>
        <td>Debt collection services</td>
        <td>City of Chandler</td>
        <td></td>
        <td>Achieved</td>
      </tr>
    </table></body></html>
    """
    app_get_resp = MagicMock(ok=True, text=app_get_html)
    app_get_resp.raise_for_status = MagicMock()
    app_post_resp = MagicMock(ok=True, text=app_post_html)
    app_post_resp.raise_for_status = MagicMock()

    mock_sess = MagicMock()
    mock_sess.headers = {}
    mock_sess.get.side_effect = lambda url, **kw: (
        mesa_resp if url == MESA_SOCRATA_URL else app_get_resp
    )
    mock_sess.post.return_value = app_post_resp

    db_path = tmp_path / "mesa_chandler.db"
    init_db(db_path)
    res_m = run_mesa_sync(db_path, limit=10, session=mock_sess)
    res_c = run_chandler_sync(db_path, limit=10, session=mock_sess)
    assert res_m == {"agency": "City of Mesa", "discovered": 1, "ingested": 1, "skipped": 0}
    assert res_c == {"agency": "City of Chandler", "discovered": 1, "ingested": 1, "skipped": 0}

    with db_session(db_path) as conn:
        rows = conn.execute(
            "SELECT agency, agency_project_num, letting_date FROM projects ORDER BY agency"
        ).fetchall()
    assert [tuple(r) for r in rows] == [
        ("City of Chandler", "BPM007852", "2026-10-28"),
        ("City of Mesa", "2027062", "2026-10-28"),
    ]


def test_bidsync_primefaces_ajax_pagination():
    from unittest.mock import MagicMock

    from src.scrapers.maricopa_bidsync import (
        BIDSYNC_MARICOPA_SEARCH_URL,
        fetch_bidsync_listings,
    )

    page0_html = """
    <html><body>
      <form id="elasticBidSearchForm">
        <input type="hidden" name="javax.faces.ViewState" value="vs-0" />
        <input type="text" name="elasticBidSearchForm:bidSearchParametersComponent:j_idt323" value="" />
      </form>
      <table><tr>
        <td>220014-DBB</td><td>Lower Buckeye Road - 71st Av to 67th Av</td><td>Maricopa County</td>
        <td>Arizona</td><td>Oct 19, 2021 2:00:00 PM MST</td><td>Bid has ended</td>
        <td><a href="https://www.bidsync.com/DPX?ac=awgroupreview&auc=2082474&supsrch=">Tab</a></td>
      </tr></table>
    </body></html>
    """
    ajax_xml = """<?xml version="1.0" encoding="utf-8"?>
    <partial-response><changes>
      <update id="elasticBidSearchForm:bidSearchParametersComponent:linksBidSearchResults"><![CDATA[
        <table>
          <tr>
            <td>230049-DBB</td><td>Sun Lakes Pavement Rehab Units 11-22</td><td>Maricopa County</td>
            <td>Arizona</td><td>Jan 31, 2023 2:00:00 PM MST</td><td>Bid has ended</td>
            <td><a href="https://www.bidsync.com/DPX?ac=awgrouplist&auc=2099999&supsrch=">Tab</a></td>
          </tr>
          <tr>
            <td>200025-DBB</td><td>Renovations of Central Courts</td><td>Maricopa County</td>
            <td>Arizona</td><td>Jan 07, 2020 2:00:00 PM MST</td><td>Bid has ended</td>
            <td><a href="https://www.bidsync.com/DPX?ac=awgroupreview&auc=2055555&supsrch=">Tab</a></td>
          </tr>
        </table>
      ]]></update>
      <update id="j_id1:javax.faces.ViewState:0"><![CDATA[vs-1]]></update>
    </changes></partial-response>
    """
    get_resp = MagicMock(ok=True, text=page0_html)
    get_resp.raise_for_status = MagicMock()
    post_init_resp = MagicMock(ok=True, text=page0_html)
    post_init_resp.raise_for_status = MagicMock()
    post_ajax_resp = MagicMock(ok=True, text=ajax_xml)

    mock_sess = MagicMock()
    mock_sess.headers = {}
    mock_sess.get.return_value = get_resp
    mock_sess.post.side_effect = [post_init_resp, post_ajax_resp]

    listings = fetch_bidsync_listings(limit=10, session=mock_sess)
    assert [l.project_num for l in listings] == ["220014-DBB", "230049-DBB"]
    assert "ac=awgroupreview" in listings[1].tab_url
    assert "expandall=Y" in listings[1].tab_url


