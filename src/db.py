"""SQLite database layer for AZ BidTabs (Postgres-ready schema)."""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import numpy as np
import pandas as pd

from src.normalize import (
    normalize_contractor_name,
    normalize_item_code,
    normalize_unit,
)

DEFAULT_DB_PATH = Path(os.environ.get("AZ_BIDTABS_DB", "data/az_bidtabs.db"))

SCHEMA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS contractors (
    contractor_id INTEGER PRIMARY KEY AUTOINCREMENT,
    canonical_name TEXT NOT NULL UNIQUE,
    az_roc_license TEXT,
    approx_employees INTEGER
);

CREATE TABLE IF NOT EXISTS projects (
    project_id INTEGER PRIMARY KEY AUTOINCREMENT,
    agency TEXT NOT NULL,
    agency_project_num TEXT NOT NULL,
    title TEXT NOT NULL,
    route_or_location TEXT,
    county TEXT,
    district TEXT,
    letting_date TEXT,
    award_date TEXT,
    engineers_estimate REAL,
    winning_bid_total REAL,
    awarded_contractor_id INTEGER REFERENCES contractors(contractor_id),
    source_url TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(agency, agency_project_num)
);

CREATE TABLE IF NOT EXISTS pay_items (
    item_id INTEGER PRIMARY KEY AUTOINCREMENT,
    spec_system TEXT NOT NULL CHECK (spec_system IN ('ADOT', 'MAG')),
    item_code TEXT NOT NULL,
    description TEXT NOT NULL,
    unit TEXT NOT NULL,
    category TEXT,
    UNIQUE(spec_system, item_code, unit)
);

CREATE TABLE IF NOT EXISTS bid_lines (
    bid_line_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    contractor_id INTEGER REFERENCES contractors(contractor_id),
    is_engineers_estimate INTEGER NOT NULL DEFAULT 0 CHECK (is_engineers_estimate IN (0, 1)),
    item_id INTEGER NOT NULL REFERENCES pay_items(item_id),
    quantity REAL NOT NULL,
    unit_price REAL NOT NULL,
    extended_total REAL NOT NULL,
    bid_rank INTEGER,
    is_winner INTEGER NOT NULL DEFAULT 0 CHECK (is_winner IN (0, 1))
);

