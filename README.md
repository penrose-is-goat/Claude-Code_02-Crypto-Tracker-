# Crypto SEC Filing Tracker

**v1.1.33.1** — Batch 1: SEC Filing Tracker

Web app that tracks SEC EDGAR filings for crypto/digital-asset content.
Extracts risk sections, generates AI summaries, presents an interactive
dashboard for crypto lawyers.

> **v1.1.33.1 is a hotfix release.** v1.1.33 crashed on every startup with
> `sqlite3.OperationalError: database is locked`. v1.1.33.1 carries the
> exact same features and fixes that bug. See "Hotfix: v1.1.33.1" below
> for the root cause.

## Quick Start

```bash
pip install -r requirements.txt

# Optional but recommended: set Anthropic API key for Claude summaries
# Without it, the app falls back to template summaries.
export ANTHROPIC_API_KEY=sk-ant-...

python run.py
# Open http://127.0.0.1:5000
# Click "Update Filings" to populate the database
```

## Hotfix: v1.1.33.1

**Symptom (in v1.1.33):** Server starts, prints "Running on http://127.0.0.1:5000",
then crashes with:

```
sqlite3.OperationalError: database is locked
  File "...\crypto_tracker\database.py", line 75, in init_db
    conn.executescript(...)
```

The webpage never loads.

**Root cause:** v1.1.33 added `_backfill_company_details(conn)` to `init_db()`.
That extra `UPDATE` extended `init_db()`'s runtime enough that Flask's debug-mode
auto-reloader (which spawns a *second* Python process) hit a race condition:

1. Parent process: `init_db()` starts, runs DDL, then begins backfill UPDATE
   (holds a write lock briefly)
2. Flask spawns child process for the actual dev server
3. Child process: also calls `init_db()`, hits `DROP VIEW IF EXISTS`
4. `conn.executescript()` does **not** respect SQLite's `busy_timeout`, so the
   child crashes instantly instead of waiting for the parent's lock to release

v1.1.32 didn't hit this because its `init_db()` had no backfill — it finished
fast enough that parent and child never overlapped. Pure luck.

**Fix:** `init_db()` now tracks schema version via `PRAGMA user_version`.

- **First run on a given DB:** runs DDL + backfill (slow path, with
  retry-on-lock as a safety net), then writes `PRAGMA user_version = 2`.
- **Every subsequent run** (including the reloader child, and every Flask
  route that re-calls `init_db()`): reads the version, sees it's current,
  returns immediately. **Zero write locks taken**, so no race possible.

To force a one-time re-backfill in the future (e.g. when more entries are
added to `COMPANY_DETAILS`), bump `SCHEMA_VERSION` in `database.py`.

## What Changed in v1.1.33

Focused speed + UX pass after researching the edgar.tools/edgartools/datamule
ecosystems and verifying claims locally. The big-ticket "switch to the new
edgartools Document API" path was tested and rejected (6-13× SLOWER than the
existing BS4+lxml pipeline). Real wins this version:

### 1. Parallel CIK search (~5× faster on that phase)
`_search_by_company_cik` previously walked 54 companies × 5 forms serially.
Now runs through a `ThreadPoolExecutor(max_workers=6)`. Each worker fetches
one company's filings in isolation; main thread dedups under a lock. Saves
several minutes per Update click.

### 2. EFTS response cache (instant repeat searches)
Every EFTS HTTP response is cached by sha256(query|form|dates) into
`~/.crypto_tracker_data/efts_cache/`. TTL is 1 hour. Click "Update Filings"
a second time within the hour and the entire search phase is near-instant.

### 3. Backfill purpose/holdings on existing 700 filings
`init_db()` now runs a one-shot UPDATE that joins existing filings' CIKs
against `COMPANY_DETAILS` and fills in purpose + holdings for everything
that wasn't yet populated. No re-download, no Claude calls.

### 4. COMPANY_DETAILS expanded 31 → 54 entries
Added 11 spot Bitcoin ETF issuers (IBIT, FBTC, ARKB, BITB, HODL, BTCO, BTCW,
BRRR, EZBC, GBTC, DEFI), 7 spot Ethereum ETF issuers (ETHA, FETH, ETHE, ETH
mini, ETHW, EZET, ETHV), and 5 newer treasury/payments/mining names
(Circle/CRCL, BitMine, ETHZilla, SOL Strategies, Bitdeer). All CIKs
verified against SEC EDGAR before adding.

### 5. Prominent in-page export buttons (no more downloading the .db)
The All Filings page now has dedicated "Download Excel" and "CSV" buttons
in its header, with a banner explaining that the raw SQLite `.db` on disk
is binary and won't open in Excel. Exports respect any active filter, so
"Download Excel" downloads exactly what you're looking at — including the
full risk-section text in a cell.

### 6. Inline risk-section size hint
The "Show Full Risk Section" button on each filing card now displays the
character count so you know how big the extracted text is before expanding.

## What Did NOT Change (and Why)

I tested and rejected several "improvements" the research agents proposed:

