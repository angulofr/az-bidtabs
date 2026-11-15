"""AZ BidTabs — Arizona Heavy-Civil Bid Tabulation Intelligence (MVP)."""

from __future__ import annotations

import importlib
import os
import sys
from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

ROOT_DIR = Path(__file__).resolve().parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from src.db import (
    DEFAULT_DB_PATH,
    compute_unit_price_stats,
    get_contractor_scorecard,
    get_database_url,
    get_filter_options,
    get_head_to_head,
    get_item_code_details,
    get_overview_stats,
    init_db,
    is_postgres,
    query_pay_item_observations,
)
from src.matching import benchmark_schedule
from src.scrapers import adot as adot_scraper
from src.scrapers import phoenix_opengov as opengov_scraper
from src.seed import seed_database

importlib.reload(adot_scraper)
importlib.reload(opengov_scraper)

st.set_page_config(
    page_title="AZ BidTabs | Arizona Heavy-Civil Bid Intelligence",
    page_icon="🏗️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ---------------------------------------------------------------------------
# Custom CSS for clean industrial SaaS look
# ---------------------------------------------------------------------------
st.markdown(
    """
    <style>
    .block-container { padding-top: 1.5rem; padding-bottom: 2rem; }
    div[data-testid="stMetric"] {
        background-color: #FFFFFF;
        border: 1px solid #E2E8F0;
        border-radius: 10px;
        padding: 14px 16px;
        box-shadow: 0 1px 2px rgba(15, 23, 42, 0.04);
    }
    .az-badge {
        display: inline-block;
        padding: 2px 8px;
        border-radius: 999px;
        font-size: 0.75rem;
        font-weight: 600;
        background-color: #FEF3C7;
        color: #92400E;
        border: 1px solid #FDE68A;
    }
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# Optional Invite-Code Gate (APP_ACCESS_CODE via env var or st.secrets)
# ---------------------------------------------------------------------------
def _get_access_code() -> str | None:
    env_code = os.environ.get("APP_ACCESS_CODE", "").strip()
    if env_code:
        return env_code
    try:
        secret_code = str(st.secrets.get("APP_ACCESS_CODE", "")).strip()
        return secret_code or None
    except Exception:
        return None


def check_access_gate() -> bool:
    required_code = _get_access_code()
    if not required_code:
        return True
    if st.session_state.get("authenticated"):
        return True

    st.markdown("## 🏗️ AZ BidTabs")
    st.caption("Arizona Heavy-Civil Bid Tabulation & Unit-Price Intelligence")
    with st.form("invite_gate_form"):
        entered = st.text_input("Enter Invite / Access Code", type="password")
        submitted = st.form_submit_button("Unlock Workspace", type="primary")
        if submitted:
            if entered == required_code:
                st.session_state["authenticated"] = True
                st.rerun()
            else:
                st.error("Invalid access code. Please contact your AZ BidTabs administrator.")
    return False


if not check_access_gate():
    st.stop()


def _get_config_flag(name: str) -> str:
    val = os.environ.get(name, "").strip().lower()
    if val:
        return val
    try:
        return str(st.secrets.get(name, "")).strip().lower()
    except Exception:
        return ""


def is_production_mode() -> bool:
    prod_flag = _get_config_flag("PRODUCTION_MODE")
    if prod_flag in ("1", "true", "yes"):
        return True
    if prod_flag in ("0", "false", "no"):
        return False
    seed_demo = _get_config_flag("SEED_DEMO_DATA")
    if seed_demo in ("1", "true", "yes"):
        return False
    return get_database_url() is not None


# Resolve database target (Postgres DATABASE_URL if configured, else SQLite)
DB_PATH: Path | str = get_database_url() or DEFAULT_DB_PATH
PROD_MODE = is_production_mode()
USING_POSTGRES = is_postgres(DB_PATH)

if PROD_MODE:
    init_db(DB_PATH)
else:
    seed_database(DB_PATH)


# ---------------------------------------------------------------------------
# Clickable Item Code Links & New-Tab Detail Helpers
# ---------------------------------------------------------------------------
_ITEM_LINK_DISPLAY_RE = r"[?&]item=([^&]+)"


def _get_base_app_url() -> str:
    try:
        raw_url = getattr(st.context, "url", None)
        if raw_url:
            parts = urlsplit(str(raw_url))
            if parts.scheme and parts.netloc:
                return f"{parts.scheme}://{parts.netloc}{parts.path}".rstrip("/")
        headers = getattr(st.context, "headers", None) or {}
        host = headers.get("Host") or headers.get("host")
        if host:
            proto = headers.get("X-Forwarded-Proto") or headers.get("x-forwarded-proto") or "https"
            return f"{proto}://{host}".rstrip("/")
    except Exception:
        pass
    return ""


def _build_item_detail_url(item_code: str, spec_system: str = "") -> str | None:
    code = str(item_code or "").strip()
    if not code:
        return None
    base = _get_base_app_url()
    q_item = quote(code, safe="-._~")
    spec = str(spec_system or "").strip().upper()
    q_spec = f"&spec={quote(spec, safe='')}" if spec in ("ADOT", "MAG") else ""
    prefix = base if base else ""
    return f"{prefix}/?item={q_item}{q_spec}"


def _with_clickable_item_links(
    df: pd.DataFrame,
    code_col: str = "item_code",
    spec_col: str | None = "spec_system",
) -> pd.DataFrame:
    if df.empty or code_col not in df.columns:
        return df
    out = df.copy()
    has_spec = bool(spec_col and spec_col in out.columns)
    out[code_col] = [
        _build_item_detail_url(c, s if has_spec else "")
        for c, s in zip(
            out[code_col],
            out[spec_col] if has_spec else [""] * len(out),
        )
    ]
    return out


def _clear_item_query_param() -> None:
    try:
        st.query_params.clear()
    except Exception:
        pass


def _render_item_detail_page(item_code: str, initial_spec: str = "ALL") -> None:
    top_l, top_r = st.columns([4, 1.3])
    with top_r:
        if st.button("← Back to Full Workspace", use_container_width=True):
            _clear_item_query_param()
            st.rerun()

    opts = get_filter_options(DB_PATH)
    spec_choices = ["ALL", "ADOT", "MAG"]
    default_spec_idx = spec_choices.index(initial_spec) if initial_spec in spec_choices else 0

    f1, f2, f3, f4 = st.columns([1.4, 0.9, 1.3, 1.3])
    code_input = f1.text_input("Pay Item Code", value=item_code)
    spec_sel = f2.selectbox("Spec System", spec_choices, index=default_spec_idx)
    ag_sel = f3.multiselect("Filter by Agency", opts["agencies"])
    co_sel = f4.multiselect("Filter by County", opts["counties"])

    t1, t2, _ = st.columns([1.2, 1.8, 2.0])
    winners_only_item = t1.toggle("Winning Bids Only", value=False)
    include_ee_rows = t2.toggle("Include Department / Engineer's Estimate in Lines", value=True)

    active_code = code_input.strip() or item_code
    contractor_summary, all_lines = get_item_code_details(
        active_code,
        DB_PATH,
        spec_system=spec_sel,
        agencies=ag_sel or None,
        counties=co_sel or None,
        winners_only=winners_only_item,
    )

    if all_lines.empty:
        with top_l:
            st.title(f"Pay Item Detail: {active_code}")
        st.warning(f"No bid line observations found for item code `{active_code}` with the current filters.")
        return

    descs = [str(d).strip() for d in all_lines["description"].dropna().unique() if str(d).strip()]
    units = [str(u).strip() for u in all_lines["unit"].dropna().unique() if str(u).strip()]
    specs = [str(s).strip() for s in all_lines["spec_system"].dropna().unique() if str(s).strip()]

    with top_l:
        st.title(f"{' / '.join(specs)} | {active_code} — {descs[0] if descs else ''}")
        st.caption(
            f"Unit(s): **{', '.join(units) or '—'}** • Cross-contractor pricing & bid history across all archived projects"
        )

    contractor_lines = all_lines[all_lines["is_engineers_estimate"] == 0]
    ee_lines = all_lines[all_lines["is_engineers_estimate"] == 1]
    stats_c = compute_unit_price_stats(contractor_lines)
    ee_median = float(ee_lines["unit_price"].median()) if not ee_lines.empty else None

    k1, k2, k3, k4, k5, k6 = st.columns(6)
    k1.metric("Contractors", f"{len(contractor_summary):,}")
    k2.metric("Projects", f"{int(all_lines['project_id'].nunique()):,}")
    k3.metric("Contractor Bids", f"{stats_c['observations']:,}")
    k4.metric("Median Price", f"${stats_c['median']:,.2f}" if stats_c["observations"] else "—")
    k5.metric("Qty-Wtd Avg", f"${stats_c['qty_weighted_avg']:,.2f}" if stats_c["observations"] else "—")
    k6.metric("Dept. Est. Median", f"${ee_median:,.2f}" if ee_median is not None else "—")

    st.subheader("All Contractors — Unit Price Breakdown")
    if not contractor_summary.empty:
        st.dataframe(
            contractor_summary,
            use_container_width=True,
            hide_index=True,
            column_config={
                "contractor_name": "Contractor",
                "approx_employees": st.column_config.NumberColumn("Approx. Employees", format="%,d"),
                "observations": st.column_config.NumberColumn("Bid Lines", format="%d"),
                "projects": st.column_config.NumberColumn("Projects", format="%d"),
                "wins": st.column_config.NumberColumn("Wins", format="%d"),
                "min_unit_price": st.column_config.NumberColumn("Min Unit Price", format="$%,.2f"),
                "median_unit_price": st.column_config.NumberColumn("Median Unit Price", format="$%,.2f"),
                "qty_weighted_avg": st.column_config.NumberColumn("Qty-Wtd Avg", format="$%,.2f"),
                "max_unit_price": st.column_config.NumberColumn("Max Unit Price", format="$%,.2f"),
                "total_quantity": st.column_config.NumberColumn("Total Qty Bid", format="%,.0f"),
                "total_extended": st.column_config.NumberColumn("Total Extended", format="$%,.2f"),
            },
        )
    else:
        st.info("Only Department / Engineer's Estimate rows exist for this item under the current filters.")

    if not contractor_lines.empty:
        st.subheader("Unit Price vs. Quantity Across Contractors")
        plot_df = (all_lines if include_ee_rows else contractor_lines).copy()
        plot_df["bid_role"] = plot_df.apply(
            lambda r: "Department Estimate"
            if r["is_engineers_estimate"] == 1
            else ("Winning Bid" if r["is_winner"] == 1 else "Competing Bid"),
            axis=1,
        )
        fig = px.scatter(
            plot_df,
            x="quantity",
            y="unit_price",
            color="bidder_name",
            symbol="bid_role",
            hover_data={
                "agency": True,
                "agency_project_num": True,
                "project_title": True,
                "letting_date": True,
                "unit": True,
                "bid_rank": True,
                "quantity": ":,.0f",
                "unit_price": ":$,.2f",
            },
            labels={
                "quantity": f"Quantity ({units[0] if units else 'Unit'})",
                "unit_price": "Unit Price ($)",
                "bidder_name": "Contractor / Estimate",
                "bid_role": "Role",
            },
        )
        if stats_c["observations"] > 0:
            fig.add_hline(
                y=stats_c["median"],
                line_dash="dash",
                line_color="#0F172A",
                annotation_text=f"Contractor Median: ${stats_c['median']:,.2f}",
            )
        fig.update_layout(height=420, margin=dict(l=10, r=10, t=20, b=10))
        st.plotly_chart(fig, use_container_width=True)

    tbl_df = (all_lines if include_ee_rows else contractor_lines).copy()
    tbl_df["is_winner"] = tbl_df["is_winner"].fillna(0).astype(bool)
    col_t, col_d = st.columns([4, 1])
    col_t.subheader("Every Bid Line for This Item Code")
    col_d.download_button(
        label="Export Item CSV",
        data=tbl_df.to_csv(index=False).encode("utf-8"),
        file_name=f"az_bidtabs_item_{active_code}.csv",
        mime="text/csv",
        use_container_width=True,
    )
    detail_cols = [
        "letting_date",
        "agency",
        "agency_project_num",
        "project_title",
        "county",
        "spec_system",
        "description",
        "unit",
        "bidder_name",
        "bidder_employees",
        "quantity",
        "unit_price",
        "extended_total",
        "bid_rank",
        "is_winner",
    ]
    st.dataframe(
        tbl_df[detail_cols],
        use_container_width=True,
        hide_index=True,
        column_config={
            "letting_date": "Letting Date",
            "agency": "Agency",
            "agency_project_num": "Project #",
            "project_title": "Project Title",
            "county": "County",
            "spec_system": "Spec",
            "description": "Description",
            "unit": "Unit",
            "bidder_name": "Contractor / Estimate",
            "bidder_employees": st.column_config.NumberColumn("Approx. Employees", format="%,d"),
            "quantity": st.column_config.NumberColumn("Quantity", format="%,.0f"),
            "unit_price": st.column_config.NumberColumn("Unit Price", format="$%,.2f"),
            "extended_total": st.column_config.NumberColumn("Extended Total", format="$%,.2f"),
            "bid_rank": st.column_config.NumberColumn("Rank", format="%d"),
            "is_winner": st.column_config.CheckboxColumn("Winner"),
        },
    )


qp_item = unquote(str(st.query_params.get("item", "") or "").strip())
qp_spec = str(st.query_params.get("spec", "ALL") or "ALL").strip().upper()


# ---------------------------------------------------------------------------
# Sidebar Navigation
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("## 🏗️ AZ BidTabs")
    mode_label = "PRODUCTION" if PROD_MODE else "DEMO"
    db_label = "POSTGRES" if USING_POSTGRES else "SQLITE"
    st.markdown(
        f'<span class="az-badge">{mode_label} • {db_label}</span>',
        unsafe_allow_html=True,
    )
    st.caption("Arizona Heavy-Civil Bid Intelligence")
    st.divider()

    page = st.radio(
        "Workspace",
        [
            "Market Overview",
            "Pay Item Explorer",
            "Competitor Intel & Head-to-Head",
            "Schedule Benchmarker",
            "Ingestion & Scrapers",
        ],
        label_visibility="collapsed",
        on_change=_clear_item_query_param,
    )

    st.divider()
    stats = get_overview_stats(DB_PATH)
    st.metric("Archived Projects", f"{stats['projects']:,}")
    st.metric("Bid Line Observations", f"{stats['bid_lines']:,}")
    st.caption(
        "ADOT 4-Month Expiry Moat: Automated weekly archiving preserves historical "
        "ADOT & MAG unit prices."
    )


# ---------------------------------------------------------------------------
# Dedicated New-Tab Pay Item Detail View (when ?item=<code> is in URL)
# ---------------------------------------------------------------------------
if qp_item:
    _render_item_detail_page(qp_item, qp_spec)

# ---------------------------------------------------------------------------
# Page 1: Market Overview
# ---------------------------------------------------------------------------
elif page == "Market Overview":
    st.title("Arizona Heavy-Civil Market Overview")
    st.caption(
        "Unified bid tabulation archive across **ADOT** and **Municipal / MAG** agencies "
        "(Phoenix, Mesa, Scottsdale, Chandler, Goodyear, MCDOT, Pima County)."
    )

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Projects Archived", f"{stats['projects']:,}")
    c2.metric("Contractors Tracked", f"{stats['contractors']:,}")
    c3.metric("Unique Pay Items", f"{stats['pay_items']:,}")
    c4.metric("Awarded Volume", f"${stats['total_awarded_volume'] / 1e6:,.1f}M")

    if stats["projects"] == 0:
        st.info(
            "Database is empty (Production Mode). Go to **Ingestion & Scrapers → Batch Upload (Multi-File)** "
            "to import ADOT Bid Tab PDFs or City of Phoenix / MCDOT CSV/XLSX exports."
        )

    col_left, col_right = st.columns([1.2, 1.8])
    with col_left:
        st.subheader("Coverage by Agency")
        ag_df = stats["agencies_df"]
        if not ag_df.empty:
            fig = px.bar(
                ag_df,
                x="projects",
                y="agency",
                orientation="h",
                color="awarded_volume",
                color_continuous_scale="YlOrBr",
                labels={"projects": "Projects", "agency": "Agency", "awarded_volume": "Awarded ($)"},
            )
            fig.update_layout(height=340, margin=dict(l=10, r=10, t=10, b=10), yaxis={"categoryorder": "total ascending"})
            st.plotly_chart(fig, use_container_width=True)

    with col_right:
        st.subheader("Recent Bid Lettings")
        rec_df = stats["recent_projects_df"]
        if not rec_df.empty:
            st.dataframe(
                rec_df,
                use_container_width=True,
                hide_index=True,
                height=340,
                column_config={
                    "engineers_estimate": st.column_config.NumberColumn(
                        "Engineer's Estimate", format="$%,.0f"
                    ),
                    "winning_bid_total": st.column_config.NumberColumn(
                        "Winning Bid Total", format="$%,.0f"
                    ),
                    "contractor_employees": st.column_config.NumberColumn(
                        "Approx. Employees", format="%,d"
                    ),
                },
            )


# ---------------------------------------------------------------------------
# Page 2: Pay Item Explorer
# ---------------------------------------------------------------------------
elif page == "Pay Item Explorer":
    st.title("Pay Item Explorer")
    st.caption("Search ADOT & MAG pay items, filter by quantity & agency, and analyze unit-price distributions.")

    opts = get_filter_options(DB_PATH)

    f1, f2, f3, f4 = st.columns([2.2, 1.0, 1.3, 1.3])
    search_q = f1.text_input(
        "Search Item Code or Description",
        placeholder="e.g. 2030301, 340-1, ASPHALT, EXCAVATION, PIPE",
    )
    spec_filter = f2.selectbox("Spec System", ["ALL", "ADOT", "MAG"])
    agency_filter = f3.multiselect("Agency", opts["agencies"])
    county_filter = f4.multiselect("County", opts["counties"])

    t1, t2, _ = st.columns([1.2, 1.5, 2.3])
    winners_only = t1.toggle("Winners Only", value=False)
    exclude_ee = t2.toggle("Exclude Engineer's Estimate", value=True)

    df_obs = query_pay_item_observations(
        DB_PATH,
        search_query=search_q,
        spec_system=spec_filter,
        agencies=agency_filter or None,
        counties=county_filter or None,
        winners_only=winners_only,
        exclude_engineers_estimate=exclude_ee,
    )

    if df_obs.empty:
        st.info("No bid line observations match your current filters.")
        st.stop()

    # Optional item selector when multiple items match
    unique_items = (
        df_obs[["spec_system", "item_code", "description", "unit"]]
        .drop_duplicates()
        .assign(label=lambda d: d["spec_system"] + " | " + d["item_code"] + " — " + d["description"] + " (" + d["unit"] + ")")
    )
    if len(unique_items) > 1:
        item_choices = ["ALL MATCHING ITEMS"] + unique_items["label"].tolist()
        selected_item_label = st.selectbox("Focus on Specific Pay Item", item_choices)
        if selected_item_label != "ALL MATCHING ITEMS":
            sel_code = selected_item_label.split("|")[1].split("—")[0].strip()
            df_obs = df_obs[df_obs["item_code"] == sel_code]

    # Quantity Range Slider
    q_min = float(df_obs["quantity"].min())
    q_max = float(df_obs["quantity"].max())
    if q_max > q_min:
        qty_range = st.slider(
            "Quantity Range Filter",
            min_value=q_min,
            max_value=q_max,
            value=(q_min, q_max),
        )
        df_filtered = df_obs[
            (df_obs["quantity"] >= qty_range[0]) & (df_obs["quantity"] <= qty_range[1])
        ].copy()
    else:
        df_filtered = df_obs.copy()

    # Summary Stats
    summary = compute_unit_price_stats(df_filtered)
    m1, m2, m3, m4, m5 = st.columns(5)
    m1.metric("Observations", f"{summary['observations']:,}")
    m2.metric("P25 Unit Price", f"${summary['p25']:,.2f}")
    m3.metric("Median (P50)", f"${summary['median']:,.2f}")
    m4.metric("P75 Unit Price", f"${summary['p75']:,.2f}")
    m5.metric("Qty-Weighted Avg", f"${summary['qty_weighted_avg']:,.2f}")

    # Unit Price vs. Quantity Scatter Chart
    st.subheader("Unit Price vs. Quantity")
    plot_df = df_filtered.copy()
    plot_df["bidder_role"] = plot_df.apply(
        lambda r: "Engineer's Estimate"
        if r["is_engineers_estimate"] == 1
        else ("Winning Bid" if r["is_winner"] == 1 else "Competing Bid"),
        axis=1,
    )
    fig_scatter = px.scatter(
        plot_df,
        x="quantity",
        y="unit_price",
        color="bidder_role",
        color_discrete_map={
            "Winning Bid": "#059669",
            "Competing Bid": "#64748B",
            "Engineer's Estimate": "#D97706",
        },
        hover_data={
            "item_code": True,
            "unit": True,
            "agency": True,
            "agency_project_num": True,
            "bidder_name": True,
            "letting_date": True,
            "quantity": ":,.0f",
            "unit_price": ":$,.2f",
        },
        labels={"quantity": "Quantity", "unit_price": "Unit Price ($)", "bidder_role": "Role"},
    )
    if summary["observations"] > 0:
        fig_scatter.add_hline(
            y=summary["median"],
            line_dash="dash",
            line_color="#0F172A",
            annotation_text=f"Median: ${summary['median']:,.2f}",
        )
    fig_scatter.update_layout(height=400, margin=dict(l=10, r=10, t=20, b=10))
    st.plotly_chart(fig_scatter, use_container_width=True)

    # Table + CSV Export
    col_tbl, col_exp = st.columns([4, 1])
    col_tbl.subheader("Bid Line Observations")
    csv_bytes = df_filtered.to_csv(index=False).encode("utf-8")
    col_exp.download_button(
        label="Export CSV",
        data=csv_bytes,
        file_name="az_bidtabs_pay_items.csv",
        mime="text/csv",
        use_container_width=True,
    )

    show_cols = [
        "letting_date",
        "agency",
        "agency_project_num",
        "county",
        "spec_system",
        "item_code",
        "description",
        "unit",
        "bidder_name",
        "bidder_employees",
        "quantity",
        "unit_price",
        "extended_total",
        "bid_rank",
        "is_winner",
    ]
    st.caption("Tip: Click any **Item Code** below to open all-contractor pricing details in a new tab.")
    st.dataframe(
        _with_clickable_item_links(df_filtered[show_cols], "item_code", "spec_system"),
        use_container_width=True,
        hide_index=True,
        column_config={
            "item_code": st.column_config.LinkColumn(
                "Item Code",
                help="Open all-contractor details for this item code in a new tab",
                display_text=_ITEM_LINK_DISPLAY_RE,
            ),
            "bidder_name": "Bidder",
            "bidder_employees": st.column_config.NumberColumn("Approx. Employees", format="%,d"),
            "quantity": st.column_config.NumberColumn("Quantity", format="%,.0f"),
            "unit_price": st.column_config.NumberColumn("Unit Price", format="$%,.2f"),
            "extended_total": st.column_config.NumberColumn("Extended Total", format="$%,.2f"),
            "bid_rank": st.column_config.NumberColumn("Rank", format="%d"),
        },
    )


# ---------------------------------------------------------------------------
# Page 3: Competitor Intel & Head-to-Head
# ---------------------------------------------------------------------------
elif page == "Competitor Intel & Head-to-Head":
    st.title("Competitor Intel & Head-to-Head")
    st.caption("Track contractor win rates, aggressiveness vs. Engineer's Estimate, and line-item head-to-head pricing.")

    scorecard = get_contractor_scorecard(DB_PATH)
    st.subheader("Contractor Scorecard")

    if not scorecard.empty:
        st.dataframe(
            scorecard.drop(columns=["contractor_id"]),
            use_container_width=True,
            hide_index=True,
            column_config={
                "contractor_name": "Contractor",
                "approx_employees": st.column_config.NumberColumn("Approx. Employees", format="%,d"),
                "az_roc_license": "AZ ROC License",
                "total_bids": st.column_config.NumberColumn("Total Bids", format="%d"),
                "wins": st.column_config.NumberColumn("Wins", format="%d"),
                "win_rate_pct": st.column_config.NumberColumn("Win Rate", format="%.1f%%"),
                "avg_pct_vs_ee": st.column_config.NumberColumn("Avg % vs. EE", format="%.2f%%"),
                "total_bid_volume": st.column_config.NumberColumn("Total Bid Volume", format="$%,.0f"),
                "won_volume": st.column_config.NumberColumn("Won Volume", format="$%,.0f"),
            },
        )
    else:
        st.info("No contractor bids recorded yet. Import bid tabulations in **Ingestion & Scrapers**.")

    st.divider()
    st.subheader("Head-to-Head Line-Item Comparison")
    contractor_names = scorecard["contractor_name"].tolist() if not scorecard.empty else []
    emp_lookup = (
        dict(zip(scorecard["contractor_name"], scorecard["approx_employees"]))
        if not scorecard.empty
        else {}
    )
    if len(contractor_names) >= 2:
        ca, cb = st.columns(2)
        contractor_a = ca.selectbox(
            "Contractor A (Your Firm / Target)",
            contractor_names,
            index=0,
            format_func=lambda n: f"{n} (~{int(emp_lookup[n]):,} emp)" if pd.notna(emp_lookup.get(n)) else n,
        )
        default_b_idx = 1 if len(contractor_names) > 1 else 0
        contractor_b = cb.selectbox(
            "Contractor B (Competitor)",
            contractor_names,
            index=default_b_idx,
            format_func=lambda n: f"{n} (~{int(emp_lookup[n]):,} emp)" if pd.notna(emp_lookup.get(n)) else n,
        )

        if contractor_a == contractor_b:
            st.warning("Select two different contractors for Head-to-Head comparison.")
        else:
            proj_h2h, line_h2h = get_head_to_head(contractor_a, contractor_b, DB_PATH)
            if proj_h2h.empty:
                st.info(f"No overlapping projects found between {contractor_a} and {contractor_b}.")
            else:
                a_wins = int((proj_h2h["a_total"] < proj_h2h["b_total"]).sum())
                b_wins = int((proj_h2h["b_total"] < proj_h2h["a_total"]).sum())
                avg_spread = float(proj_h2h["a_vs_b_pct"].mean())

                k1, k2, k3, k4 = st.columns(4)
                k1.metric("Overlapping Projects", len(proj_h2h))
                k2.metric(f"{contractor_a[:18]} Lower", a_wins)
                k3.metric(f"{contractor_b[:18]} Lower", b_wins)
                k4.metric("Avg A vs. B Spread", f"{avg_spread:+.2f}%")

                st.markdown("**Overlapping Projects**")
                st.dataframe(
                    proj_h2h.drop(columns=["project_id"]),
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "engineers_estimate": st.column_config.NumberColumn("Engineer's Est.", format="$%,.0f"),
                        "a_total": st.column_config.NumberColumn("A Total", format="$%,.0f"),
                        "a_rank": st.column_config.NumberColumn("A Rank", format="%d"),
                        "b_total": st.column_config.NumberColumn("B Total", format="$%,.0f"),
                        "b_rank": st.column_config.NumberColumn("B Rank", format="%d"),
                        "a_vs_b_pct": st.column_config.NumberColumn("A vs. B (%)", format="%.2f%%"),
                    },
                )

                st.markdown("**Line-Item Unit Price Comparison (Sorted by $ Impact)**")
                st.caption("Tip: Click any **Item Code** below to open a new tab with pricing from **all contractors**.")
                st.dataframe(
                    _with_clickable_item_links(line_h2h, "item_code", "spec_system"),
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "item_code": st.column_config.LinkColumn(
                            "Item Code",
                            help="Open all-contractor details for this item code in a new tab",
                            display_text=_ITEM_LINK_DISPLAY_RE,
                        ),
                        "quantity": st.column_config.NumberColumn("Quantity", format="%,.0f"),
                        "a_unit_price": st.column_config.NumberColumn("A Unit Price", format="$%,.2f"),
                        "b_unit_price": st.column_config.NumberColumn("B Unit Price", format="$%,.2f"),
                        "a_vs_b_pct": st.column_config.NumberColumn("A vs. B (%)", format="%.2f%%"),
                        "extended_diff": st.column_config.NumberColumn("Extended Diff ($)", format="$%,.2f"),
                    },
                )


# ---------------------------------------------------------------------------
# Page 4: Schedule Benchmarker
# ---------------------------------------------------------------------------
elif page == "Schedule Benchmarker":
    st.title("Schedule Benchmarker")
    st.caption(
        "Upload your bid schedule CSV (`item_code, quantity, your_unit_price`, optional `description, unit, spec_system`) "
        "to benchmark against historical medians and flag items **>20% off market**."
    )

    sample_csv = (
        "item_code,description,unit,spec_system,quantity,your_unit_price\n"
        "2030301,ROADWAY EXCAVATION,CU.YD.,ADOT,15000,16.50\n"
        "4160004,ASPHALTIC CONCRETE 3/4 INCH MIX,TON,ADOT,12000,152.00\n"
        "6010002,STRUCTURAL CONCRETE CLASS S,CU.YD.,ADOT,450,650.00\n"
        "340-1,CONCRETE CURB AND GUTTER,L.FT.,MAG,4200,35.50\n"
        "MISC-99,WATERLINE 12 INCH DIP CLASS 350,L.FT.,MAG,2800,170.00\n"
    )

    b1, b2, b3, b4 = st.columns([2.2, 1.1, 1.1, 1.1])
    uploaded = b1.file_uploader("Upload Bid Schedule CSV", type=["csv"])
    thresh = b2.number_input("Flag Threshold (%)", min_value=5.0, max_value=50.0, value=20.0, step=5.0)
    winners_only_bm = b3.toggle("Benchmark vs. Winners Only", value=False)
    b4.download_button(
        "Download Sample CSV",
        data=sample_csv.encode("utf-8"),
        file_name="sample_bid_schedule.csv",
        mime="text/csv",
        use_container_width=True,
    )

    use_sample = st.button("Load Sample Bid Schedule", type="secondary")

    sched_df = None
    if uploaded is not None:
        sched_df = pd.read_csv(uploaded)
    elif use_sample:
        import io
        sched_df = pd.read_csv(io.StringIO(sample_csv))

    if sched_df is not None:
        try:
            bm_df = benchmark_schedule(
                sched_df,
                DB_PATH,
                winners_only=winners_only_bm,
                threshold_pct=float(thresh),
            )
        except ValueError as exc:
            st.error(str(exc))
            st.stop()

        matched = bm_df[bm_df["flag"] != "UNMATCHED"]
        flagged = bm_df[bm_df["flag"].str.startswith(("HIGH", "LOW"))]
        your_total = float(bm_df["your_extended"].sum())
        net_var = float(matched["extended_vs_median"].sum()) if not matched.empty else 0.0

        s1, s2, s3, s4 = st.columns(4)
        s1.metric("Schedule Items", len(bm_df))
        s2.metric("Matched Items", f"{len(matched)} / {len(bm_df)}")
        s3.metric(f"Flagged (>{thresh:.0f}% off)", len(flagged))
        s4.metric("Total Variance vs. Median", f"${net_var:+,.0f}", delta=f"Your Bid: ${your_total:,.0f}")

        st.caption("Tip: Click any **Matched Code** below to open all-contractor pricing details in a new tab.")
        st.dataframe(
            _with_clickable_item_links(bm_df, "matched_code", "matched_spec"),
            use_container_width=True,
            hide_index=True,
            column_config={
                "matched_code": st.column_config.LinkColumn(
                    "Matched Code",
                    help="Open all-contractor details for this matched item code in a new tab",
                    display_text=_ITEM_LINK_DISPLAY_RE,
                ),
                "quantity": st.column_config.NumberColumn("Quantity", format="%,.0f"),
                "your_unit_price": st.column_config.NumberColumn("Your Unit Price", format="$%,.2f"),
                "your_extended": st.column_config.NumberColumn("Your Extended", format="$%,.2f"),
                "match_score": st.column_config.NumberColumn("Match Score", format="%.1f"),
                "observations": st.column_config.NumberColumn("Obs.", format="%d"),
                "hist_p25": st.column_config.NumberColumn("Hist. P25", format="$%,.2f"),
                "hist_median": st.column_config.NumberColumn("Hist. Median", format="$%,.2f"),
                "hist_p75": st.column_config.NumberColumn("Hist. P75", format="$%,.2f"),
                "hist_qty_wtd": st.column_config.NumberColumn("Qty-Wtd Avg", format="$%,.2f"),
                "pct_vs_median": st.column_config.NumberColumn("% vs. Median", format="%.1f%%"),
                "extended_vs_median": st.column_config.NumberColumn("Ext. Variance ($)", format="$%,.2f"),
            },
        )

        st.download_button(
            "Export Benchmarked Schedule CSV",
            data=bm_df.to_csv(index=False).encode("utf-8"),
            file_name="az_bidtabs_benchmarked_schedule.csv",
            mime="text/csv",
        )


# ---------------------------------------------------------------------------
# Page 5: Ingestion & Scrapers
# ---------------------------------------------------------------------------
elif page == "Ingestion & Scrapers":
    st.title("Data Ingestion & Scrapers")
    st.caption("Batch-ingest ADOT Bid Tabulation PDFs, Municipal/OpenGov CSV/XLSX exports, or run live portal checks.")

    tab_batch, tab_pdf, tab_og, tab_live = st.tabs(
        [
            "Batch Upload (Multi-File)",
            "Single ADOT PDF",
            "Single OpenGov Export",
            "Live Scraper Checks & DB Admin",
        ]
    )

    with tab_batch:
        st.markdown(
            "Drop **dozens of ADOT Bid Tab PDFs and/or OpenGov/MCDOT CSV/XLSX files** at once "
            "to parse and persist them into your database."
        )
        b_col1, b_col2 = st.columns(2)
        batch_agency = b_col1.selectbox(
            "Default Agency (for CSV/XLSX files)",
            [
                "City of Phoenix",
                "MCDOT",
                "City of Mesa",
                "City of Scottsdale",
                "City of Chandler",
                "City of Goodyear",
                "Pima County",
            ],
        )
        batch_county = b_col2.selectbox(
            "Default County (for CSV/XLSX files)",
            ["Maricopa", "Pima", "Pinal", "Yavapai", "Coconino"],
        )
        batch_files = st.file_uploader(
            "Upload Multiple Bid Tab Files (PDF, CSV, XLSX, XLS)",
            type=["pdf", "csv", "xlsx", "xls"],
            accept_multiple_files=True,
        )
        if batch_files:
            parsed_batch = []
            preview_rows = []
            for f in batch_files:
                raw_bytes = f.read()
                ext = Path(f.name).suffix.lower()
                try:
                    if ext == ".csv" and adot_scraper.is_e2c2_csv(raw_bytes):
                        parsed_batch.append((f.name, ("E2C2", raw_bytes), None))
                        preview_rows.append(
                            {
                                "Filename": f.name,
                                "Agency": "ADOT (E2C2 Bulk)",
                                "Project #": "MULTI-PROJECT",
                                "Spec": "ADOT",
                                "Parsed Lines": "Auto (All Rows)",
                                "Status": "READY (E2C2)",
                            }
                        )
                    elif ext == ".pdf":
                        parsed_item = adot_scraper.parse_adot_bid_tab_pdf(raw_bytes, filename=f.name)
                        if parsed_item.agency_project_num == "ADOT-UNKNOWN":
                            parsed_item.agency_project_num = Path(f.name).stem[:32]
                        parsed_batch.append((f.name, parsed_item, None))
                        preview_rows.append(
                            {
                                "Filename": f.name,
                                "Agency": parsed_item.agency,
                                "Project #": parsed_item.agency_project_num,
                                "Spec": parsed_item.spec_system,
                                "Parsed Lines": len(parsed_item.lines),
                                "Status": "READY" if parsed_item.lines else "NO LINES DETECTED",
                            }
                        )
                    else:
                        parsed_item = opengov_scraper.parse_opengov_bid_tab_export(
                            raw_bytes,
                            f.name,
                            agency=batch_agency,
                            project_num=Path(f.name).stem[:32],
                            title=f"{batch_agency}: {Path(f.name).stem}",
                            county=batch_county,
                        )
                        parsed_batch.append((f.name, parsed_item, None))
                        preview_rows.append(
                            {
                                "Filename": f.name,
                                "Agency": parsed_item.agency,
                                "Project #": parsed_item.agency_project_num,
                                "Spec": parsed_item.spec_system,
                                "Parsed Lines": len(parsed_item.lines),
                                "Status": "READY" if parsed_item.lines else "NO LINES DETECTED",
                            }
                        )
                except Exception as exc:
                    parsed_batch.append((f.name, None, str(exc)))
                    preview_rows.append(
                        {
                            "Filename": f.name,
                            "Agency": "—",
                            "Project #": "—",
                            "Spec": "—",
                            "Parsed Lines": 0,
                            "Status": f"ERROR: {exc}",
                        }
                    )

            st.dataframe(pd.DataFrame(preview_rows), use_container_width=True, hide_index=True)

            ready_items = [
                (n, p)
                for n, p, err in parsed_batch
                if p is not None and (isinstance(p, tuple) or p.lines)
            ]
            if ready_items and st.button(
                f"Ingest {len(ready_items)} Valid File(s) into Database", type="primary"
            ):
                prog = st.progress(0.0)
                ingested_cnt = 0
                total_lines_cnt = 0
                errs = []
                for idx, (fname, p_obj) in enumerate(ready_items, start=1):
                    try:
                        if isinstance(p_obj, tuple) and p_obj[0] == "E2C2":
                            res_e2c2 = adot_scraper.ingest_e2c2_csv(p_obj[1], DB_PATH)
                            ingested_cnt += res_e2c2["projects"]
                            total_lines_cnt += res_e2c2["bid_lines"]
                        else:
                            adot_scraper.ingest_parsed_project(p_obj, DB_PATH)
                            ingested_cnt += 1
                            total_lines_cnt += len(p_obj.lines)
                    except Exception as exc:
                        errs.append(f"{fname}: {exc}")
                    prog.progress(idx / len(ready_items))
                st.success(
                    f"Batch complete: Ingested **{ingested_cnt}** project(s) "
                    f"and **{total_lines_cnt:,}** bid lines."
                )
                if errs:
                    st.warning(f"Errors: {errs[:5]}")

    with tab_pdf:
        st.markdown("Upload an official ADOT Bid Tabulation PDF (`azdot.gov`) to parse and ingest.")
        pdf_file = st.file_uploader("ADOT Bid Tab PDF", type=["pdf"])
        if pdf_file is not None:
            parsed = adot_scraper.parse_adot_bid_tab_pdf(pdf_file.read(), filename=pdf_file.name)
            st.write(
                f"**Project:** `{parsed.agency_project_num}` — {parsed.title} | "
                f"**County:** {parsed.county or '—'} | **Parsed Lines:** {len(parsed.lines)}"
            )
            if parsed.lines:
                st.dataframe(pd.DataFrame([vars(l) for l in parsed.lines]), use_container_width=True)
                if st.button("Ingest Parsed ADOT Project into Database", type="primary"):
                    pid = adot_scraper.ingest_parsed_project(parsed, DB_PATH)
                    st.success(f"Ingested ADOT project ID {pid} ({len(parsed.lines)} bid lines).")
            else:
                st.warning("No structured pay item lines were detected in this PDF layout.")

    with tab_og:
        st.markdown("Upload a City of Phoenix OpenGov Bid Tabulation CSV or Excel export (MAG specs).")
        og_file = st.file_uploader("OpenGov Export (CSV/XLSX)", type=["csv", "xlsx", "xls"])
        og_num = st.text_input("Project Number", value="PHX-2026-NEW")
        og_title = st.text_input("Project Title", value="City of Phoenix MAG Street Improvements")
        if og_file is not None:
            parsed_og = opengov_scraper.parse_opengov_bid_tab_export(
                og_file.read(),
                og_file.name,
                project_num=og_num,
                title=og_title,
            )
            st.write(f"**Parsed MAG Lines:** {len(parsed_og.lines)}")
            if parsed_og.lines:
                st.dataframe(pd.DataFrame([vars(l) for l in parsed_og.lines]), use_container_width=True)
                if st.button("Ingest MAG Project into Database", type="primary"):
                    pid = adot_scraper.ingest_parsed_project(parsed_og, DB_PATH)
                    st.success(f"Ingested City of Phoenix project ID {pid}.")

    with tab_live:
        c_e2c2, c_phx, c_reset = st.columns(3)
        with c_e2c2:
            if st.button("Sync ADOT Bid Tab PDFs", type="primary"):
                try:
                    res = adot_scraper.run_adot_archive_sync(DB_PATH, limit=15)
                    st.success(
                        f"Discovered {res['discovered']} PDFs | "
                        f"Ingested: {res['ingested']} | Skipped: {res['skipped']}"
                    )
                    if res["errors"]:
                        st.warning(f"Warnings: {res['errors'][:3]}")
                except Exception as exc:
                    st.warning(f"ADOT Bid Tab sync failed: {exc}")

            e2c2_code = st.text_input("ADOT E2C2 Item Code", value="2030301")
            if st.button("Test ADOT E2C2 Query"):
                try:
                    rows = adot_scraper.scrape_adot_e2c2_historical(e2c2_code)
                    st.success(f"Fetched {len(rows)} E2C2 records.")
                    if rows:
                        st.dataframe(pd.DataFrame(rows[:25]), use_container_width=True)
                except Exception as exc:
                    st.warning(f"Live E2C2 request failed (network/proxy or portal change): {exc}")

        with c_phx:
            if st.button("Sync Phoenix OpenGov", type="primary"):
                try:
                    res_og = opengov_scraper.run_phoenix_opengov_sync(DB_PATH, limit=15)
                    st.success(
                        f"Discovered {res_og['discovered']} solicitations | "
                        f"New: {res_og['ingested']} | Skipped: {res_og['skipped']}"
                    )
                except Exception as exc:
                    st.warning(f"OpenGov sync failed: {exc}")

            if st.button("Fetch Phoenix OpenGov Solicitations"):
                try:
                    sols = opengov_scraper.fetch_phoenix_opengov_solicitations(limit=10)
                    st.success(f"Fetched {len(sols)} solicitations.")
                    if sols:
                        st.dataframe(pd.DataFrame([vars(s) for s in sols]), use_container_width=True)
                except Exception as exc:
                    st.warning(f"Live OpenGov request failed: {exc}")

        with c_reset:
            if PROD_MODE:
                st.info("Production Mode active: synthetic demo seeding is disabled.")
            else:
                st.markdown("Reset database to the 210-project Arizona demo dataset.")
                if st.button("Re-seed 210-Project Database"):
                    seed_database(DB_PATH, force=True)
                    st.success("Database re-seeded with 210 Arizona ADOT + MAG projects.")
                    st.rerun()
