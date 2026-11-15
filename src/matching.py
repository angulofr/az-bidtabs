"""Fuzzy & cross-spec (ADOT <-> MAG) pay item matching and schedule benchmarking."""

from __future__ import annotations

import re
from difflib import SequenceMatcher
from pathlib import Path

import numpy as np
import pandas as pd

from src.db import DEFAULT_DB_PATH, db_session
from src.normalize import normalize_item_code, normalize_unit


def _tokenize(text: str) -> set[str]:
    if not text:
        return set()
    cleaned = re.sub(r"[^A-Z0-9\s]", " ", text.upper())
    return {t for t in cleaned.split() if len(t) > 1}


def _similarity_score(desc_a: str, desc_b: str, unit_a: str = "", unit_b: str = "") -> float:
    """Compute 0-100 similarity score between two pay item descriptions and units."""
    tok_a = _tokenize(desc_a)
    tok_b = _tokenize(desc_b)
    if not tok_a or not tok_b:
        return 0.0
    jaccard = len(tok_a & tok_b) / len(tok_a | tok_b)
    seq = SequenceMatcher(None, desc_a.upper(), desc_b.upper()).ratio()
    score = (0.65 * jaccard + 0.35 * seq) * 100.0

    norm_ua = normalize_unit(unit_a) if unit_a else ""
    norm_ub = normalize_unit(unit_b) if unit_b else ""
    if norm_ua and norm_ub:
        if norm_ua == norm_ub:
            score = min(100.0, score + 10.0)
        else:
            score = max(0.0, score - 15.0)
    return round(score, 1)


def load_item_benchmarks(
    db_path: Path | str = DEFAULT_DB_PATH,
    *,
    winners_only: bool = False,
) -> pd.DataFrame:
    """Load aggregated historical unit-price benchmarks for all pay items in the DB."""
    where_clause = "WHERE bl.is_engineers_estimate = 0"
    if winners_only:
        where_clause += " AND bl.is_winner = 1"

    sql = f"""
        SELECT
            pi.item_id,
            pi.spec_system,
            pi.item_code,
            pi.description,
            pi.unit,
            pi.category,
            bl.quantity,
            bl.unit_price
        FROM bid_lines bl
        JOIN pay_items pi ON bl.item_id = pi.item_id
        {where_clause}
    """
    with db_session(db_path) as conn:
        raw = pd.read_sql_query(sql, conn)

    if raw.empty:
        return pd.DataFrame(
            columns=[
                "item_id",
                "spec_system",
                "item_code",
                "description",
                "unit",
                "category",
                "observations",
                "p25",
                "median",
                "p75",
                "qty_weighted_avg",
            ]
        )

    rows = []
    for (item_id, spec, code, desc, unit, cat), grp in raw.groupby(
        ["item_id", "spec_system", "item_code", "description", "unit", "category"],
        dropna=False,
    ):
        prices = grp["unit_price"].astype(float).to_numpy()
        qtys = grp["quantity"].astype(float).to_numpy()
        total_q = float(np.sum(qtys))
        qw = float(np.sum(prices * qtys) / total_q) if total_q > 0 else float(np.mean(prices))
        rows.append(
            {
                "item_id": int(item_id),
                "spec_system": spec,
                "item_code": code,
                "description": desc,
                "unit": unit,
                "category": cat or "",
                "observations": int(len(grp)),
                "p25": float(np.percentile(prices, 25)),
                "median": float(np.percentile(prices, 50)),
                "p75": float(np.percentile(prices, 75)),
                "qty_weighted_avg": qw,
            }
        )
    return pd.DataFrame(rows)


