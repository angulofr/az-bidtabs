"""AZ BidTabs — Arizona Heavy-Civil Bid Tabulation Intelligence (MVP)."""

from __future__ import annotations

import os
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

from src.db import (
    DEFAULT_DB_PATH,
    compute_unit_price_stats,
    get_contractor_scorecard,
    get_filter_options,
    get_head_to_head,
    get_overview_stats,
    query_pay_item_observations,
)
from src.matching import benchmark_schedule
from src.scrapers.adot import (
    ingest_parsed_project,
    parse_adot_bid_tab_pdf,
    run_adot_archive_sync,
    scrape_adot_e2c2_historical,
)
from src.scrapers.phoenix_opengov import (
    fetch_phoenix_opengov_solicitations,
    parse_opengov_bid_tab_export,
    run_phoenix_opengov_sync,
)
from src.seed import seed_database

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

# Ensure database is initialized & seeded on first run
DB_PATH: Path = DEFAULT_DB_PATH
seed_database(DB_PATH)

# ---------------------------------------------------------------------------
# Sidebar Navigation
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("## 🏗️ AZ BidTabs")
    st.markdown('<span class="az-badge">ADOT + MAG SPEC</span>', unsafe_allow_html=True)
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
# Page 1: Market Overview
# ---------------------------------------------------------------------------
if page == "Market Overview":
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
            display_rec = rec_df.copy()
            display_rec["engineers_estimate"] = display_rec["engineers_estimate"].map(
                lambda v: f"${v:,.0f}" if pd.notna(v) else "—"
            )
            display_rec["winning_bid_total"] = display_rec["winning_bid_total"].map(
                lambda v: f"${v:,.0f}" if pd.notna(v) else "—"
            )
            st.dataframe(
                display_rec,
                use_container_width=True,
                hide_index=True,
                height=340,
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
        "quantity",
        "unit_price",
        "extended_total",
        "bid_rank",
        "is_winner",
    ]
    st.dataframe(df_filtered[show_cols], use_container_width=True, hide_index=True)


