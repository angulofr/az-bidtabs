from pathlib import Path

import pandas as pd
import pytest

from src.matching import benchmark_schedule
from src.seed import seed_database


def test_benchmark_schedule_exact_fuzzy_and_flags(tmp_path: Path):
    db_path = tmp_path / "test_match.db"
    seed_database(db_path)

    sched = pd.DataFrame(
        [
            {
                "item_code": "2030301",
                "description": "ROADWAY EXCAVATION",
                "unit": "CU.YD.",
                "spec_system": "ADOT",
                "quantity": 10000,
                "your_unit_price": 45.00,  # Way above ~$16-18 median -> HIGH
            },
            {
                "item_code": "6010002",
                "description": "STRUCTURAL CONCRETE",
                "unit": "CU.YD.",
                "spec_system": "ADOT",
                "quantity": 200,
                "your_unit_price": 400.00,  # Way below ~$850 median -> LOW
            },
            {
                "item_code": "CUSTOM-WL",
                "description": "WATERLINE 12 INCH DIP CLASS 350",
                "unit": "LF",
                "spec_system": "",
                "quantity": 1500,
                "your_unit_price": 165.00,  # Should fuzzy match MAG 610-1
            },
        ]
    )

    res = benchmark_schedule(sched, db_path, threshold_pct=20.0)
    assert len(res) == 3
    assert res.iloc[0]["match_type"] == "EXACT CODE"
    assert res.iloc[0]["flag"].startswith("HIGH")
    assert res.iloc[1]["flag"].startswith("LOW")
    assert res.iloc[2]["match_type"].startswith("FUZZY")
    assert res.iloc[2]["matched_code"] == "610-1"


def test_benchmark_schedule_missing_columns(tmp_path: Path):
    db_path = tmp_path / "test_match_err.db"
    seed_database(db_path)
    bad = pd.DataFrame([{"item_code": "2030301", "quantity": 100}])
    with pytest.raises(ValueError):
        benchmark_schedule(bad, db_path)