CREATE INDEX IF NOT EXISTS idx_projects_agency ON projects(agency);
CREATE INDEX IF NOT EXISTS idx_projects_county ON projects(county);
CREATE INDEX IF NOT EXISTS idx_projects_letting ON projects(letting_date);
CREATE INDEX IF NOT EXISTS idx_pay_items_code ON pay_items(item_code);
CREATE INDEX IF NOT EXISTS idx_pay_items_spec ON pay_items(spec_system);
CREATE INDEX IF NOT EXISTS idx_bid_lines_project ON bid_lines(project_id);
CREATE INDEX IF NOT EXISTS idx_bid_lines_item ON bid_lines(item_id);
CREATE INDEX IF NOT EXISTS idx_bid_lines_contractor ON bid_lines(contractor_id);
"""


def get_connection(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


@contextmanager
def db_session(db_path: Path | str = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path | str = DEFAULT_DB_PATH) -> None:
    with db_session(db_path) as conn:
        conn.executescript(SCHEMA_SQL)
        # Safe migration for existing SQLite databases
        cols = {r[1] for r in conn.execute("PRAGMA table_info(contractors)").fetchall()}
        if "approx_employees" not in cols:
            conn.execute("ALTER TABLE contractors ADD COLUMN approx_employees INTEGER")


def upsert_contractor(
    conn: sqlite3.Connection,
    raw_name: str,
    az_roc_license: str | None = None,
    approx_employees: int | None = None,
) -> int:
    canonical = normalize_contractor_name(raw_name)
    if not canonical:
        raise ValueError("Contractor name cannot be empty.")
    row = conn.execute(
        "SELECT contractor_id, az_roc_license, approx_employees FROM contractors WHERE canonical_name = ?",
        (canonical,),
    ).fetchone()
    if row:
        if az_roc_license and not row["az_roc_license"]:
            conn.execute(
                "UPDATE contractors SET az_roc_license = ? WHERE contractor_id = ?",
                (az_roc_license, row["contractor_id"]),
            )
        if approx_employees is not None and row["approx_employees"] is None:
            conn.execute(
                "UPDATE contractors SET approx_employees = ? WHERE contractor_id = ?",
                (int(approx_employees), row["contractor_id"]),
            )
        return int(row["contractor_id"])
    cur = conn.execute(
        "INSERT INTO contractors (canonical_name, az_roc_license, approx_employees) VALUES (?, ?, ?)",
        (canonical, az_roc_license, int(approx_employees) if approx_employees is not None else None),
    )
    return int(cur.lastrowid)


def upsert_pay_item(
    conn: sqlite3.Connection,
    spec_system: str,
    item_code: str,
    description: str,
    unit: str,
    category: str | None = None,
) -> int:
    spec = spec_system.upper().strip()
    if spec not in ("ADOT", "MAG"):
        raise ValueError(f"Unsupported spec_system: {spec_system}")
    code = normalize_item_code(item_code)
    norm_unit = normalize_unit(unit)
    row = conn.execute(
        "SELECT item_id FROM pay_items WHERE spec_system = ? AND item_code = ? AND unit = ?",
        (spec, code, norm_unit),
    ).fetchone()
    if row:
        return int(row["item_id"])
    cur = conn.execute(
        """
        INSERT INTO pay_items (spec_system, item_code, description, unit, category)
        VALUES (?, ?, ?, ?, ?)
        """,
        (spec, code, description.strip(), norm_unit, category),
    )
    return int(cur.lastrowid)


def upsert_project(
    conn: sqlite3.Connection,
    *,
    agency: str,
    agency_project_num: str,
    title: str,
    route_or_location: str | None = None,
    county: str | None = None,
    district: str | None = None,
    letting_date: str | None = None,
    award_date: str | None = None,
    engineers_estimate: float | None = None,
    winning_bid_total: float | None = None,
    awarded_contractor_id: int | None = None,
    source_url: str | None = None,
) -> int:
    row = conn.execute(
        "SELECT project_id FROM projects WHERE agency = ? AND agency_project_num = ?",
        (agency.strip(), agency_project_num.strip()),
    ).fetchone()
    if row:
        pid = int(row["project_id"])
        conn.execute(
            """
            UPDATE projects
            SET title = ?, route_or_location = ?, county = ?, district = ?,
                letting_date = ?, award_date = ?, engineers_estimate = ?,
                winning_bid_total = ?, awarded_contractor_id = ?, source_url = ?
            WHERE project_id = ?
            """,
            (
                title,
                route_or_location,
                county,
                district,
                letting_date,
                award_date,
                engineers_estimate,
                winning_bid_total,
                awarded_contractor_id,
                source_url,
                pid,
            ),
        )
        return pid
    cur = conn.execute(
        """
        INSERT INTO projects (
            agency, agency_project_num, title, route_or_location, county, district,
            letting_date, award_date, engineers_estimate, winning_bid_total,
            awarded_contractor_id, source_url
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            agency.strip(),
            agency_project_num.strip(),
            title,
            route_or_location,
            county,
            district,
            letting_date,
            award_date,
            engineers_estimate,
            winning_bid_total,
            awarded_contractor_id,
            source_url,
        ),
    )
    return int(cur.lastrowid)