# ---------------------------------------------------------------------------
# Page 3: Competitor Intel & Head-to-Head
# ---------------------------------------------------------------------------
elif page == "Competitor Intel & Head-to-Head":
    st.title("Competitor Intel & Head-to-Head")
    st.caption("Track contractor win rates, aggressiveness vs. Engineer's Estimate, and line-item head-to-head pricing.")

    scorecard = get_contractor_scorecard(DB_PATH)
    st.subheader("Contractor Scorecard")

    if not scorecard.empty:
        disp_sc = scorecard.copy()
        disp_sc["win_rate_pct"] = disp_sc["win_rate_pct"].map(lambda v: f"{v:.1f}%")
        disp_sc["avg_pct_vs_ee"] = disp_sc["avg_pct_vs_ee"].map(
            lambda v: f"{v:+.2f}%" if pd.notna(v) else "—"
        )
        disp_sc["total_bid_volume"] = disp_sc["total_bid_volume"].map(lambda v: f"${v:,.0f}")
        disp_sc["won_volume"] = disp_sc["won_volume"].map(lambda v: f"${v:,.0f}")
        st.dataframe(
            disp_sc.drop(columns=["contractor_id"]),
            use_container_width=True,
            hide_index=True,
        )

    st.divider()
    st.subheader("Head-to-Head Line-Item Comparison")
    contractor_names = scorecard["contractor_name"].tolist() if not scorecard.empty else []
    if len(contractor_names) >= 2:
        ca, cb = st.columns(2)
        contractor_a = ca.selectbox("Contractor A (Your Firm / Target)", contractor_names, index=0)
        default_b_idx = 1 if len(contractor_names) > 1 else 0
        contractor_b = cb.selectbox("Contractor B (Competitor)", contractor_names, index=default_b_idx)

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
                )

                st.markdown("**Line-Item Unit Price Comparison (Sorted by $ Impact)**")
                st.dataframe(line_h2h, use_container_width=True, hide_index=True)


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

        st.dataframe(bm_df, use_container_width=True, hide_index=True)

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
    st.caption("Ingest ADOT Bid Tabulation PDFs, City of Phoenix OpenGov exports, or run live portal checks.")

    tab_pdf, tab_og, tab_live = st.tabs(
        ["ADOT Bid Tab PDF Parser", "City of Phoenix OpenGov Import", "Live Scraper Checks & DB Admin"]
    )

    with tab_pdf:
        st.markdown("Upload an official ADOT Bid Tabulation PDF (`azdot.gov`) to parse and ingest into SQLite.")
        pdf_file = st.file_uploader("ADOT Bid Tab PDF", type=["pdf"])
        if pdf_file is not None:
            parsed = parse_adot_bid_tab_pdf(pdf_file.read())
            st.write(
                f"**Project:** `{parsed.agency_project_num}` — {parsed.title} | "
                f"**County:** {parsed.county or '—'} | **Parsed Lines:** {len(parsed.lines)}"
            )
            if parsed.lines:
                st.dataframe(pd.DataFrame([vars(l) for l in parsed.lines]), use_container_width=True)
                if st.button("Ingest Parsed ADOT Project into Database", type="primary"):
                    pid = ingest_parsed_project(parsed, DB_PATH)
                    st.success(f"Ingested ADOT project ID {pid} ({len(parsed.lines)} bid lines).")
            else:
                st.warning("No structured pay item lines were detected in this PDF layout.")

    with tab_og:
        st.markdown("Upload a City of Phoenix OpenGov Bid Tabulation CSV or Excel export (MAG specs).")
        og_file = st.file_uploader("OpenGov Export (CSV/XLSX)", type=["csv", "xlsx", "xls"])
        og_num = st.text_input("Project Number", value="PHX-2026-NEW")
        og_title = st.text_input("Project Title", value="City of Phoenix MAG Street Improvements")
        if og_file is not None:
            parsed_og = parse_opengov_bid_tab_export(
                og_file.read(),
                og_file.name,
                project_num=og_num,
                title=og_title,
            )
            st.write(f"**Parsed MAG Lines:** {len(parsed_og.lines)}")
            if parsed_og.lines:
                st.dataframe(pd.DataFrame([vars(l) for l in parsed_og.lines]), use_container_width=True)
                if st.button("Ingest MAG Project into Database", type="primary"):
                    pid = ingest_parsed_project(parsed_og, DB_PATH)
                    st.success(f"Ingested City of Phoenix project ID {pid}.")

    with tab_live:
        c_e2c2, c_phx, c_reset = st.columns(3)
        with c_e2c2:
            if st.button("Sync ADOT Bid Tab PDFs", type="primary"):
                try:
                    res = run_adot_archive_sync(DB_PATH, limit=15)
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
                    rows = scrape_adot_e2c2_historical(e2c2_code)
                    st.success(f"Fetched {len(rows)} E2C2 records.")
                    if rows:
                        st.dataframe(pd.DataFrame(rows[:25]), use_container_width=True)
                except Exception as exc:
                    st.warning(f"Live E2C2 request failed (network/proxy or portal change): {exc}")

        with c_phx:
            if st.button("Sync Phoenix OpenGov", type="primary"):
                try:
                    res_og = run_phoenix_opengov_sync(DB_PATH, limit=15)
                    st.success(
                        f"Discovered {res_og['discovered']} solicitations | "
                        f"New: {res_og['ingested']} | Skipped: {res_og['skipped']}"
                    )
                except Exception as exc:
                    st.warning(f"OpenGov sync failed: {exc}")

            if st.button("Fetch Phoenix OpenGov Solicitations"):
                try:
                    sols = fetch_phoenix_opengov_solicitations(limit=10)
                    st.success(f"Fetched {len(sols)} solicitations.")
                    if sols:
                        st.dataframe(pd.DataFrame([vars(s) for s in sols]), use_container_width=True)
                except Exception as exc:
                    st.warning(f"Live OpenGov request failed: {exc}")

        with c_reset:
            st.markdown("Reset SQLite database to the 210-project Arizona dataset.")
            if st.button("Re-seed 210-Project Database"):
                seed_database(DB_PATH, force=True)
                st.success("Database re-seeded with 210 Arizona ADOT + MAG projects.")
                st.rerun()