def benchmark_schedule(
    schedule_df: pd.DataFrame,
    db_path: Path | str = DEFAULT_DB_PATH,
    *,
    winners_only: bool = False,
    threshold_pct: float = 20.0,
    min_fuzzy_score: float = 45.0,
) -> pd.DataFrame:
    """Match an uploaded bid schedule CSV against historical benchmarks and flag >20% variances.

    Expected input columns: `item_code`, `quantity`, `your_unit_price`.
    Optional input columns: `description`, `unit`, `spec_system`.
    """
    benchmarks = load_item_benchmarks(db_path, winners_only=winners_only)

    # Normalize column names
    df = schedule_df.copy()
    df.columns = [c.strip().lower() for c in df.columns]

    required = {"item_code", "quantity", "your_unit_price"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"Missing required CSV columns: {', '.join(sorted(missing))}")

    results = []
    for _, row in df.iterrows():
        raw_code = str(row.get("item_code", "")).strip()
        norm_code = normalize_item_code(raw_code)
        qty = float(row["quantity"])
        your_price = float(row["your_unit_price"])
        desc = str(row.get("description", "") or "").strip()
        unit = str(row.get("unit", "") or "").strip()
        spec = str(row.get("spec_system", "") or "").strip().upper()

        match_row = None
        match_type = "NO MATCH"
        match_score = 0.0

        if not benchmarks.empty:
            # 1. Exact code match (optionally filtered by spec_system)
            code_matches = benchmarks[benchmarks["item_code"] == norm_code]
            if spec in ("ADOT", "MAG") and not code_matches.empty:
                spec_filtered = code_matches[code_matches["spec_system"] == spec]
                if not spec_filtered.empty:
                    code_matches = spec_filtered

            if not code_matches.empty:
                # Pick highest observation count if multiple units exist
                match_row = code_matches.sort_values("observations", ascending=False).iloc[0]
                match_type = "EXACT CODE"
                match_score = 100.0
            elif desc:
                # 2. Fuzzy description + unit cross-spec match
                best_score = -1.0
                best_idx = None
                for idx, cand in benchmarks.iterrows():
                    s = _similarity_score(desc, cand["description"], unit, cand["unit"])
                    if s > best_score:
                        best_score = s
                        best_idx = idx
                if best_idx is not None and best_score >= min_fuzzy_score:
                    match_row = benchmarks.loc[best_idx]
                    match_type = f"FUZZY ({match_row['spec_system']})"
                    match_score = best_score

        if match_row is not None and float(match_row["median"]) > 0:
            hist_median = float(match_row["median"])
            hist_p25 = float(match_row["p25"])
            hist_p75 = float(match_row["p75"])
            hist_qw = float(match_row["qty_weighted_avg"])
            obs = int(match_row["observations"])
            pct_diff = ((your_price - hist_median) / hist_median) * 100.0
            ext_diff = (your_price - hist_median) * qty

            if pct_diff > threshold_pct:
                flag = f"HIGH (>+{threshold_pct:.0f}%)"
            elif pct_diff < -threshold_pct:
                flag = f"LOW (<-{threshold_pct:.0f}%)"
            else:
                flag = "WITHIN RANGE"

            results.append(
                {
                    "input_code": raw_code,
                    "input_description": desc or match_row["description"],
                    "quantity": qty,
                    "your_unit_price": your_price,
                    "your_extended": round(qty * your_price, 2),
                    "match_type": match_type,
                    "match_score": match_score,
                    "matched_spec": match_row["spec_system"],
                    "matched_code": match_row["item_code"],
                    "matched_unit": match_row["unit"],
                    "observations": obs,
                    "hist_p25": round(hist_p25, 2),
                    "hist_median": round(hist_median, 2),
                    "hist_p75": round(hist_p75, 2),
                    "hist_qty_wtd": round(hist_qw, 2),
                    "pct_vs_median": round(pct_diff, 1),
                    "extended_vs_median": round(ext_diff, 2),
                    "flag": flag,
                }
            )
        else:
            results.append(
                {
                    "input_code": raw_code,
                    "input_description": desc,
                    "quantity": qty,
                    "your_unit_price": your_price,
                    "your_extended": round(qty * your_price, 2),
                    "match_type": "NO MATCH",
                    "match_score": 0.0,
                    "matched_spec": "",
                    "matched_code": "",
                    "matched_unit": unit,
                    "observations": 0,
                    "hist_p25": None,
                    "hist_median": None,
                    "hist_p75": None,
                    "hist_qty_wtd": None,
                    "pct_vs_median": None,
                    "extended_vs_median": None,
                    "flag": "UNMATCHED",
                }
            )

    out_df = pd.DataFrame(results)
    for col in [
        "quantity",
        "your_unit_price",
        "your_extended",
        "match_score",
        "observations",
        "hist_p25",
        "hist_median",
        "hist_p75",
        "hist_qty_wtd",
        "pct_vs_median",
        "extended_vs_median",
    ]:
        if col in out_df.columns:
            out_df[col] = pd.to_numeric(out_df[col], errors="coerce")
    return out_df
