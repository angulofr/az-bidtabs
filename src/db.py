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

CREATE TABLE IF NOT EXISTS project_bids (
    project_bid_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    contractor_id INTEGER REFERENCES contractors(contractor_id),
    is_engineers_estimate INTEGER NOT NULL DEFAULT 0 CHECK (is_engineers_estimate IN (0, 1)),
    bid_total REAL NOT NULL,
    bid_rank INTEGER,
    is_winner INTEGER NOT NULL DEFAULT 0 CHECK (is_winner IN (0, 1))
);

CREATE TABLE IF NOT EXISTS project_documents (
    doc_id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id INTEGER NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
    doc_type TEXT NOT NULL,
    title TEXT,
    source_url TEXT NOT NULL,
    archive_url TEXT,
    local_path TEXT,
    bucket TEXT,
    asset_name TEXT,
    sha256 TEXT,
    file_size INTEGER,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(project_id, doc_type, source_url)
);

CREATE INDEX IF NOT EXISTS idx_projects_agency ON projects(agency);
CREATE INDEX IF NOT EXISTS idx_projects_county ON projects(county);
CREATE INDEX IF NOT EXISTS idx_projects_letting ON projects(letting_date);
CREATE INDEX IF NOT EXISTS idx_pay_items_code ON pay_items(item_code);
CREATE INDEX IF NOT EXISTS idx_pay_items_spec ON pay_items(spec_system);
CREATE INDEX IF NOT EXISTS idx_bid_lines_project ON bid_lines(project_id);
CREATE INDEX IF NOT EXISTS idx_bid_lines_item ON bid_lines(item_id);
CREATE INDEX IF NOT EXISTS idx_bid_lines_contractor ON bid_lines(contractor_id);
CREATE INDEX IF NOT EXISTS idx_project_bids_project ON project_bids(project_id);
CREATE INDEX IF NOT EXISTS idx_project_bids_contractor ON project_bids(contractor_id);
CREATE INDEX IF NOT EXISTS idx_project_documents_project ON project_documents(project_id);
CREATE INDEX IF NOT EXISTS idx_project_documents_sha256 ON project_documents(sha256);
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
    """
    CREATE TABLE IF NOT EXISTS project_bids (
        project_bid_id SERIAL PRIMARY KEY,
        project_id INTEGER NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
        contractor_id INTEGER REFERENCES contractors(contractor_id),
        is_engineers_estimate INTEGER NOT NULL DEFAULT 0 CHECK (is_engineers_estimate IN (0, 1)),
        bid_total DOUBLE PRECISION NOT NULL,
        bid_rank INTEGER,
        is_winner INTEGER NOT NULL DEFAULT 0 CHECK (is_winner IN (0, 1))
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_project_bids_project ON project_bids(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_project_bids_contractor ON project_bids(contractor_id)",
    """
    CREATE TABLE IF NOT EXISTS project_documents (
        doc_id SERIAL PRIMARY KEY,
        project_id INTEGER NOT NULL REFERENCES projects(project_id) ON DELETE CASCADE,
        doc_type TEXT NOT NULL,
        title TEXT,
        source_url TEXT NOT NULL,
        archive_url TEXT,
        local_path TEXT,
        bucket TEXT,
        asset_name TEXT,
        sha256 TEXT,
        file_size INTEGER,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(project_id, doc_type, source_url)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_project_documents_project ON project_documents(project_id)",
    "CREATE INDEX IF NOT EXISTS idx_project_documents_sha256 ON project_documents(sha256)",
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
    from src.contractor_profiles import canonicalize_contractor_name, lookup_contractor_profile

    canonical = canonicalize_contractor_name(raw_name) or normalize_contractor_name(raw_name)
    if not canonical:
        raise ValueError("Contractor name cannot be empty.")
    if az_roc_license is None or approx_employees is None:
        prof_roc, prof_emp = lookup_contractor_profile(canonical)
        if az_roc_license is None:
            az_roc_license = prof_roc
        if approx_employees is None:
            approx_employees = prof_emp
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
            SET title = COALESCE(?, title),
                route_or_location = COALESCE(?, route_or_location),
                county = COALESCE(?, county),
                district = COALESCE(?, district),
                letting_date = COALESCE(?, letting_date),
                award_date = COALESCE(?, award_date),
                engineers_estimate = COALESCE(?, engineers_estimate),
                winning_bid_total = COALESCE(?, winning_bid_total),
                awarded_contractor_id = COALESCE(?, awarded_contractor_id),
                source_url = COALESCE(?, source_url)
            WHERE project_id = ?
            """,
            (
                title or None,
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


def upsert_project_bid(
    conn: _DBConn,
    *,
    project_id: int,
    contractor_id: int | None,
    is_engineers_estimate: bool,
    bid_total: float,
    bid_rank: int | None = None,
    is_winner: bool = False,
) -> int:
    """Insert or update a project-level bid summary (for lump-sum / as-read / STB awards)."""
    est_flag = 1 if is_engineers_estimate else 0
    cid = None if is_engineers_estimate else contractor_id
    win_flag = 1 if (is_winner and not is_engineers_estimate) else 0

    if cid is None:
        existing = conn.execute(
            "SELECT project_bid_id FROM project_bids WHERE project_id = ? AND contractor_id IS NULL AND is_engineers_estimate = ?",
            (project_id, est_flag),
        ).fetchone()
    else:
        existing = conn.execute(
            "SELECT project_bid_id FROM project_bids WHERE project_id = ? AND contractor_id = ? AND is_engineers_estimate = ?",
            (project_id, cid, est_flag),
        ).fetchone()

    if existing:
        pbid = int(existing[0])
        conn.execute(
            """
            UPDATE project_bids
            SET bid_total = ?,
                bid_rank = COALESCE(?, bid_rank),
                is_winner = CASE WHEN ? = 1 THEN 1 ELSE is_winner END
            WHERE project_bid_id = ?
            """,
            (float(bid_total), bid_rank, win_flag, pbid),
        )
        return pbid

    return conn.insert_returning_id(
        """
        INSERT INTO project_bids (
            project_id, contractor_id, is_engineers_estimate, bid_total, bid_rank, is_winner
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (project_id, cid, est_flag, float(bid_total), bid_rank, win_flag),
        "project_bid_id",
    )


def upsert_project_document(
    conn: _DBConn,
    *,
    project_id: int,
    doc_type: str,
    source_url: str,
    title: str | None = None,
    archive_url: str | None = None,
    local_path: str | None = None,
    bucket: str | None = None,
    asset_name: str | None = None,
    sha256: str | None = None,
    file_size: int | None = None,
) -> int:
    """Insert or update a PDF / external source document linked to a project."""
    if not source_url:
        raise ValueError("source_url is required for upsert_project_document")
    existing = conn.execute(
        "SELECT doc_id FROM project_documents WHERE project_id = ? AND doc_type = ? AND source_url = ?",
        (project_id, doc_type.strip(), source_url.strip()),
    ).fetchone()
    if existing:
        did = int(existing[0])
        conn.execute(
            """
            UPDATE project_documents
            SET title = COALESCE(?, title),
                archive_url = COALESCE(?, archive_url),
                local_path = COALESCE(?, local_path),
                bucket = COALESCE(?, bucket),
                asset_name = COALESCE(?, asset_name),
                sha256 = COALESCE(?, sha256),
                file_size = COALESCE(?, file_size)
            WHERE doc_id = ?
            """,
            (title, archive_url, local_path, bucket, asset_name, sha256, file_size, did),
        )
        return did
    return conn.insert_returning_id(
        """
        INSERT INTO project_documents (
            project_id, doc_type, title, source_url, archive_url,
            local_path, bucket, asset_name, sha256, file_size
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            project_id,
            doc_type.strip(),
            title,
            source_url.strip(),
            archive_url,
            local_path,
            bucket,
            asset_name,
            sha256,
            int(file_size) if file_size is not None else None,
        ),
        "doc_id",
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
        WITH itemized_bids AS (
            SELECT
                bl.project_id,
                bl.contractor_id,
                SUM(bl.extended_total) AS bidder_total,
                MAX(bl.is_winner) AS is_winner,
                MIN(bl.bid_rank) AS bid_rank
            FROM bid_lines bl
            WHERE bl.is_engineers_estimate = 0 AND bl.contractor_id IS NOT NULL
            GROUP BY bl.project_id, bl.contractor_id
        ),
        summary_bids AS (
            SELECT
                pb.project_id,
                pb.contractor_id,
                pb.bid_total AS bidder_total,
                pb.is_winner,
                pb.bid_rank
            FROM project_bids pb
            WHERE pb.is_engineers_estimate = 0
              AND pb.contractor_id IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM itemized_bids ib
                  WHERE ib.project_id = pb.project_id AND ib.contractor_id = pb.contractor_id
              )
        ),
        combined_project_bids AS (
            SELECT
                cb.project_id,
                cb.contractor_id,
                c.canonical_name AS contractor_name,
                c.approx_employees,
                c.az_roc_license,
                p.agency,
                p.engineers_estimate,
                cb.bidder_total,
                cb.is_winner,
                cb.bid_rank
            FROM (
                SELECT * FROM itemized_bids
                UNION ALL
                SELECT * FROM summary_bids
            ) cb
            JOIN contractors c ON cb.contractor_id = c.contractor_id
            JOIN projects p ON cb.project_id = p.project_id
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
        FROM combined_project_bids
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


def get_all_projects(
    db_path: Path | str | None = None,
    *,
    agency: str = "ALL",
    county: str = "ALL",
    search: str = "",
    awarded_only: bool = False,
) -> pd.DataFrame:
    """Return archived projects with awarded contractor & line-item counts."""
    sql = """
        WITH line_counts AS (
            SELECT project_id, COUNT(*) AS bid_line_count
            FROM bid_lines
            GROUP BY project_id
        ),
        doc_summary AS (
            SELECT
                project_id,
                COUNT(*) AS doc_count,
                MAX(archive_url) AS archive_url
            FROM project_documents
            GROUP BY project_id
        )
        SELECT
            p.project_id,
            p.agency,
            p.agency_project_num,
            p.title,
            p.county,
            p.letting_date,
            p.engineers_estimate,
            p.winning_bid_total,
            c.canonical_name AS awarded_contractor,
            c.approx_employees AS contractor_employees,
            COALESCE(lc.bid_line_count, 0) AS bid_line_count,
            COALESCE(ds.doc_count, 0) AS doc_count,
            COALESCE(ds.archive_url, p.source_url) AS source_url
        FROM projects p
        LEFT JOIN contractors c ON p.awarded_contractor_id = c.contractor_id
        LEFT JOIN line_counts lc ON p.project_id = lc.project_id
        LEFT JOIN doc_summary ds ON p.project_id = ds.project_id
        WHERE 1=1
    """
    params: list[Any] = []
    if agency and agency != "ALL":
        sql += " AND p.agency = ?"
        params.append(agency)
    if county and county != "ALL":
        sql += " AND p.county = ?"
        params.append(county)
    if awarded_only:
        sql += " AND COALESCE(p.winning_bid_total, 0) > 0"
    if search.strip():
        q = f"%{search.strip()}%"
        sql += (
            " AND (UPPER(p.agency_project_num) LIKE UPPER(?)"
            " OR UPPER(p.title) LIKE UPPER(?)"
            " OR UPPER(COALESCE(c.canonical_name, '')) LIKE UPPER(?))"
        )
        params.extend([q, q, q])

    if awarded_only:
        sql += " ORDER BY COALESCE(p.winning_bid_total, 0) DESC, p.letting_date DESC"
    else:
        sql += (
            " ORDER BY CASE WHEN p.letting_date IS NULL OR p.letting_date = '' THEN 1 ELSE 0 END,"
            " p.letting_date DESC, p.project_id DESC"
        )

    with db_session(db_path) as conn:
        df = conn.query_df(sql, params)
    for col in [
        "engineers_estimate",
        "winning_bid_total",
        "contractor_employees",
        "bid_line_count",
        "doc_count",
    ]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def get_all_contractors(
    db_path: Path | str | None = None,
    *,
    search: str = "",
) -> pd.DataFrame:
    """Return all tracked contractors (both awarded-project owners and itemized bidders)."""
    sql = """
        WITH awarded_stats AS (
            SELECT
                awarded_contractor_id AS contractor_id,
                COUNT(*) AS projects_awarded,
                COALESCE(SUM(winning_bid_total), 0) AS awarded_project_volume
            FROM projects
            WHERE awarded_contractor_id IS NOT NULL
            GROUP BY awarded_contractor_id
        ),
        bid_stats AS (
            SELECT
                contractor_id,
                COUNT(DISTINCT project_id) AS itemized_bids,
                COUNT(DISTINCT CASE WHEN is_winner = 1 THEN project_id END) AS itemized_wins,
                COUNT(*) AS bid_line_count
            FROM (
                SELECT contractor_id, project_id, is_winner FROM bid_lines
                WHERE is_engineers_estimate = 0 AND contractor_id IS NOT NULL
                UNION ALL
                SELECT pb.contractor_id, pb.project_id, pb.is_winner FROM project_bids pb
                WHERE pb.is_engineers_estimate = 0 AND pb.contractor_id IS NOT NULL
            ) all_b
            GROUP BY contractor_id
        ),
        line_only_counts AS (
            SELECT contractor_id, COUNT(*) AS true_line_count
            FROM bid_lines
            WHERE is_engineers_estimate = 0 AND contractor_id IS NOT NULL
            GROUP BY contractor_id
        )
        SELECT
            c.contractor_id,
            c.canonical_name AS contractor_name,
            c.approx_employees,
            COALESCE(c.az_roc_license, '—') AS az_roc_license,
            COALESCE(a.projects_awarded, 0) AS projects_awarded,
            COALESCE(a.awarded_project_volume, 0) AS awarded_volume,
            COALESCE(b.itemized_bids, 0) AS itemized_bids,
            COALESCE(b.itemized_wins, 0) AS itemized_wins,
            COALESCE(loc.true_line_count, 0) AS bid_line_count
        FROM contractors c
        LEFT JOIN awarded_stats a ON c.contractor_id = a.contractor_id
        LEFT JOIN bid_stats b ON c.contractor_id = b.contractor_id
        LEFT JOIN line_only_counts loc ON c.contractor_id = loc.contractor_id
        WHERE 1=1
    """
    params: list[Any] = []
    if search.strip():
        q = f"%{search.strip()}%"
        sql += " AND UPPER(c.canonical_name) LIKE UPPER(?)"
        params.append(q)

    sql += " ORDER BY projects_awarded DESC, itemized_bids DESC, c.canonical_name ASC"
    with db_session(db_path) as conn:
        df = conn.query_df(sql, params)
    for col in [
        "approx_employees",
        "projects_awarded",
        "awarded_volume",
        "itemized_bids",
        "itemized_wins",
        "bid_line_count",
    ]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def get_all_pay_items(
    db_path: Path | str | None = None,
    *,
    spec_system: str = "ALL",
    search: str = "",
) -> pd.DataFrame:
    """Return all unique pay items with observation counts and unit-price ranges."""
    sql = """
        WITH item_stats AS (
            SELECT
                item_id,
                COUNT(*) AS observations,
                CAST(ROUND(CAST(AVG(unit_price) AS NUMERIC), 2) AS DOUBLE PRECISION) AS avg_unit_price,
                MIN(unit_price) AS min_unit_price,
                MAX(unit_price) AS max_unit_price
            FROM bid_lines
            WHERE is_engineers_estimate = 0
            GROUP BY item_id
        )
        SELECT
            pi.item_id,
            pi.spec_system,
            pi.item_code,
            pi.description,
            pi.unit,
            COALESCE(pi.category, '') AS category,
            COALESCE(s.observations, 0) AS observations,
            s.avg_unit_price,
            s.min_unit_price,
            s.max_unit_price
        FROM pay_items pi
        LEFT JOIN item_stats s ON pi.item_id = s.item_id
        WHERE 1=1
    """
    params: list[Any] = []
    if spec_system in ("ADOT", "MAG"):
        sql += " AND pi.spec_system = ?"
        params.append(spec_system)
    if search.strip():
        q = f"%{search.strip()}%"
        sql += " AND (UPPER(pi.item_code) LIKE UPPER(?) OR UPPER(pi.description) LIKE UPPER(?))"
        params.extend([q, q])

    sql += " ORDER BY observations DESC, pi.spec_system ASC, pi.item_code ASC"
    with db_session(db_path) as conn:
        df = conn.query_df(sql, params)
    for col in ["observations", "avg_unit_price", "min_unit_price", "max_unit_price"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def get_contractor_details(
    contractor_name: str,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Return full profile, won projects, submitted bids, agency breakdown, and top items."""
    canonical = normalize_contractor_name(contractor_name) or contractor_name.strip().upper()
    with db_session(db_path) as conn:
        row = conn.execute(
            """
            SELECT contractor_id, canonical_name, az_roc_license, approx_employees
            FROM contractors
            WHERE canonical_name = ?
            """,
            (canonical,),
        ).fetchone()
        if not row:
            return {
                "contractor": None,
                "summary": {},
                "won_projects_df": pd.DataFrame(),
                "all_bids_df": pd.DataFrame(),
                "agency_breakdown_df": pd.DataFrame(),
                "top_items_df": pd.DataFrame(),
            }

        cid = int(row[0])
        contractor_info = {
            "contractor_id": cid,
            "contractor_name": str(row[1]),
            "az_roc_license": row[2],
            "approx_employees": int(row[3]) if row[3] is not None else None,
        }

        won_sql = """
            WITH line_winners AS (
                SELECT
                    project_id,
                    SUM(extended_total) AS bidder_total,
                    COUNT(*) AS line_count
                FROM bid_lines
                WHERE contractor_id = ?
                  AND is_engineers_estimate = 0
                  AND is_winner = 1
                GROUP BY project_id
            ),
            summary_winners AS (
                SELECT project_id, MAX(bid_total) AS bidder_total
                FROM project_bids
                WHERE contractor_id = ?
                  AND is_engineers_estimate = 0
                  AND is_winner = 1
                GROUP BY project_id
            ),
            all_lines AS (
                SELECT project_id, COUNT(*) AS line_count
                FROM bid_lines
                WHERE contractor_id = ? AND is_engineers_estimate = 0
                GROUP BY project_id
            )
            SELECT
                p.project_id,
                p.agency,
                p.agency_project_num,
                p.title,
                p.county,
                p.letting_date,
                p.engineers_estimate,
                COALESCE(p.winning_bid_total, lw.bidder_total, sw.bidder_total) AS winning_bid_total,
                COALESCE(al.line_count, 0) AS bid_line_count,
                p.source_url
            FROM projects p
            LEFT JOIN line_winners lw ON p.project_id = lw.project_id
            LEFT JOIN summary_winners sw ON p.project_id = sw.project_id
            LEFT JOIN all_lines al ON p.project_id = al.project_id
            WHERE p.awarded_contractor_id = ? OR lw.project_id IS NOT NULL OR sw.project_id IS NOT NULL
            ORDER BY CASE WHEN p.letting_date IS NULL OR p.letting_date = '' THEN 1 ELSE 0 END,
                     p.letting_date DESC, p.project_id DESC
        """
        won_df = conn.query_df(won_sql, [cid, cid, cid, cid])

        bids_sql = """
            WITH line_proj_bids AS (
                SELECT
                    bl.project_id,
                    SUM(bl.extended_total) AS bidder_total,
                    MIN(bl.bid_rank) AS bid_rank,
                    MAX(bl.is_winner) AS is_winner,
                    COUNT(*) AS bid_line_count
                FROM bid_lines bl
                WHERE bl.contractor_id = ? AND bl.is_engineers_estimate = 0
                GROUP BY bl.project_id
            ),
            summary_proj_bids AS (
                SELECT
                    pb.project_id,
                    pb.bid_total AS bidder_total,
                    pb.bid_rank,
                    pb.is_winner,
                    0 AS bid_line_count
                FROM project_bids pb
                WHERE pb.contractor_id = ?
                  AND pb.is_engineers_estimate = 0
                  AND NOT EXISTS (
                      SELECT 1 FROM line_proj_bids lp WHERE lp.project_id = pb.project_id
                  )
            ),
            combined AS (
                SELECT * FROM line_proj_bids
                UNION ALL
                SELECT * FROM summary_proj_bids
            )
            SELECT
                p.project_id,
                p.agency,
                p.agency_project_num,
                p.title,
                p.county,
                p.letting_date,
                p.engineers_estimate,
                c.bidder_total,
                CAST(ROUND(CAST(CASE WHEN p.engineers_estimate > 0
                    THEN 100.0 * (c.bidder_total - p.engineers_estimate) / p.engineers_estimate
                    ELSE NULL END AS NUMERIC), 2) AS DOUBLE PRECISION) AS pct_vs_ee,
                c.bid_rank,
                c.is_winner,
                c.bid_line_count,
                p.source_url
            FROM combined c
            JOIN projects p ON c.project_id = p.project_id
            ORDER BY CASE WHEN p.letting_date IS NULL OR p.letting_date = '' THEN 1 ELSE 0 END,
                     p.letting_date DESC, p.project_id DESC
        """
        bids_df = conn.query_df(bids_sql, [cid, cid])

        items_sql = """
            SELECT
                pi.spec_system,
                pi.item_code,
                pi.description,
                pi.unit,
                COUNT(*) AS times_bid,
                CAST(ROUND(CAST(AVG(bl.unit_price) AS NUMERIC), 2) AS DOUBLE PRECISION) AS avg_unit_price,
                CAST(ROUND(CAST(SUM(bl.extended_total) AS NUMERIC), 2) AS DOUBLE PRECISION) AS total_extended
            FROM bid_lines bl
            JOIN pay_items pi ON bl.item_id = pi.item_id
            WHERE bl.contractor_id = ? AND bl.is_engineers_estimate = 0
            GROUP BY pi.item_id, pi.spec_system, pi.item_code, pi.description, pi.unit
            ORDER BY total_extended DESC, times_bid DESC
            LIMIT 100
        """
        items_df = conn.query_df(items_sql, [cid])

    for col in ["engineers_estimate", "winning_bid_total", "bid_line_count"]:
        if col in won_df.columns:
            won_df[col] = pd.to_numeric(won_df[col], errors="coerce")
    for col in ["engineers_estimate", "bidder_total", "pct_vs_ee", "bid_rank", "is_winner", "bid_line_count"]:
        if col in bids_df.columns:
            bids_df[col] = pd.to_numeric(bids_df[col], errors="coerce")
    for col in ["times_bid", "avg_unit_price", "total_extended"]:
        if col in items_df.columns:
            items_df[col] = pd.to_numeric(items_df[col], errors="coerce")

    # Agency summary combining awarded wins and itemized bids
    agency_rows: dict[str, dict[str, Any]] = {}
    if not won_df.empty:
        for ag, grp in won_df.groupby("agency"):
            agency_rows[str(ag)] = {
                "agency": str(ag),
                "projects_won": int(len(grp)),
                "won_volume": float(grp["winning_bid_total"].fillna(0).sum()),
                "bids_submitted": 0,
                "bid_volume": 0.0,
            }
    if not bids_df.empty:
        for ag, grp in bids_df.groupby("agency"):
            entry = agency_rows.setdefault(
                str(ag),
                {
                    "agency": str(ag),
                    "projects_won": 0,
                    "won_volume": 0.0,
                    "bids_submitted": 0,
                    "bid_volume": 0.0,
                },
            )
            entry["bids_submitted"] = int(len(grp))
            entry["bid_volume"] = float(grp["bidder_total"].fillna(0).sum())

    agency_df = (
        pd.DataFrame(list(agency_rows.values())).sort_values(
            ["projects_won", "bids_submitted", "won_volume"], ascending=False
        )
        if agency_rows
        else pd.DataFrame()
    )

    total_bids = int(len(bids_df))
    itemized_wins = int((bids_df["is_winner"] == 1).sum()) if not bids_df.empty else 0
    win_rate = round(100.0 * itemized_wins / total_bids, 1) if total_bids > 0 else None
    ee_series = bids_df["pct_vs_ee"].dropna() if not bids_df.empty else pd.Series(dtype=float)

    summary = {
        "projects_won": int(len(won_df)),
        "total_won_volume": float(won_df["winning_bid_total"].fillna(0).sum()) if not won_df.empty else 0.0,
        "total_bids_submitted": total_bids,
        "total_bid_volume": float(bids_df["bidder_total"].fillna(0).sum()) if not bids_df.empty else 0.0,
        "win_rate_pct": win_rate,
        "avg_pct_vs_ee": round(float(ee_series.mean()), 2) if not ee_series.empty else None,
        "agencies_count": int(len(agency_rows)),
    }
    return {
        "contractor": contractor_info,
        "summary": summary,
        "won_projects_df": won_df,
        "all_bids_df": bids_df,
        "agency_breakdown_df": agency_df,
        "top_items_df": items_df,
    }


def get_project_details(
    project_id: int,
    db_path: Path | str | None = None,
) -> dict[str, Any]:
    """Return full details, saved PDFs/source documents, bidder rankings, and itemized lines for a project."""
    pid = int(project_id)
    with db_session(db_path) as conn:
        proj_sql = """
            SELECT
                p.project_id,
                p.agency,
                p.agency_project_num,
                p.title,
                p.route_or_location,
                p.county,
                p.district,
                p.letting_date,
                p.award_date,
                p.engineers_estimate,
                p.winning_bid_total,
                p.source_url,
                c.canonical_name AS awarded_contractor,
                c.az_roc_license AS contractor_roc_license,
                c.approx_employees AS contractor_employees
            FROM projects p
            LEFT JOIN contractors c ON p.awarded_contractor_id = c.contractor_id
            WHERE p.project_id = ?
        """
        proj_df = conn.query_df(proj_sql, [pid])
        if proj_df.empty:
            return {
                "project": None,
                "documents_df": pd.DataFrame(),
                "bidders_df": pd.DataFrame(),
                "lines_df": pd.DataFrame(),
            }
        project_info = proj_df.iloc[0].to_dict()

        docs_sql = """
            SELECT
                doc_id,
                doc_type,
                title,
                source_url,
                archive_url,
                local_path,
                bucket,
                asset_name,
                file_size,
                sha256
            FROM project_documents
            WHERE project_id = ?
            ORDER BY CASE WHEN archive_url IS NOT NULL THEN 0 ELSE 1 END, doc_id ASC
        """
        docs_df = conn.query_df(docs_sql, [pid])
        raw_src = project_info.get("source_url")
        if raw_src and (
            docs_df.empty
            or str(raw_src) not in set(docs_df["source_url"].dropna().astype(str))
        ):
            fallback_row = pd.DataFrame(
                [
                    {
                        "doc_id": None,
                        "doc_type": "source_link",
                        "title": "Primary Project Source",
                        "source_url": str(raw_src),
                        "archive_url": None,
                        "local_path": None,
                        "bucket": None,
                        "asset_name": None,
                        "file_size": None,
                        "sha256": None,
                    }
                ]
            )
            docs_df = (
                pd.concat([docs_df, fallback_row], ignore_index=True)
                if not docs_df.empty
                else fallback_row
            )

        bidders_sql = """
            WITH itemized_totals AS (
                SELECT
                    bl.contractor_id,
                    bl.is_engineers_estimate,
                    MAX(bl.bid_rank) AS bid_rank,
                    MAX(bl.is_winner) AS is_winner,
                    COUNT(*) AS line_count,
                    SUM(bl.extended_total) AS bid_total
                FROM bid_lines bl
                WHERE bl.project_id = ?
                GROUP BY bl.contractor_id, bl.is_engineers_estimate
            ),
            summary_totals AS (
                SELECT
                    pb.contractor_id,
                    pb.is_engineers_estimate,
                    pb.bid_rank,
                    pb.is_winner,
                    0 AS line_count,
                    pb.bid_total
                FROM project_bids pb
                WHERE pb.project_id = ?
                  AND NOT EXISTS (
                      SELECT 1 FROM bid_lines bl2
                      WHERE bl2.project_id = pb.project_id
                        AND bl2.is_engineers_estimate = pb.is_engineers_estimate
                        AND COALESCE(bl2.contractor_id, -1) = COALESCE(pb.contractor_id, -1)
                  )
            ),
            combined AS (
                SELECT * FROM itemized_totals
                UNION ALL
                SELECT * FROM summary_totals
            )
            SELECT
                CASE WHEN cb.is_engineers_estimate = 1 THEN 'ENGINEER''S ESTIMATE'
                     ELSE COALESCE(c.canonical_name, 'UNKNOWN BIDDER') END AS bidder_name,
                cb.is_engineers_estimate,
                cb.bid_total,
                CASE WHEN COALESCE(p.engineers_estimate, 0) > 0 AND cb.is_engineers_estimate = 0
                     THEN CAST(ROUND(CAST(((cb.bid_total - p.engineers_estimate) / p.engineers_estimate) * 100.0 AS NUMERIC), 2) AS DOUBLE PRECISION)
                     ELSE NULL END AS pct_vs_ee,
                cb.bid_rank,
                cb.is_winner,
                cb.line_count,
                c.approx_employees,
                c.az_roc_license
            FROM combined cb
            JOIN projects p ON p.project_id = ?
            LEFT JOIN contractors c ON cb.contractor_id = c.contractor_id
            ORDER BY cb.is_engineers_estimate DESC,
                     CASE WHEN cb.bid_rank IS NULL THEN 999 ELSE cb.bid_rank END ASC,
                     cb.bid_total ASC
        """
        bidders_df = conn.query_df(bidders_sql, [pid, pid, pid])

        lines_sql = """
            SELECT
                pi.spec_system,
                pi.item_code,
                pi.description,
                pi.unit,
                CASE WHEN bl.is_engineers_estimate = 1 THEN 'ENGINEER''S ESTIMATE'
                     ELSE COALESCE(c.canonical_name, 'UNKNOWN') END AS bidder_name,
                bl.is_engineers_estimate,
                bl.quantity,
                bl.unit_price,
                bl.extended_total,
                bl.bid_rank,
                bl.is_winner
            FROM bid_lines bl
            JOIN pay_items pi ON bl.item_id = pi.item_id
            LEFT JOIN contractors c ON bl.contractor_id = c.contractor_id
            WHERE bl.project_id = ?
            ORDER BY pi.item_code ASC, bl.is_engineers_estimate DESC,
                     CASE WHEN bl.bid_rank IS NULL THEN 999 ELSE bl.bid_rank END ASC
        """
        lines_df = conn.query_df(lines_sql, [pid])

    for col in ("bid_total", "pct_vs_ee", "bid_rank", "is_winner", "line_count", "approx_employees"):
        if col in bidders_df.columns:
            bidders_df[col] = pd.to_numeric(bidders_df[col], errors="coerce")
    for col in ("quantity", "unit_price", "extended_total", "bid_rank", "is_winner"):
        if col in lines_df.columns:
            lines_df[col] = pd.to_numeric(lines_df[col], errors="coerce")

    return {
        "project": project_info,
        "documents_df": docs_df,
        "bidders_df": bidders_df,
        "lines_df": lines_df,
    }


_DEDUP_ABBREV_MAP: dict[str, str] = {
    "CONST": "CONSTRUCTION",
    "CONSTR": "CONSTRUCTION",
    "CONSTRUCT": "CONSTRUCTION",
    "CONSTRUCTN": "CONSTRUCTION",
    "CONSTRUCTORS": "CONSTRUCTOR",
    "CONTRACTORS": "CONTRACTOR",
    "CONTR": "CONTRACTING",
    "CONTRAC": "CONTRACTING",
    "ENGR": "ENGINEERING",
    "ENG": "ENGINEERING",
    "ENGIN": "ENGINEERING",
    "MAINT": "MAINTENANCE",
    "DEV": "DEVELOPMENT",
    "DEVEL": "DEVELOPMENT",
    "ASSOC": "ASSOCIATES",
    "SYS": "SYSTEMS",
    "SVC": "SERVICES",
    "SVCS": "SERVICES",
    "SRVS": "SERVICES",
    "ELECT": "ELECTRICAL",
    "ELEC": "ELECTRICAL",
    "IND": "INDUSTRIES",
    "MATL": "MATERIALS",
    "MATLS": "MATERIALS",
    "PAV": "PAVING",
    "UTIL": "UTILITIES",
    "UTILS": "UTILITIES",
    "EQUIP": "EQUIPMENT",
    "MGMT": "MANAGEMENT",
    "BROS": "BROTHERS",
    "BUILDERS": "BUILDER",
}

_DEDUP_STOPWORDS = {"AND", "THE", "OF", "FOR", "A", "AN"}
_DEDUP_TRAIL_GEO = {
    "AZ",
    "ARIZONA",
    "PHOENIX",
    "TUCSON",
    "TEMPE",
    "MESA",
    "CHANDLER",
    "GILBERT",
    "GLENDALE",
    "SCOTTSDALE",
    "AVONDALE",
    "PEORIA",
    "PRESCOTT",
    "FLAGSTAFF",
    "YUMA",
    "USA",
}
_GENERIC_FIRST_TOKENS = {
    "ARIZONA",
    "AMERICAN",
    "WESTERN",
    "SOUTHWEST",
    "SUN",
    "VALLEY",
    "PHOENIX",
    "NATIONAL",
    "GENERAL",
    "UNITED",
    "STANDARD",
    "PACIFIC",
    "FIRST",
    "NEW",
    "PRECISION",
    "TRI",
    "ALL",
    "CENTRAL",
    "MOUNTAIN",
    "DESERT",
    "RED",
    "BLUE",
    "GOLD",
    "SILVER",
    "EAGLE",
    "STAR",
    "QUALITY",
    "CUSTOM",
    "PRO",
    "ELITE",
    "APEX",
    "PREMIER",
    "COPPER",
    "SONORAN",
    "CANYON",
    "SUMMIT",
    "SIERRA",
    "LEGACY",
    "STATE",
    "CITY",
    "TOWN",
    "COUNTY",
    "MARICOPA",
    "PIMA",
}
_INDUSTRY_SUFFIX_WORDS = {
    "CONSTRUCTION",
    "CONTRACTING",
    "CONTRACTOR",
    "ASPHALT",
    "PAVING",
    "ELECTRIC",
    "ELECTRICAL",
    "EXCAVATING",
    "EXCAVATION",
    "UTILITY",
    "UTILITIES",
    "CONCRETE",
    "ENGINEERING",
    "MAINTENANCE",
    "STRIPING",
    "GRADING",
    "DEVELOPMENT",
    "ENTERPRISES",
    "COMPANIES",
    "GROUP",
    "SERVICES",
    "SYSTEMS",
    "SUPPLY",
    "MATERIALS",
    "CIVIL",
}


def _contractor_signature_tokens(name: str) -> tuple[str, ...]:
    """Build tightened comparison tokens (collapsed initials, normalized abbreviations, no stopwords/geo)."""
    from src.contractor_profiles import canonicalize_contractor_name

    canon = canonicalize_contractor_name(name)
    raw_tokens = canon.split()
    # Collapse adjacent single-letter initials: ["C", "S", "CONSTRUCTION"] -> ["CS", "CONSTRUCTION"]
    merged: list[str] = []
    i = 0
    while i < len(raw_tokens):
        if len(raw_tokens[i]) == 1 and raw_tokens[i].isalpha():
            buf = raw_tokens[i]
            j = i + 1
            while j < len(raw_tokens) and len(raw_tokens[j]) == 1 and raw_tokens[j].isalpha():
                buf += raw_tokens[j]
                j += 1
            merged.append(buf)
            i = j
        else:
            merged.append(raw_tokens[i])
            i += 1

    out: list[str] = []
    for idx, tok in enumerate(merged):
        if tok in _DEDUP_STOPWORDS:
            continue
        if len(out) >= 1 and tok in _DEDUP_TRAIL_GEO and idx >= len(merged) - 2:
            continue
        out.append(_DEDUP_ABBREV_MAP.get(tok, tok))

    if len(out) >= 2 and len(out[-1]) >= 6 and out[-1].endswith("S") and not out[-1].endswith("SS"):
        singular = out[-1][:-1]
        if singular in _DEDUP_ABBREV_MAP.values() or singular in _INDUSTRY_SUFFIX_WORDS:
            out[-1] = singular

    return tuple(out)


def deduplicate_contractors(
    db_path: Path | str | None = None,
    *,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Merge duplicate contractor rows across projects, bid_lines, and project_bids safely."""
    from difflib import SequenceMatcher

    from src.contractor_profiles import _PROFILES, canonicalize_contractor_name, lookup_contractor_profile

    with db_session(db_path) as conn:
        rows = conn.execute(
            """
            SELECT contractor_id, canonical_name, az_roc_license, approx_employees
            FROM contractors
            ORDER BY contractor_id ASC
            """
        ).fetchall()
        if not rows:
            return {"groups_merged": 0, "duplicates_removed": 0, "merges": []}

        # Gather usage counts so we pick the highest-signal survivor row per cluster
        ref_counts: dict[int, int] = {}
        for sql in (
            "SELECT awarded_contractor_id, COUNT(*) FROM projects WHERE awarded_contractor_id IS NOT NULL GROUP BY awarded_contractor_id",
            "SELECT contractor_id, COUNT(*) FROM bid_lines WHERE contractor_id IS NOT NULL GROUP BY contractor_id",
            "SELECT contractor_id, COUNT(*) FROM project_bids WHERE contractor_id IS NOT NULL GROUP BY contractor_id",
        ):
            for cid_raw, cnt in conn.execute(sql).fetchall():
                cid = int(cid_raw)
                ref_counts[cid] = ref_counts.get(cid, 0) + int(cnt)

        records: dict[int, dict[str, Any]] = {}
        for cid_raw, cname_raw, roc, emp in rows:
            cid = int(cid_raw)
            cname = str(cname_raw or "").strip()
            upper = cname.upper()
            if any(
                upper.startswith(p)
                for p in ("ADOT LOW BIDDER", "ADOT BIDDER", "DEPARTMENT", "UNKNOWN", "ENGINEER")
            ):
                continue
            canon = canonicalize_contractor_name(cname) or cname
            sig = _contractor_signature_tokens(canon)
            is_jv = "JV" in canon.split() or ("JOINT" in canon.split() and "VENTURE" in canon.split())
            records[cid] = {
                "cid": cid,
                "orig_name": cname,
                "canon_name": canon,
                "sig": sig,
                "roc": (str(roc).strip().upper() if roc else None),
                "emp": emp,
                "is_jv": is_jv,
                "refs": ref_counts.get(cid, 0),
            }

        cids = list(records.keys())
        parent = {cid: cid for cid in cids}

        def _find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def _union(a: int, b: int) -> None:
            ra, rb = _find(a), _find(b)
            if ra != rb:
                parent[rb] = ra

        # Pass 1: exact canonicalize_contractor_name() match
        by_canon: dict[str, list[int]] = {}
        for cid, rec in records.items():
            by_canon.setdefault(rec["canon_name"], []).append(cid)
        for group_ids in by_canon.values():
            for other in group_ids[1:]:
                _union(group_ids[0], other)

        # Pass 2: identical tightened signature tokens (non-JV or identical JV)
        by_sig: dict[tuple[bool, tuple[str, ...]], list[int]] = {}
        for cid, rec in records.items():
            if rec["sig"]:
                by_sig.setdefault((rec["is_jv"], rec["sig"]), []).append(cid)
        for group_ids in by_sig.values():
            for other in group_ids[1:]:
                _union(group_ids[0], other)

        # Pass 3: shared AZ ROC license + identical primary token (non-JV)
        by_roc_first: dict[tuple[str, str], list[int]] = {}
        for cid, rec in records.items():
            if not rec["is_jv"] and rec["roc"] and rec["sig"]:
                first = rec["sig"][0]
                if len(first) >= 4 and first not in _GENERIC_FIRST_TOKENS:
                    by_roc_first.setdefault((rec["roc"], first), []).append(cid)
        for group_ids in by_roc_first.values():
            for other in group_ids[1:]:
                _union(group_ids[0], other)

        # Pass 4: unambiguous strict prefix collapse on non-JV signature tokens
        all_sigs_map: dict[tuple[str, ...], list[int]] = {}
        for cid, rec in records.items():
            if not rec["is_jv"] and rec["sig"]:
                all_sigs_map.setdefault(rec["sig"], []).append(cid)

        unique_sigs = sorted(all_sigs_map.keys(), key=len)
        for sig in unique_sigs:
            k = len(sig)
            if k == 1 and (len(sig[0]) < 6 or sig[0] in _GENERIC_FIRST_TOKENS):
                continue
            extensions = [
                ext
                for ext in unique_sigs
                if len(ext) > k and ext[:k] == sig
            ]
            if not extensions:
                continue
            # Check that extensions don't branch into conflicting second words when k == 1
            if k == 1:
                distinct_second_words = {ext[1] for ext in extensions}
                # Only collapse single-word bare stems if every extension uses standard industry suffixes
                # or all extensions agree on the exact same root 2nd word
                if len(distinct_second_words) > 1 and not distinct_second_words.issubset(_INDUSTRY_SUFFIX_WORDS):
                    continue
                if len(distinct_second_words) == 1 and next(iter(distinct_second_words)) not in _INDUSTRY_SUFFIX_WORDS:
                    continue
                target_ext = sorted(extensions, key=lambda e: (len(e), e))[0]
            else:
                # k >= 2: e.g. ("MARKHAM", "CONTRACTING") vs ("MARKHAM", "CONTRACTING", "CO", ...)
                target_ext = sig
            base_cid = all_sigs_map[sig][0]
            for ext in extensions:
                # Merge extensions onto the same cluster only if unambiguous (single distinct 2-token root when k==1)
                if k == 1 and len({e[:2] for e in extensions}) > 1 and ext[:2] != target_ext[:2]:
                    continue
                _union(base_cid, all_sigs_map[ext][0])

        # Pass 5: single-token OCR/typo near-match sharing exact industry tail (e.g. NESBLTT vs NESBITT CONTRACTING)
        by_tail: dict[str, list[int]] = {}
        for cid, rec in records.items():
            sig = rec["sig"]
            if not rec["is_jv"] and len(sig) == 2 and sig[1] in _INDUSTRY_SUFFIX_WORDS and len(sig[0]) >= 6:
                by_tail.setdefault(sig[1], []).append(cid)
        for tail_cids in by_tail.values():
            for idx_a in range(len(tail_cids)):
                ca = tail_cids[idx_a]
                w_a = records[ca]["sig"][0]
                for idx_b in range(idx_a + 1, len(tail_cids)):
                    cb = tail_cids[idx_b]
                    w_b = records[cb]["sig"][0]
                    if w_a[0] == w_b[0] and abs(len(w_a) - len(w_b)) <= 1:
                        if SequenceMatcher(None, w_a, w_b).ratio() >= 0.85:
                            _union(ca, cb)

        # Group connected components
        components: dict[int, list[int]] = {}
        for cid in cids:
            root = _find(cid)
            components.setdefault(root, []).append(cid)

        # Also check any single row whose canonical_name differs from canonicalize_contractor_name(orig_name)
        merges_summary: list[dict[str, Any]] = []
        removed_total = 0

        for comp_cids in components.values():
            # Choose canonical target name and best survivor ID
            def _name_score(cid: int) -> tuple[int, int, int, int]:
                rec = records[cid]
                in_profiles = 0 if rec["canon_name"] in _PROFILES else 1
                is_clean_canon = 0 if rec["orig_name"] == rec["canon_name"] else 1
                sig_len = len(rec["sig"])
                return (in_profiles, is_clean_canon, -rec["refs"], sig_len)

            comp_sorted = sorted(comp_cids, key=lambda c: (_name_score(c), c))
            best_cid = comp_sorted[0]
            # Pick best canonical target name (prefer curated _PROFILES key, else cleanest rec["canon_name"])
            target_name = records[best_cid]["canon_name"]
            for cid in comp_sorted:
                cn = records[cid]["canon_name"]
                if cn in _PROFILES:
                    target_name = cn
                    break

            # Pick actual DB survivor (if any other row already holds target_name, keep it!)
            survivor_cid = best_cid
            for cid in comp_sorted:
                if records[cid]["orig_name"] == target_name:
                    survivor_cid = cid
                    break

            dup_cids = [c for c in comp_cids if c != survivor_cid]
            needs_rename = records[survivor_cid]["orig_name"] != target_name
            if not dup_cids and not needs_rename:
                continue

            best_roc = next((records[c]["roc"] for c in comp_sorted if records[c]["roc"]), None)
            best_emp = next((records[c]["emp"] for c in comp_sorted if records[c]["emp"] is not None), None)
            prof_roc, prof_emp = lookup_contractor_profile(target_name)
            best_roc = best_roc or prof_roc
            best_emp = best_emp if best_emp is not None else prof_emp

            merges_summary.append(
                {
                    "survivor_id": survivor_cid,
                    "canonical_name": target_name,
                    "merged_names": [records[c]["orig_name"] for c in comp_cids],
                    "removed_count": len(dup_cids),
                }
            )
            removed_total += len(dup_cids)

            if dry_run:
                continue

            holder = conn.execute(
                "SELECT contractor_id FROM contractors WHERE canonical_name = ? AND contractor_id != ?",
                (target_name, survivor_cid),
            ).fetchone()
            if holder:
                real_survivor = int(holder[0])
                dup_cids.append(survivor_cid)
                survivor_cid = real_survivor

            if dup_cids:
                placeholders = ",".join("?" for _ in [survivor_cid, *dup_cids])
                # 1. Deduplicate project_bids on (project_id, is_engineers_estimate) across the group
                pb_rows = conn.execute(
                    f"""
                    SELECT project_bid_id, project_id, contractor_id, is_engineers_estimate,
                           bid_total, bid_rank, is_winner
                    FROM project_bids
                    WHERE contractor_id IN ({placeholders})
                    ORDER BY is_winner DESC,
                             CASE WHEN bid_rank IS NULL THEN 999 ELSE bid_rank END ASC,
                             project_bid_id ASC
                    """,
                    [survivor_cid, *dup_cids],
                ).fetchall()
                seen_pb_keys: set[tuple[int, int]] = set()
                keep_pb_ids: list[int] = []
                del_pb_ids: list[int] = []
                for pbid, pid_val, _, est_flag, _, _, _ in pb_rows:
                    key = (int(pid_val), int(est_flag))
                    if key in seen_pb_keys:
                        del_pb_ids.append(int(pbid))
                    else:
                        seen_pb_keys.add(key)
                        keep_pb_ids.append(int(pbid))
                if del_pb_ids:
                    del_ph = ",".join("?" for _ in del_pb_ids)
                    conn.execute(f"DELETE FROM project_bids WHERE project_bid_id IN ({del_ph})", del_pb_ids)
                if keep_pb_ids:
                    keep_ph = ",".join("?" for _ in keep_pb_ids)
                    conn.execute(
                        f"UPDATE project_bids SET contractor_id = ? WHERE project_bid_id IN ({keep_ph})",
                        [survivor_cid, *keep_pb_ids],
                    )

                dup_ph = ",".join("?" for _ in dup_cids)
                conn.execute(
                    f"UPDATE bid_lines SET contractor_id = ? WHERE contractor_id IN ({dup_ph})",
                    [survivor_cid, *dup_cids],
                )
                conn.execute(
                    f"UPDATE projects SET awarded_contractor_id = ? WHERE awarded_contractor_id IN ({dup_ph})",
                    [survivor_cid, *dup_cids],
                )
                conn.execute(
                    f"DELETE FROM contractors WHERE contractor_id IN ({dup_ph})",
                    dup_cids,
                )

            conn.execute(
                """
                UPDATE contractors
                SET canonical_name = ?,
                    az_roc_license = COALESCE(?, az_roc_license),
                    approx_employees = COALESCE(?, approx_employees)
                WHERE contractor_id = ?
                """,
                (
                    target_name,
                    best_roc,
                    int(best_emp) if best_emp is not None else None,
                    survivor_cid,
                ),
            )

    return {
        "groups_merged": len(merges_summary),
        "duplicates_removed": removed_total,
        "merges": merges_summary,
    }



