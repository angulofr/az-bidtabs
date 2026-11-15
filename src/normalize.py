"""Normalization utilities for contractor names, units of measure, and item codes."""

import re

_LEGAL_SUFFIXES = {
    "INC",
    "INCORPORATED",
    "LLC",
    "CORP",
    "CORPORATION",
    "CO",
    "COMPANY",
    "LTD",
    "LIMITED",
    "LP",
    "LLP",
    "PLLC",
    "PC",
    "PLC",
}

_UNIT_MAP = {
    # Cubic Yards
    "CY": "CU.YD.",
    "C.Y": "CU.YD.",
    "C.Y.": "CU.YD.",
    "CU YD": "CU.YD.",
    "CU YDS": "CU.YD.",
    "CU.YD": "CU.YD.",
    "CU.YD.": "CU.YD.",
    "CU. YD.": "CU.YD.",
    "CUBIC YARD": "CU.YD.",
    "CUBIC YARDS": "CU.YD.",
    # Linear Feet
    "LF": "L.FT.",
    "L.F": "L.FT.",
    "L.F.": "L.FT.",
    "LIN FT": "L.FT.",
    "LIN. FT.": "L.FT.",
    "LIN.FT.": "L.FT.",
    "L.FT": "L.FT.",
    "L.FT.": "L.FT.",
    "FT": "L.FT.",
    "FEET": "L.FT.",
    "FOOT": "L.FT.",
    "LINEAR FOOT": "L.FT.",
    "LINEAR FEET": "L.FT.",
    # Lump Sum
    "LS": "L.SUM",
    "L.S": "L.SUM",
    "L.S.": "L.SUM",
    "L SUM": "L.SUM",
    "L.SUM": "L.SUM",
    "L.SUM.": "L.SUM",
    "LUMP SUM": "L.SUM",
    "LUMP": "L.SUM",
    # Ton
    "TON": "TON",
    "TONS": "TON",
    "TN": "TON",
    # Each
    "EA": "EACH",
    "EA.": "EACH",
    "EACH": "EACH",
    # Square Yards
    "SY": "SQ.YD.",
    "S.Y": "SQ.YD.",
    "S.Y.": "SQ.YD.",
    "SQ YD": "SQ.YD.",
    "SQ YDS": "SQ.YD.",
    "SQ.YD": "SQ.YD.",
    "SQ.YD.": "SQ.YD.",
    "SQ. YD.": "SQ.YD.",
    "SQUARE YARD": "SQ.YD.",
    "SQUARE YARDS": "SQ.YD.",
    # Square Feet
    "SF": "SQ.FT.",
    "S.F": "SQ.FT.",
    "S.F.": "SQ.FT.",
    "SQ FT": "SQ.FT.",
    "SQ.FT": "SQ.FT.",
    "SQ.FT.": "SQ.FT.",
    "SQ. FT.": "SQ.FT.",
    "SQUARE FOOT": "SQ.FT.",
    "SQUARE FEET": "SQ.FT.",
    # Cubic Feet
    "CF": "CU.FT.",
    "C.F": "CU.FT.",
    "C.F.": "CU.FT.",
    "CU FT": "CU.FT.",
    "CU.FT": "CU.FT.",
    "CU.FT.": "CU.FT.",
    "CU. FT.": "CU.FT.",
    "CUBIC FOOT": "CU.FT.",
    "CUBIC FEET": "CU.FT.",
    # Other common heavy-civil units
    "EACH-DAY": "EACH-DAY",
    "EACH/DAY": "EACH-DAY",
    "EA-DAY": "EACH-DAY",
    "EA/DAY": "EACH-DAY",
    "EACH DAY": "EACH-DAY",
    "L.FT.-DAY": "L.FT.-DAY",
    "L.FT-DAY": "L.FT.-DAY",
    "L.FT/DAY": "L.FT.-DAY",
    "LF-DAY": "L.FT.-DAY",
    "LF/DAY": "L.FT.-DAY",
    "L.F.-DAY": "L.FT.-DAY",
    "HR": "HOUR",
    "HRS": "HOUR",
    "HOUR": "HOUR",
    "HOURS": "HOUR",
    "GAL": "GAL",
    "GALLON": "GAL",
    "GALLONS": "GAL",
    "MGAL": "M.GAL",
    "M.GAL": "M.GAL",
    "DAY": "DAY",
    "DAYS": "DAY",
    "DY": "DAY",
    "AC": "ACRE",
    "ACRE": "ACRE",
    "ACRES": "ACRE",
    "LB": "LB",
    "LBS": "LB",
    "POUND": "LB",
    "POUNDS": "LB",
    "MO": "MONTH",
    "MOS": "MONTH",
    "MONTH": "MONTH",
    "MONTHS": "MONTH",
    "FA": "F.A.",
    "F.A.": "F.A.",
    "FORCE ACCOUNT": "F.A.",
    "DOL": "DOLLAR",
    "DOLLAR": "DOLLAR",
    "DOLLARS": "DOLLAR",
}


def normalize_contractor_name(raw_name: str) -> str:
    """Normalize contractor name to canonical uppercase without punctuation or legal suffixes.

    Example:
        "Fann Contracting, Inc." -> "FANN CONTRACTING"
        "Fisher Sand & Gravel Co., LLC" -> "FISHER SAND AND GRAVEL"
    """
    if not raw_name:
        return ""
    name = raw_name.upper().strip()
    name = name.replace("&", " AND ")
    # Remove punctuation (keep alphanumeric and spaces)
    name = re.sub(r"[^\w\s]", " ", name)
    tokens = name.split()

    # Strip trailing legal suffixes (e.g., "CO INC", "LLC")
    while tokens and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()

    return " ".join(tokens)


def normalize_unit(raw_unit: str) -> str:
    """Normalize unit of measure to standard heavy-civil abbreviations."""
    if not raw_unit:
        return ""
    cleaned = re.sub(r"\s+", " ", raw_unit.upper().strip())
    if cleaned in _UNIT_MAP:
        return _UNIT_MAP[cleaned]
    # Try stripping trailing period
    no_dot = cleaned.rstrip(".")
    if no_dot in _UNIT_MAP:
        return _UNIT_MAP[no_dot]
    return cleaned


def normalize_item_code(raw_code: str) -> str:
    """Normalize pay item code (trim whitespace, uppercase)."""
    if not raw_code:
        return ""
    return re.sub(r"\s+", "", raw_code.upper().strip())