def insert_bid_line(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    contractor_id: int | None,
    is_engineers_estimate: bool,
    item_id: int,
    quantity: float,
    unit_price: float,
    extended_total: float | None = None,
    bid_rank: int | None = None,
    is_winner: bool = False,
) -> int:
    ext = extended_total if extended_total is not None else round(quantity * unit_price, 2)
    cur = conn.execute(
        """
        INSERT INTO bid_lines (
            project_id, contractor_id, is_engineers_estimate, item_id,
            quantity, unit_price, extended_total, bid_rank, is_winner
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_id,
            None if is_engineers_estimate else contractor_id,
            1 if is_engineers_estimate else 0,
            item_id,
            float(quantity),
            float(unit_price),
            float(ext),
            bid_rank,
            1 if is_winner else 0,
        ),
    )
    return int(cur.lastrowid)


# ---------------------------------------------------------------------------
# Analytical Queries
# ---------------------------------------------------------------------------


def get_overview_stats(db_path: Path | str = DEFAULT_DB_PATH) -> dict[str, Any]:
    with db_session(db_path) as conn:
        projects_cnt = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        contractors_cnt = conn.execute("SELECT COUNT(*) FROM contractors").fetchone()[0]
        items_cnt = conn.execute("SELECT COUNT(*) FROM pay_items").fetchone()[0]
        lines_cnt = conn.execute("SELECT COUNT(*) FROM bid_lines").fetchone()[0]
        total_awarded = (
            conn.execute("SELECT COALESCE(SUM(winning_bid_total), 0) FROM projects").fetchone()[0]
        )
        agencies = pd.read_sql_query(
            """
            SELECT agency, COUNT(*) AS projects, COALESCE(SUM(winning_bid_total), 0) AS awarded_volume
            FROM projects
            GROUP BY agency
            ORDER BY projects DESC
            """,
            conn,
        )
        recent_projects = pd.read_sql_query(
            """
            SELECT p.agency, p.agency_project_num, p.title, p.county, p.letting_date,
                   p.engineers_estimate, p.winning_bid_total,
                   c.canonical_name AS awarded_contractor,
                   c.approx_employees AS contractor_employees
            FROM projects p
            LEFT JOIN contractors c ON p.awarded_contractor_id = c.contractor_id
            ORDER BY p.letting_date DESC
            LIMIT 15
            """,
            conn,
        )
    return {
        "projects": int(projects_cnt),
        "contractors": int(contractors_cnt),
        "pay_items": int(items_cnt),
        "bid_lines": int(lines_cnt),
        "total_awarded_volume": float(total_awarded),
        "agencies_df": agencies,
        "recent_projects_df": recent_projects,
    }


def get_filter_options(db_path: Path | str = DEFAULT_DB_PATH) -> dict[str, list[str]]:
    with db_session(db_path) as conn:
        agencies = [
            r[0]
            for r in conn.execute("SELECT DISTINCT agency FROM projects ORDER BY agency").fetchall()
        ]
        counties = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT county FROM projects WHERE county IS NOT NULL ORDER BY county"
            ).fetchall()
        ]
        categories = [
            r[0]
            for r in conn.execute(
                "SELECT DISTINCT category FROM pay_items WHERE category IS NOT NULL ORDER BY category"
            ).fetchall()
        ]
        contractors = [
            r[0]
            for r in conn.execute(
                "SELECT canonical_name FROM contractors ORDER BY canonical_name"
            ).fetchall()
        ]
    return {
        "agencies": agencies,
        "counties": counties,
        "categories": categories,
        "contractors": contractors,
    }


def query_pay_item_observations(
    db_path: Path | str = DEFAULT_DB_PATH,
    *,
    search_query: str = "",
    spec_system: str = "ALL",
    agencies: list[str] | None = None,
    counties: list[str] | None = None,
    winners_only: bool = False,
    exclude_engineers_estimate: bool = True,
) -> pd.DataFrame:
    sql = """
        SELECT
            bl.bid_line_id,
            pi.item_id,
            pi.spec_system,
            pi.item_code,
            pi.description,
            pi.unit,
            pi.category,
            p.project_id,
            p.agency,
            p.agency_project_num,
            p.title AS project_title,
            p.county,
            p.letting_date,
            COALESCE(c.canonical_name, 'ENGINEER''S ESTIMATE') AS bidder_name,
            c.approx_employees AS bidder_employees,
            bl.is_engineers_estimate,
            bl.quantity,
            bl.unit_price,
            bl.extended_total,
            bl.bid_rank,
            bl.is_winner
        FROM bid_lines bl
        JOIN pay_items pi ON bl.item_id = pi.item_id
        JOIN projects p ON bl.project_id = p.project_id
        LEFT JOIN contractors c ON bl.contractor_id = c.contractor_id
        WHERE 1=1
    """
    params: list[Any] = []

    if search_query.strip():
        q = f"%{search_query.strip()}%"
        sql += " AND (pi.item_code LIKE ? OR pi.description LIKE ?)"
        params.extend([q, q])

    if spec_system in ("ADOT", "MAG"):
        sql += " AND pi.spec_system = ?"
        params.append(spec_system)

    if agencies:
        placeholders = ",".join("?" for _ in agencies)
        sql += f" AND p.agency IN ({placeholders})"
        params.extend(agencies)

    if counties:
        placeholders = ",".join("?" for _ in counties)
        sql += f" AND p.county IN ({placeholders})"
        params.extend(counties)

    if exclude_engineers_estimate:
        sql += " AND bl.is_engineers_estimate = 0"

    if winners_only:
        sql += " AND bl.is_winner = 1"

    sql += " ORDER BY p.letting_date DESC, pi.item_code, bl.bid_rank"

    with db_session(db_path) as conn:
        df = pd.read_sql_query(sql, conn, params=params)
    return df


def compute_unit_price_stats(df: pd.DataFrame) -> dict[str, float | int]:
    if df.empty:
        return {
            "observations": 0,
            "p25": 0.0,
            "median": 0.0,
            "p75": 0.0,
            "qty_weighted_avg": 0.0,
        }
    prices = df["unit_price"].astype(float).to_numpy()
    qtys = df["quantity"].astype(float).to_numpy()
    total_qty = float(np.sum(qtys))
    qw_avg = float(np.sum(prices * qtys) / total_qty) if total_qty > 0 else float(np.mean(prices))
    return {
        "observations": int(len(df)),
        "p25": float(np.percentile(prices, 25)),
        "median": float(np.percentile(prices, 50)),
        "p75": float(np.percentile(prices, 75)),
        "qty_weighted_avg": qw_avg,
    }


def get_contractor_scorecard(db_path: Path | str = DEFAULT_DB_PATH) -> pd.DataFrame:
    """Compute contractor win-rate, avg % vs Engineer's Estimate, and bid volume."""
    sql = """
        WITH project_bids AS (
            SELECT
                bl.project_id,
                bl.contractor_id,
                c.canonical_name AS contractor_name,
                c.approx_employees,
                c.az_roc_license,
                p.agency,
                p.engineers_estimate,
                SUM(bl.extended_total) AS bidder_total,
                MAX(bl.is_winner) AS is_winner,
                MIN(bl.bid_rank) AS bid_rank
            FROM bid_lines bl
            JOIN contractors c ON bl.contractor_id = c.contractor_id
            JOIN projects p ON bl.project_id = p.project_id
            WHERE bl.is_engineers_estimate = 0
            GROUP BY bl.project_id, bl.contractor_id
        )
        SELECT
            contractor_id,
            contractor_name,
            approx_employees,
            COALESCE(az_roc_license, '—') AS az_roc_license,
            COUNT(*) AS total_bids,
            SUM(is_winner) AS wins,
            ROUND(100.0 * SUM(is_winner) / COUNT(*), 1) AS win_rate_pct,
            ROUND(AVG(CASE WHEN engineers_estimate > 0
                THEN 100.0 * (bidder_total - engineers_estimate) / engineers_estimate
                ELSE NULL END), 2) AS avg_pct_vs_ee,
            ROUND(SUM(bidder_total), 2) AS total_bid_volume,
            ROUND(SUM(CASE WHEN is_winner = 1 THEN bidder_total ELSE 0 END), 2) AS won_volume
        FROM project_bids
        GROUP BY contractor_id, contractor_name
        ORDER BY wins DESC, total_bids DESC
    """
    with db_session(db_path) as conn:
        return pd.read_sql_query(sql, conn)


def get_head_to_head(
    contractor_a: str,
    contractor_b: str,
    db_path: Path | str = DEFAULT_DB_PATH,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (project_level_comparison_df, line_item_comparison_df) for two contractors."""
    with db_session(db_path) as conn:
        proj_sql = """
            WITH bids AS (
                SELECT
                    bl.project_id,
                    c.canonical_name,
                    SUM(bl.extended_total) AS bid_total,
                    MIN(bl.bid_rank) AS bid_rank,
                    MAX(bl.is_winner) AS is_winner
                FROM bid_lines bl
                JOIN contractors c ON bl.contractor_id = c.contractor_id
                WHERE bl.is_engineers_estimate = 0
                  AND c.canonical_name IN (?, ?)
                GROUP BY bl.project_id, c.canonical_name
            )
            SELECT
                p.project_id,
                p.agency,
                p.agency_project_num,
                p.title,
                p.letting_date,
                p.engineers_estimate,
                a.bid_total AS a_total,
                a.bid_rank AS a_rank,
                b.bid_total AS b_total,
                b.bid_rank AS b_rank,
                ROUND(100.0 * (a.bid_total - b.bid_total) / NULLIF(b.bid_total, 0), 2) AS a_vs_b_pct
            FROM projects p
            JOIN bids a ON p.project_id = a.project_id AND a.canonical_name = ?
            JOIN bids b ON p.project_id = b.project_id AND b.canonical_name = ?
            ORDER BY p.letting_date DESC
        """
        proj_df = pd.read_sql_query(
            proj_sql, conn, params=[contractor_a, contractor_b, contractor_a, contractor_b]
        )

        if proj_df.empty:
            return proj_df, pd.DataFrame()

        pids = proj_df["project_id"].tolist()
        placeholders = ",".join("?" for _ in pids)
        line_sql = f"""
            SELECT
                p.agency_project_num,
                p.title AS project_title,
                pi.spec_system,
                pi.item_code,
                pi.description,
                pi.unit,
                a.quantity,
                a.unit_price AS a_unit_price,
                b.unit_price AS b_unit_price,
                ROUND(100.0 * (a.unit_price - b.unit_price) / NULLIF(b.unit_price, 0), 2) AS a_vs_b_pct,
                ROUND((a.unit_price - b.unit_price) * a.quantity, 2) AS extended_diff
            FROM bid_lines a
            JOIN contractors ca ON a.contractor_id = ca.contractor_id AND ca.canonical_name = ?
            JOIN bid_lines b ON a.project_id = b.project_id AND a.item_id = b.item_id
            JOIN contractors cb ON b.contractor_id = cb.contractor_id AND cb.canonical_name = ?
            JOIN projects p ON a.project_id = p.project_id
            JOIN pay_items pi ON a.item_id = pi.item_id
            WHERE a.project_id IN ({placeholders})
              AND a.is_engineers_estimate = 0
              AND b.is_engineers_estimate = 0
            ORDER BY ABS((a.unit_price - b.unit_price) * a.quantity) DESC
        """
        line_df = pd.read_sql_query(line_sql, conn, params=[contractor_a, contractor_b, *pids])
        return proj_df, line_df
