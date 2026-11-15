# AZ BidTabs — Arizona Heavy-Civil Bid Tabulation Intelligence

A modern, Arizona-first heavy-civil bid tabulation archive and unit-price intelligence SaaS for **ADOT** and **Municipal / MAG** agencies (City of Phoenix, Mesa, Scottsdale, Chandler, Goodyear, MCDOT, Pima County).

## Features (v1)
1. **Pay Item Explorer:** Search by ADOT/MAG code or description; filter by spec system, agency, county, winners-only, and quantity range slider; view Observations, P25, Median, P75, Quantity-Weighted Avg, `Unit Price vs. Quantity` scatter plot, and CSV export.
2. **Competitor Intel & Head-to-Head:** Contractor win-rate scorecard (with approximate employee counts), `% vs. Engineer's Estimate`, and line-item head-to-head comparison on overlapping projects.
3. **Schedule Benchmarker:** Upload a bid schedule CSV (`item_code, quantity, your_unit_price`) with exact + fuzzy cross-spec (ADOT <-> MAG) matching and automatic >20% variance flagging.
4. **Multi-File Batch Ingestion:** Upload dozens of ADOT Bid Tab PDFs (`pdfplumber`) and OpenGov/MCDOT CSV/XLSX exports at once in the UI or via `scripts/import_folder.py`.
5. **Dual-Backend Persistence & Production Mode:** Supports **Postgres (`DATABASE_URL`)** for persistent production deployments and **SQLite** for local dev/testing.

## Local Development

```bash
uv venv .venv
source .venv/bin/activate
uv pip install -r requirements.txt
streamlit run app.py
```

Batch-import a local folder of downloaded bid tabs into SQLite or Postgres (`DATABASE_URL`):

```bash
python scripts/import_folder.py /path/to/bidtabs_folder --agency "City of Phoenix"
```

Run tests:

```bash
pytest -v
```

## Automated Weekly ADOT Sync (Windows / Mac)
Because `azdot.gov` blocks datacenter/cloud IPs, run the local Playwright sync script on your PC (or schedule it weekly in Windows Task Scheduler):

1. Create `.env.local` in the repo root with your Neon connection string:
   ```env
   DATABASE_URL=postgresql://...
   ```
2. Double-click `scripts\sync_adot_windows.bat` (or run from terminal):
   ```bash
   pip install -r requirements.txt playwright
   playwright install chromium
   python scripts/sync_adot_browser.py --limit 100 --save-dir data/adot_pdfs
   ```
   It skips already-archived TRACS numbers, downloads only new ADOT Bid Tab PDFs, parses all bidders & line items, and inserts directly into Neon Postgres.

## Deploying to Streamlit Community Cloud (Production Mode + Postgres)
1. Create a free Postgres database on [Supabase](https://supabase.com) or [Neon](https://neon.tech) and copy the connection URI (`postgresql://...`).
2. Push this repository to GitHub and deploy `app.py` on [share.streamlit.io](https://share.streamlit.io).
3. In **App settings → Secrets**, add:
   ```toml
   DATABASE_URL = "postgresql://user:password@host:5432/dbname?sslmode=require"
   PRODUCTION_MODE = "true"
   APP_ACCESS_CODE = "your-invite-code"
   ```
   When `DATABASE_URL` or `PRODUCTION_MODE = "true"` is set, synthetic demo seeding is disabled and only your ingested bid tabulations are stored permanently in Postgres.
