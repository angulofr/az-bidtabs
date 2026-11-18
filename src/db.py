"""Dual-backend database layer for AZ BidTabs (Postgres via DATABASE_URL + SQLite fallback)."""

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


def _read_dotenv_var(key: str) -> str | None:
    for fname in (".env.local", ".env"):
        env_path = Path(__file__).resolve().parent.parent / fname
        if env_path.is_file():
            try:
                for line in env_path.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if not line or line.startswith("#") or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    if k.strip() == key:
                        return v.strip().strip('"').strip("'")
            except Exception:
                pass
    return None


def get_database_url() -> str | None:
    """Return Postgres DATABASE_URL from env var, .env.local, or Streamlit secrets."""
    env_url = os.environ.get("DATABASE_URL", "").strip()
    if env_url:
        return env_url
    dotenv_url = _read_dotenv_var("DATABASE_URL")
    if dotenv_url:
        return dotenv_url
    try:
        import streamlit as st

        secret_url = str(st.secrets.get("DATABASE_URL", "")).strip()
        return secret_url or None
    except Exception:
        return None


def is_postgres(db_target: Path | str | None = None) -> bool:
    """Return True if the resolved target is a Postgres connection URI."""
    if db_target is None:
        url = get_database_url()
        return bool(url and url.startswith(("postgres://", "postgresql://")))
    if isinstance(db_target, str):
        return db_target.startswith(("postgres://", "postgresql://"))
    return False


def _adapt_sql(sql: str, postgres: bool) -> str:
    """Adapt SQLite-style `?` placeholders to `%s` for psycopg when running on Postgres."""
    if not postgres:
        return sql
    return sql.replace("?", "%s")


