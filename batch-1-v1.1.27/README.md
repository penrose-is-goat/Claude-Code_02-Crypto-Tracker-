# Crypto SEC Filing Tracker

**v1.1.27** — Batch 1: SEC Filing Tracker (major rewrite)

Web app that tracks SEC EDGAR filings for crypto/digital-asset content.
Extracts risk sections, generates AI summaries, presents an interactive
dashboard for crypto lawyers.

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

## What Changed in v1.1.27

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
| **1** | SEC Filing Tracker | v1.1.27 current |
| 2 | Regulatory Action Tracker (SEC/CFTC litigation, no-action letters) | Planned |
| 3 | Real-Time Alerts (RSS monitors + push notifications) | Planned |
| 4 | Full SEC Coverage (beyond crypto) | Planned |
| 5 | User accounts + UI configuration + API | Planned |
