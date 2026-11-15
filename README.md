# AZ BidTabs — Arizona Heavy-Civil Bid Tabulation Intelligence

A modern, Arizona-first heavy-civil bid tabulation archive and unit-price intelligence SaaS for **ADOT** and **Municipal / MAG** agencies (City of Phoenix, Mesa, Scottsdale, Chandler, Goodyear, MCDOT, Pima County).

## Features (v1 MVP)
1. **Pay Item Explorer:** Search by ADOT/MAG code or description; filter by spec system, agency, county, winners-only, and quantity range slider; view Observations, P25, Median, P75, Quantity-Weighted Avg, `Unit Price vs. Quantity` scatter plot, and CSV export.
2. **Competitor Intel & Head-to-Head:** Contractor win-rate scorecard, `% vs. Engineer's Estimate`, and line-item head-to-head comparison on overlapping projects.
3. **Schedule Benchmarker:** Upload a bid schedule CSV (`item_code, quantity, your_unit_price`) with exact + fuzzy cross-spec (ADOT <-> MAG) matching and automatic >20% variance flagging.
4. **Scrapers & Parsers:** ADOT Bid Tabulation PDF parser (`pdfplumber`), ADOT E2C2 Historical Price scraper, and City of Phoenix OpenGov Procurement scraper/importer.
5. **Invite-Code Gate:** Optional `APP_ACCESS_CODE` via environment variable or Streamlit Secrets.

## Local Development

```bash
uv venv .venv
source .venv/bin/activate
uv pip install -r requirements.txt
streamlit run app.py
```

Run tests:

```bash
pytest -v
```

## Deploying to Streamlit Community Cloud
1. Push this repository to GitHub.
2. Create a new app on `share.streamlit.io` pointing to `app.py`.
3. (Optional) Add `APP_ACCESS_CODE = "your-invite-code"` in **App Settings -> Secrets** to enable the invite-code gate.
