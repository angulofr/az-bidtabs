from src.normalize import normalize_contractor_name, normalize_item_code, normalize_unit


def test_normalize_contractor_name():
    assert normalize_contractor_name("Fann Contracting, Inc.") == "FANN CONTRACTING"
    assert normalize_contractor_name("Fisher Sand & Gravel Co., LLC") == "FISHER SAND AND GRAVEL"
    assert normalize_contractor_name("Achen-Gardner Construction, LLC") == "ACHEN GARDNER CONSTRUCTION"
    assert normalize_contractor_name("Combs Construction Company, Inc.") == "COMBS CONSTRUCTION"


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