- **edgartools `Document.get_sec_section()`** — 6-13× slower than our BS4+lxml
  path on every realistic HTML size we tried. The smarter section detection
  isn't worth the overhead for a batch pipeline.
- **edgartools `prepare_for_llm()`** — method signature exists but crashes
  with `ModuleNotFoundError: edgar.documents.ai` in 5.33.0. Not shipped.
- **edgartools `filing.sgml()` for HTTP savings** — `filing.html()` already
  calls `.sgml()` internally in 5.33. We already get the one-call benefit.
- **datamule library** — 200+ PyPI releases = high API churn risk, and it
  doesn't beat what edgartools already does for us.
- **SEC daily bulk archive** — terabytes of unrelated data to find our
  ~700 crypto filings. Wrong tool for targeted pulls.

## What Changed in v1.1.27 (baseline rewrite)

v1.1.26 was 2 hours slow and had the same extraction bugs as v24. v1.1.27
is a structural rewrite that addresses the root causes:

### 1. Normalized Database Schema
Four tables replace the single `filings` table:
- `filings` — metadata (one row per filing)
- `filing_documents` — raw cached text of each attachment
- `filing_sections` — extraction candidates with confidence scores
- `filing_summaries` — AI/template summaries

Result: re-extracting or re-summarizing no longer requires re-scraping.
Call `POST /api/reprocess` to regenerate sections/summaries from cached text.

### 2. Proven Extraction Patterns
Three patterns adopted from open-source SEC tooling:

- **Boundary-pair regex with largest-span selection** (from nlpaueb/edgar-crawler):
  match `(Item 1A .. Item 1B)` pairs, pick the LARGEST span. TOC entries
  are adjacent (small spans), body sections are huge. Kills the "extracted
  the TOC" bug.
- **Structural HTML prefilter** (from alphanome-ai/sec-parser): BeautifulSoup
  drops TOC tables, unwraps inline tags, then regex runs on clean text.
- **Dual-signal validation** (from SECurityTr8Ker/FinBERT pattern): sections
  must pass header match AND keyword-density check. Rejects fee schedules
  and cross-references.

### 3. edgartools Local Cache
`EDGAR_USE_LOCAL_DATA=True` — all filing fetches are cached to disk.
Re-runs are 5-10x faster. Eliminates rate-limit timeouts.

### 4. Collapsed Search Query
99 search calls → 9. One combined OR-query per form instead of per-keyword.

### 5. Claude Haiku Summaries
If `ANTHROPIC_API_KEY` is set, each filing is summarized by Claude Haiku 4.5
with a structured prompt designed for crypto lawyers. Cost: ~$2 for 2000
filings. Template fallback if no key.

### 6. Multiple Candidates with Confidence
Every filing gets up to 5 extraction candidates with confidence scores.
The primary one (highest confidence) drives the display, but alternates
are queryable in the detail page — and you can click "Make Primary" on
any alternate to override the auto-pick.

## Testing

19 unit tests run offline without network. All pass:

```bash
python test_extraction.py           # Offline unit tests (19 tests)
python test_extraction.py --live    # + live tests against real SEC filings
```

Tests cover: boundary-pair extraction, prospectus-header extraction,
HTML cleaning (TOC removal), dual-signal validation, full pipeline on
mock 10-K and mock 485APOS filings.

## Architecture

```
crypto_tracker/
  config.py          Version, cache settings, API keys, keywords, forms
  database.py        4-table normalized schema + v_filings_display view
  scraper.py         Combined OR-query search + parallel processing
  extractor.py       3-pattern extraction pipeline with confidence scoring
  summarizer.py      Claude Haiku (primary) + template (fallback)
  app.py             Flask routes incl. /api/reprocess for no-rescrape
  templates/         Jinja2 HTML (dashboard, filings, settings, detail)
  static/css/        Dark theme
  data/              SQLite + edgar_cache/ (gitignored)
run.py               Entry point
test_extraction.py   19 unit tests + 5 live tests
```

## References

Research sources that informed this rewrite:
- [nlpaueb/edgar-crawler](https://github.com/nlpaueb/edgar-crawler) — boundary-pair regex pattern
- [alphanome-ai/sec-parser](https://github.com/alphanome-ai/sec-parser) — HTML structural prefilter
- [pancak3lullz/SECurityTr8Ker](https://github.com/pancak3lullz/SECurityTr8Ker) — dual-signal pattern
- [dgunning/edgartools](https://github.com/dgunning/edgartools) — local cache + Fund API
- [ProsusAI/finBERT](https://github.com/ProsusAI/finBERT) — financial-domain validation idea

## Project Roadmap

| Batch | Description | Status |
|-------|-------------|--------|
| **1** | SEC Filing Tracker | v1.1.33.1 current |
| 2 | Regulatory Action Tracker (SEC/CFTC litigation, no-action letters) | Planned |
| 3 | Real-Time Alerts (RSS monitors + push notifications) | Planned |
| 4 | Full SEC Coverage (beyond crypto) | Planned |
| 5 | User accounts + UI configuration + API | Planned |
