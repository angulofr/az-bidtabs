from src.contractor_profiles import canonicalize_contractor_name
from src.normalize import normalize_contractor_name, normalize_item_code, normalize_unit


def test_normalize_contractor_name():
    assert normalize_contractor_name("Fann Contracting, Inc.") == "FANN CONTRACTING"
    assert normalize_contractor_name("Fisher Sand & Gravel Co., LLC") == "FISHER SAND AND GRAVEL"
    assert normalize_contractor_name("Achen-Gardner Construction, LLC") == "ACHEN GARDNER CONSTRUCTION"
    assert normalize_contractor_name("Combs Construction Company, Inc.") == "COMBS CONSTRUCTION"
    assert normalize_contractor_name("MARKHAM CONTRACTING CO., INC. ARIZONA") == "MARKHAM CONTRACTING"
    assert normalize_contractor_name("MARKHAM CONTRACTING CO INC PHOENIX, AZ") == "MARKHAM CONTRACTING"
    assert canonicalize_contractor_name("Markham") == "MARKHAM CONTRACTING"
    assert canonicalize_contractor_name("C S CONSTRUCTION, INC.") == "CS CONSTRUCTION"
    assert canonicalize_contractor_name("Sundt / Rummel Joint Venture") == "SUNDT RUMMEL JOINT VENTURE"


def test_normalize_unit():
    assert normalize_unit("CY") == "CU.YD."
    assert normalize_unit("cu yd") == "CU.YD."
    assert normalize_unit("LF") == "L.FT."
    assert normalize_unit("Linear Feet") == "L.FT."
    assert normalize_unit("LS") == "L.SUM"
    assert normalize_unit("Lump Sum") == "L.SUM"
    assert normalize_unit("TONS") == "TON"
    assert normalize_unit("EA") == "EACH"
    assert normalize_unit("SY") == "SQ.YD."
    assert normalize_unit("SF") == "SQ.FT."


def test_normalize_item_code():
    assert normalize_item_code(" 2030301 ") == "2030301"
    assert normalize_item_code("340 - 1") == "340-1"
