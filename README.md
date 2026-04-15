# Crypto SEC Filing Tracker

**v1.1.26** — Batch 1: SEC Filing Tracker

A web application that tracks SEC EDGAR filings related to digital assets, cryptocurrency, and blockchain companies/funds. Extracts risk sections, generates summaries, and provides an interactive dashboard.

## Quick Start

```bash
pip install -r requirements.txt
python run.py
# Open http://127.0.0.1:5000 in your browser
# Click "Update Filings" to populate the database
```

## Architecture

```
crypto_tracker/
  config.py          Settings (keywords, form types, dates, API keys)
  database.py        SQLite schema, queries, CSV/Excel export
  scraper.py         EDGAR search + edgartools Filing object processing
  extractor.py       Risk section extraction (edgartools sections/search/attachments)
  summarizer.py      Template-based summarization (optional AI via Anthropic API)
  app.py             Flask web server, routes, scraper control
  templates/         Jinja2 HTML templates (dashboard, filings, settings, detail)
  static/css/        Dark theme stylesheet
  data/              SQLite database + exports (gitignored)
run.py               Entry point (web server or CLI scraper)
test_extraction.py   Validation test suite for extraction quality
```

**Stack:** Python 3.10+ / Flask / SQLite / edgartools / Chart.js / BeautifulSoup

**Data Flow:**
1. `scraper.py` searches EDGAR EFTS via `edgartools.search_filings()`
2. For each filing, `edgartools.get_by_accession_number()` retrieves a Filing object
3. `extractor.py` uses `Filing.sections()`, `Filing.search()`, and `Filing.attachments` to find and extract the crypto-relevant risk section
4. `summarizer.py` generates a structured summary describing the filing type, entity, and key risks
5. Results stored in SQLite, served via Flask to the dashboard and filings pages

## Pages

| Page | URL | Description |
|------|-----|-------------|
| Dashboard | `/` | Interactive charts (monthly filings, top companies, form types, keywords). Click any chart element to filter. |
| All Filings | `/filings` | Full filing list with date/company/type filters, AI summaries, and expandable risk section dropdowns. |
| Filing Detail | `/filing/<accession>` | Single filing view with full metadata, summary, and complete risk section. |
| Settings | `/settings` | Version info, API keys, scraping parameters, database stats, documentation links. |

## Key Features (Batch 1)

- Searches 11 crypto keywords across 9 SEC form types (S-1, N-1A, 485APOS, 10-K, 10-Q, 8-K, D)
- Uses edgartools' built-in document parsing instead of raw HTTP + regex
- Multi-fund filing handling: scores each document by crypto*risk density to find the right fund
- Risk section validation: rejects fee schedules and non-crypto sections
- Incremental database: only processes new filings on each update
- Export to CSV or Excel
- Background scraper with progress bar

## Version History

### v1.1.26 (2026-04-14) — Major extraction rewrite
- Replaced raw HTTP document downloads with edgartools Filing objects
- Uses `Filing.sections()`, `Filing.search()`, `Filing.attachments` for proper document parsing
- Rebuilt summarizer: describes filing type, entity, crypto assets, specific risk areas
- Non-risk filings (8-K, D) now properly summarized by purpose/parties/key terms
- Fee schedule rejection in sub-heading detection
- Added settings page, test suite, version labeling
- Added README with project architecture

### v1.1.25 (2026-04-12) — Flask web app rebuild
- Replaced monolithic Colab notebook with structured Flask application
- Separated into modules (config, database, scraper, extractor, summarizer)
- Two-page web app with interactive dashboard
- SQLite database with export capability

### Pre-1.0 (v20-v24) — Google Colab prototypes
- Single Python script generating static HTML
- Raw HTTP downloads from SEC filing index pages
- Regex-based risk section extraction (unreliable for multi-fund filings)
- Keyword-based summaries (not actually AI-generated)

## Project Roadmap

| Batch | Description | Status |
|-------|-------------|--------|
| **1** | SEC Filing Tracker — Crypto filings from EDGAR with risk sections, summaries, interactive dashboard | In Progress |
| **2** | Regulatory Action Tracker — Statements and regulations from SEC, CFTC, Congress, Treasury, state regulators | Planned |
| **3** | Real-Time Alerts — Push notifications for new filings/regulations, searchable/downloadable sections, email/text alerts | Planned |
| **4** | Full SEC Coverage — Expand beyond crypto to all SEC filings and regulatory actions | Planned |
| **5** | Configuration & Polish — User accounts, configurable settings via UI, API access | Planned |

## Running the Test Suite

```bash
python test_extraction.py
```

Downloads 40 real filings from SEC EDGAR and validates:
- Risk section extraction quality
- Summary informativeness
- Fee schedule rejection
- Crypto content validation

Target: 75%+ pass rate.

## Configuration

Edit `crypto_tracker/config.py` to change:
- `SEC_IDENTITY` — Your name and email for EDGAR fair-access policy
- `START_DATE` / `END_DATE` — Date range for filing search
- `KEYWORDS` — Crypto search terms
- `FORM_TYPES` — SEC form types to search
- `ANTHROPIC_API_KEY` — Optional, for AI-powered summaries
- `FLASK_PORT` — Web server port (default 5000)

## Data Sources

- [SEC EDGAR Full-Text Search (EFTS)](https://efts.sec.gov/LATEST/)
- [SEC Digital Assets Landing Page](https://www.sec.gov/digital-assets)
- [edgartools Python Library](https://pypi.org/project/edgartools/)