SQLITE_SCHEMA_SQL = """
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

POSTGRES_STATEMENTS = [
    """
    CREATE TABLE IF NOT EXISTS contractors (
        contractor_id SERIAL PRIMARY KEY,
        canonical_name TEXT NOT NULL UNIQUE,
        az_roc_license TEXT,
        approx_employees INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS projects (
        project_id SERIAL PRIMARY KEY,
        agency TEXT NOT NULL,
        agency_project_num TEXT NOT NULL,
        title TEXT NOT NULL,
        route_or_location TEXT,
        county TEXT,
        district TEXT,
        letting_date TEXT,
        award_date TEXT,
        engineers_estimate DOUBLE PRECISION,
        winning_bid_total DOUBLE PRECISION,
        awarded_contractor_id INTEGER REFERENCES contractors(contractor_id),
        source_url TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(agency, agency_project_num)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS pay_items (
        item_id SERIAL PRIMARY KEY,
        spec_system TEXT NOT NULL CHECK (spec_system IN ('ADOT', 'MAG')),
        item_code TEXT NOT NULL,
        description TEXT NOT NULL,
        unit TEXT NOT NULL,
        category TEXT,
        UNIQUE(spec_system, item_code, unit)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS bid_lines (
        bid_line_id SERIAL PRIMARY KEY,
        project_id INTEGER NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
        contractor_id INTEGER REFERENCES contractors(contractor_id),
        is_engineers_estimate INTEGER NOT NULL DEFAULT 0 CHECK (is_engineers_estimate IN (0, 1)),
        item_id INTEGER NOT NULL REFERENCES pay_items(item_id),
        quantity DOUBLE PRECISION NOT NULL,
        unit_price DOUBLE PRECISION NOT NULL,
        extended_total DOUBLE PRECISION NOT NULL,
        bid_rank INTEGER,
        is_winner INTEGER NOT NULL DEFAULT 0 CHECK (is_winner IN (0, 1))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_projects_agency ON projects(agency)",
    "CREATE INDEX IF NOT EXISTS idx_projects_county ON projects(county)",
    "CREATE INDEX IF NOT EXISTS idx_projects_letting ON projects(letting_date)",
    "CREATE INDEX IF NOT EXISTS idx_pay_items_code ON pay_items(item_code)",
    "CREATE INDEX IF NOT EXISTS idx_pay_items_spec ON pay_items(spec_system)",
    "CREATE INDEX IF NOT EXISTS idx_bid_lines_project ON bid_lines(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_bid_lines_item ON bid_lines(item_id)",
    "CREATE INDEX IF NOT EXISTS idx_bid_lines_contractor ON bid_lines(contractor_id)",
    "ALTER TABLE contractors ADD COLUMN IF NOT EXISTS approx_employees INTEGER",
]


class _DBConn:
    """Thin unified wrapper around sqlite3.Connection and psycopg.Connection."""

    def __init__(self, raw_conn: Any, postgres: bool) -> None:
        self._conn = raw_conn
        self.postgres = postgres

    def execute(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> Any:
        adapted = _adapt_sql(sql, self.postgres)
        return self._conn.execute(adapted, params)

    def executemany(self, sql: str, seq_of_params: list[tuple[Any, ...]]) -> Any:
        adapted = _adapt_sql(sql, self.postgres)
        if self.postgres:
            with self._conn.cursor() as cur:
                cur.executemany(adapted, seq_of_params)
            return None
        return self._conn.executemany(adapted, seq_of_params)

    def insert_returning_id(
        self, sql: str, params: tuple[Any, ...] | list[Any], pk_col: str
    ) -> int:
        if self.postgres:
            row = self.execute(f"{sql.rstrip()} RETURNING {pk_col}", params).fetchone()
            return int(row[0])
        cur = self.execute(sql, params)
        return int(cur.lastrowid)

    def query_df(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> pd.DataFrame:
        adapted = _adapt_sql(sql, self.postgres)
        if self.postgres:
            with self._conn.cursor() as cur:
                cur.execute(adapted, params)
                cols = [d.name for d in cur.description] if cur.description else []
                rows = cur.fetchall()
            return pd.DataFrame(rows, columns=cols)
        cur = self._conn.execute(adapted, params)
        cols = [d[0] for d in cur.description] if cur.description else []
        rows = [tuple(r) for r in cur.fetchall()]
        return pd.DataFrame(rows, columns=cols)

    def commit(self) -> None:
        self._conn.commit()

    def rollback(self) -> None:
        self._conn.rollback()

    def close(self) -> None:
        self._conn.close()


def _resolve_target(db_path: Path | str | None) -> tuple[bool, Path | str]:
    if db_path is None:
        pg_url = get_database_url()
        if pg_url:
            return True, pg_url
        return False, DEFAULT_DB_PATH
    if isinstance(db_path, str) and db_path.startswith(("postgres://", "postgresql://")):
        return True, db_path
    return False, Path(db_path)


def get_connection(db_path: Path | str | None = None) -> _DBConn:
    use_pg, target = _resolve_target(db_path)
    if use_pg:
        import psycopg

        raw = psycopg.connect(str(target))
        return _DBConn(raw, postgres=True)
    path = Path(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(str(path), check_same_thread=False)
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA foreign_keys = ON;")
    return _DBConn(raw, postgres=False)


@contextmanager
def db_session(db_path: Path | str | None = None) -> Iterator[_DBConn]:
    conn = get_connection(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: Path | str | None = None) -> None:
    with db_session(db_path) as conn:
        if conn.postgres:
            for stmt in POSTGRES_STATEMENTS:
                conn.execute(stmt)
        else:
            conn._conn.executescript(SQLITE_SCHEMA_SQL)
            cols = {r[1] for r in conn.execute("PRAGMA table_info(contractors)").fetchall()}
            if "approx_employees" not in cols:
                conn.execute("ALTER TABLE contractors ADD COLUMN approx_employees INTEGER")


def upsert_contractor(
    conn: _DBConn,
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
        cid, existing_roc, existing_emp = int(row[0]), row[1], row[2]
        if az_roc_license and not existing_roc:
            conn.execute(
                "UPDATE contractors SET az_roc_license = ? WHERE contractor_id = ?",
                (az_roc_license, cid),
            )
        if approx_employees is not None and existing_emp is None:
            conn.execute(
                "UPDATE contractors SET approx_employees = ? WHERE contractor_id = ?",
                (int(approx_employees), cid),
            )
        return cid
    return conn.insert_returning_id(
        "INSERT INTO contractors (canonical_name, az_roc_license, approx_employees) VALUES (?, ?, ?)",
        (canonical, az_roc_license, int(approx_employees) if approx_employees is not None else None),
        "contractor_id",
    )


def upsert_pay_item(
    conn: _DBConn,
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
        return int(row[0])
    return conn.insert_returning_id(
        """
        INSERT INTO pay_items (spec_system, item_code, description, unit, category)
        VALUES (?, ?, ?, ?, ?)
        """,
        (spec, code, description.strip(), norm_unit, category),
        "item_id",
    )


def upsert_project(
    conn: _DBConn,
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
        pid = int(row[0])
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
    return conn.insert_returning_id(
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
        "project_id",
    )


def insert_bid_line(
    conn: _DBConn,
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
    return conn.insert_returning_id(
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
        "bid_line_id",
    )


# ---------------------------------------------------------------------------
# Analytical Queries
# ---------------------------------------------------------------------------


def get_overview_stats(db_path: Path | str | None = None) -> dict[str, Any]:
    with db_session(db_path) as conn:
        projects_cnt = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        contractors_cnt = conn.execute("SELECT COUNT(*) FROM contractors").fetchone()[0]
        items_cnt = conn.execute("SELECT COUNT(*) FROM pay_items").fetchone()[0]
        lines_cnt = conn.execute("SELECT COUNT(*) FROM bid_lines").fetchone()[0]
        total_awarded = (
            conn.execute("SELECT COALESCE(SUM(winning_bid_total), 0) FROM projects").fetchone()[0]
        )
        agencies = conn.query_df(
            """
            SELECT agency, COUNT(*) AS projects, COALESCE(SUM(winning_bid_total), 0) AS awarded_volume
            FROM projects
            GROUP BY agency
            ORDER BY projects DESC
            """
        )
        recent_projects = conn.query_df(
            """
            SELECT p.agency, p.agency_project_num, p.title, p.county, p.letting_date,
                   p.engineers_estimate, p.winning_bid_total,
                   c.canonical_name AS awarded_contractor,
                   c.approx_employees AS contractor_employees
            FROM projects p
            LEFT JOIN contractors c ON p.awarded_contractor_id = c.contractor_id
            ORDER BY p.letting_date DESC
            LIMIT 15
            """
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


def get_filter_options(db_path: Path | str | None = None) -> dict[str, list[str]]:
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
    db_path: Path | str | None = None,
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
        sql += " AND (UPPER(pi.item_code) LIKE UPPER(?) OR UPPER(pi.description) LIKE UPPER(?))"
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
        df = conn.query_df(sql, params)
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


def get_item_code_details(
    item_code: str,
    db_path: Path | str | None = None,
    *,
    spec_system: str = "ALL",
    agencies: list[str] | None = None,
    counties: list[str] | None = None,
    winners_only: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return (contractor_summary_df, all_lines_df) for an exact pay item code across all contractors."""
    code = normalize_item_code(item_code)
    if not code:
        return pd.DataFrame(), pd.DataFrame()

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
            COALESCE(c.canonical_name, 'DEPARTMENT ESTIMATE') AS bidder_name,
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
        WHERE UPPER(pi.item_code) = ?
    """
    params: list[Any] = [code]

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

    if winners_only:
        sql += " AND (bl.is_winner = 1 OR bl.is_engineers_estimate = 1)"

    sql += " ORDER BY p.letting_date DESC, p.agency_project_num, bl.is_engineers_estimate DESC, bl.bid_rank ASC, bl.unit_price ASC"

    with db_session(db_path) as conn:
        all_lines_df = conn.query_df(sql, params)

    if all_lines_df.empty:
        return pd.DataFrame(), all_lines_df

    for col in ["quantity", "unit_price", "extended_total", "bid_rank", "bidder_employees", "is_engineers_estimate", "is_winner"]:
        if col in all_lines_df.columns:
            all_lines_df[col] = pd.to_numeric(all_lines_df[col], errors="coerce")

    contractor_lines = all_lines_df[all_lines_df["is_engineers_estimate"] == 0]
    if contractor_lines.empty:
        return pd.DataFrame(), all_lines_df

    summary_rows: list[dict[str, Any]] = []
    for cname, grp in contractor_lines.groupby("bidder_name", sort=False):
        prices = grp["unit_price"].astype(float).to_numpy()
        qtys = grp["quantity"].astype(float).to_numpy()
        exts = grp["extended_total"].astype(float).to_numpy()
        total_qty = float(np.sum(qtys))
        qw_avg = float(np.sum(prices * qtys) / total_qty) if total_qty > 0 else float(np.mean(prices))
        emp_vals = grp["bidder_employees"].dropna()
        summary_rows.append(
            {
                "contractor_name": cname,
                "approx_employees": int(emp_vals.iloc[0]) if not emp_vals.empty else None,
                "observations": int(len(grp)),
                "projects": int(grp["project_id"].nunique()),
                "wins": int(grp["is_winner"].fillna(0).sum()),
                "min_unit_price": float(np.min(prices)),
                "median_unit_price": float(np.percentile(prices, 50)),
                "qty_weighted_avg": qw_avg,
                "max_unit_price": float(np.max(prices)),
                "total_quantity": total_qty,
                "total_extended": float(np.sum(exts)),
            }
        )

    contractor_summary_df = pd.DataFrame(summary_rows).sort_values(
        by=["observations", "wins", "median_unit_price"],
        ascending=[False, False, True],
    ).reset_index(drop=True)
    return contractor_summary_df, all_lines_df



def get_contractor_scorecard(db_path: Path | str | None = None) -> pd.DataFrame:
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
            GROUP BY bl.project_id, bl.contractor_id, c.canonical_name, c.approx_employees, c.az_roc_license, p.agency, p.engineers_estimate
        )
        SELECT
            contractor_id,
            contractor_name,
            approx_employees,
            COALESCE(az_roc_license, '—') AS az_roc_license,
            COUNT(*) AS total_bids,
            SUM(is_winner) AS wins,
            CAST(ROUND(CAST(100.0 * SUM(is_winner) / COUNT(*) AS NUMERIC), 1) AS DOUBLE PRECISION) AS win_rate_pct,
            CAST(ROUND(CAST(AVG(CASE WHEN engineers_estimate > 0
                THEN 100.0 * (bidder_total - engineers_estimate) / engineers_estimate
                ELSE NULL END) AS NUMERIC), 2) AS DOUBLE PRECISION) AS avg_pct_vs_ee,
            CAST(ROUND(CAST(SUM(bidder_total) AS NUMERIC), 2) AS DOUBLE PRECISION) AS total_bid_volume,
            CAST(ROUND(CAST(SUM(CASE WHEN is_winner = 1 THEN bidder_total ELSE 0 END) AS NUMERIC), 2) AS DOUBLE PRECISION) AS won_volume
        FROM project_bids
        GROUP BY contractor_id, contractor_name, approx_employees, az_roc_license
        ORDER BY wins DESC, total_bids DESC
    """
    with db_session(db_path) as conn:
        df = conn.query_df(sql)
    for col in ["total_bids", "wins", "win_rate_pct", "avg_pct_vs_ee", "total_bid_volume", "won_volume"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def get_head_to_head_opponents(
    db_path: Path | str | None = None,
) -> dict[str, list[tuple[str, int]]]:
    """Return mapping of contractor -> [(opponent_name, overlapping_projects), ...].

    Only includes contractor pairs that have at least one overlapping non-estimate project.
    """
    sql = """
        WITH contractor_projects AS (
            SELECT DISTINCT bl.project_id, c.canonical_name
            FROM bid_lines bl
            JOIN contractors c ON bl.contractor_id = c.contractor_id
            WHERE bl.is_engineers_estimate = 0
        )
        SELECT
            a.canonical_name AS contractor_a,
            b.canonical_name AS contractor_b,
            COUNT(DISTINCT a.project_id) AS overlapping_projects
        FROM contractor_projects a
        JOIN contractor_projects b
          ON a.project_id = b.project_id
         AND a.canonical_name <> b.canonical_name
        GROUP BY a.canonical_name, b.canonical_name
        ORDER BY a.canonical_name, overlapping_projects DESC, b.canonical_name
    """
    with db_session(db_path) as conn:
        df = conn.query_df(sql)
    if df.empty:
        return {}
    opponents: dict[str, list[tuple[str, int]]] = {}
    for _, row in df.iterrows():
        ca = str(row["contractor_a"])
        cb = str(row["contractor_b"])
        cnt = int(row["overlapping_projects"])
        opponents.setdefault(ca, []).append((cb, cnt))
    return opponents


def get_head_to_head(
    contractor_a: str,
    contractor_b: str,
    db_path: Path | str | None = None,
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
                CAST(ROUND(CAST(100.0 * (a.bid_total - b.bid_total) / NULLIF(b.bid_total, 0) AS NUMERIC), 2) AS DOUBLE PRECISION) AS a_vs_b_pct
            FROM projects p
            JOIN bids a ON p.project_id = a.project_id AND a.canonical_name = ?
            JOIN bids b ON p.project_id = b.project_id AND b.canonical_name = ?
            ORDER BY p.letting_date DESC
        """
        proj_df = conn.query_df(
            proj_sql, [contractor_a, contractor_b, contractor_a, contractor_b]
        )

        if proj_df.empty:
            return proj_df, pd.DataFrame()

        pids = [int(x) for x in proj_df["project_id"].tolist()]
        placeholders = ",".join("?" for _ in pids)
        line_sql = f"""
            WITH a_lines AS (
                SELECT
                    bl.project_id,
                    bl.item_id,
                    bl.quantity,
                    bl.unit_price,
                    ROW_NUMBER() OVER (
                        PARTITION BY bl.project_id, bl.item_id, bl.quantity
                        ORDER BY bl.bid_line_id
                    ) AS rn
                FROM bid_lines bl
                JOIN contractors c ON bl.contractor_id = c.contractor_id
                WHERE c.canonical_name = ?
                  AND bl.is_engineers_estimate = 0
                  AND bl.project_id IN ({placeholders})
            ),
            b_lines AS (
                SELECT
                    bl.project_id,
                    bl.item_id,
                    bl.quantity,
                    bl.unit_price,
                    ROW_NUMBER() OVER (
                        PARTITION BY bl.project_id, bl.item_id, bl.quantity
                        ORDER BY bl.bid_line_id
                    ) AS rn
                FROM bid_lines bl
                JOIN contractors c ON bl.contractor_id = c.contractor_id
                WHERE c.canonical_name = ?
                  AND bl.is_engineers_estimate = 0
                  AND bl.project_id IN ({placeholders})
            )
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
                CAST(ROUND(CAST(100.0 * (a.unit_price - b.unit_price) / NULLIF(b.unit_price, 0) AS NUMERIC), 2) AS DOUBLE PRECISION) AS a_vs_b_pct,
                CAST(ROUND(CAST((a.unit_price - b.unit_price) * a.quantity AS NUMERIC), 2) AS DOUBLE PRECISION) AS extended_diff
            FROM a_lines a
            JOIN b_lines b
              ON a.project_id = b.project_id
             AND a.item_id = b.item_id
             AND a.quantity = b.quantity
             AND a.rn = b.rn
            JOIN projects p ON a.project_id = p.project_id
            JOIN pay_items pi ON a.item_id = pi.item_id
            ORDER BY ABS((a.unit_price - b.unit_price) * a.quantity) DESC
        """
        line_df = conn.query_df(line_sql, [contractor_a, *pids, contractor_b, *pids])
        for col in ["quantity", "a_unit_price", "b_unit_price", "a_vs_b_pct", "extended_diff"]:
            if col in line_df.columns:
                line_df[col] = pd.to_numeric(line_df[col], errors="coerce")
        return proj_df, line_df

