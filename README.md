# Crypto SEC Filing Tracker

**v1.1.37** — Batch 1: SEC Filing Tracker

Web app that tracks SEC EDGAR filings for crypto/digital-asset content.
Extracts risk sections, generates AI summaries, presents an interactive
dashboard for crypto lawyers.

## What Changed in v1.1.37

### 1. The Database Is the Source of Truth

Raw filing source is now stored **in the database**, gzip-compressed, at the
moment a filing is first fetched. Previously the only copy lived in a
filesystem cache under `~/.crypto_tracker_cache` that `clear_runtime_caches()`
deletes — so "re-extract everything" silently meant "re-download everything
from SEC."

With the source in the database, improved extraction is re-derived locally.
Browsing never contacts SEC (it did not before either), and now neither does
regeneration.

### 2. Version-Targeted Regeneration

Each filing records the `EXTRACTION_VERSION` that produced its stored data.
An update run rebuilds only the filings whose stamp is behind — not
all-or-nothing reprocessing. Bump `EXTRACTION_VERSION` in `config.py` when
extraction logic changes what gets stored, and the next update regenerates
exactly the affected filings, offline.

Filings where the new extractor produces nothing usable keep their existing
data rather than having it overwritten with a worse result.

`backfill_raw_source()` is the one-time catch-up for filings stored by earlier
versions, which have no raw source in the database yet. It reads the local
cache first and only contacts SEC for what it must.

### 3. Filing Overview and What's New

Alongside the risk-factor summary, each filing now carries:

- **Overview** — plain English on what the filing actually is
  ("Coinbase Global Inc (COIN) — annual report, covering Bitcoin, stablecoins…")
- **What's new** — a diff against that filer's previous filing of the same
  form ("Versus the prior 10-K (2024-02-15): newly discusses valuation")

Both are derived from data already in the database, so they cost no network
requests and no API calls. They work without an Anthropic API key.

### 4. FRED-Inspired Interface

The dark retro theme is replaced with a light, institutional design modeled on
the St. Louis Fed's FRED site: navy masthead, white canvas, hairline rules,
dense readable data tables, restrained blue accents. Base font size increased
from 13px to 15px.

## What Changed in v1.1.36

### Bounded edgartools Fallback

Update runs could crawl for hours at ~0.1 filings/sec with logs full of
`PoolTimeout('')`. All worker threads were piling onto edgartools' shared
throttled HTTP client (5s pool timeout, its own rate limiter). Worse, those
errors were swallowed and recorded as terminal "text too short (0 chars)"
skips, permanently dropping real filings.

edgartools calls now run behind a concurrency gate with retry on transient
timeouts, its rate limit is capped so the combined request rate stays under
SEC's 10/s, and empty fetched text is a retryable failure rather than a
terminal skip.

### Analysis Runs Automatically

`exact_only` mode defers summaries by design, but nothing in the UI ever
triggered the code that generates them, so filings sat at "Analysis pending"
forever. Analysis now runs as a phase of every update and reprocess.

## What Changed in v1.1.35

### 1. Faster Filing Discovery

Discovery now prefers direct SEC submissions JSON for known CIKs, one pull per
company, then dedupes by accession before any filing document is fetched. EFTS
search responses and submissions JSON are cached separately, and already-seen
accessions are skipped unless a reprocess path asks for cached work.

### 2. Exact Source-Backed Risk Extraction

Risk extraction now stores the actual filing section text before analysis. For
10-K/10-Q filings it extracts the body `Item 1A` section through the next peer
item heading. For prospectus and ETF filings it extracts from `Principal
Investment Risks`, `Principal Risks`, or `Risk Factors` through the next peer
prospectus section. TOC spans and fee-table spans are rejected.

### 3. Provenance and Hash-Based Analysis Skips

Each extracted risk section records source provenance: accession, document name
and URL, source hash, exact text hash, extraction method, confidence, and
offsets where available. Summaries are keyed by exact text hash, so unchanged
source text skips analysis while new or changed risk text is re-summarized.

### 4. Optional edgartools Fallback

Direct SEC HTTP is now the primary path for submissions and raw document fetches.
edgartools remains available as a fallback when installed, but Flask startup and
direct processing no longer depend on edgartools import side effects.

### 5. Verification with Real Filing Text Fixtures

The offline test suite uses saved SEC filing text fixtures from Coinbase's
2024 10-K and iShares Bitcoin Trust's 2024 424B3 prospectus, then compares
extracted text against source offsets and exact hashes. Coverage includes 10-K
body extraction, TOC rejection, prospectus risk extraction, fee-table rejection,
multi-fund crypto document selection, cache namespacing, submissions-cache
performance, API filters, exports, and Flask routes. `python test_extraction.py
--live` re-fetches the curated SEC source documents and verifies the same exact
hashes without using paid analysis services.

## What Changed in v1.1.34

### 1. Per-Filing Contextual Metadata (Type / Purpose / Holdings)

Previously, every filing from the same company showed identical static text.
Now each filing gets context-aware descriptions based on what the entity IS
(ETF, miner, exchange, treasury company) AND what this form type MEANS:

| Entity | Form | Type | Purpose (excerpt) |
|--------|------|------|-------------------|
| IBIT | 485APOS | ETF — Post-effective prospectus amendment | ...a spot BTC ETF. Updates prospectus disclosures including risk factors and fund performance. |
| Coinbase | 8-K | Crypto Exchange — Current report | ...a cryptocurrency exchange. Discloses material event such as regulatory action, product launch, or executive change. |
| Marathon | 10-Q | Bitcoin Miner — Quarterly report | ...a BTC mining company. Quarterly mining output, revenue, and operational metrics. |
| MicroStrategy | 8-K | Corporate Treasury — Current report | ...holds BTC as a corporate treasury reserve. Material event such as additional crypto purchase, sale, or strategy change. |

Holdings are also entity-aware:
- ETF: "Holds spot BTC in trust for shareholders"
- Miner: "Mines and holds BTC as primary business output"
- Exchange: "Operates marketplace for BTC, ETH, USDT, SOL, DOGE and other digital assets"
- Company: "Holds BTC as corporate treasury reserve asset"

### 2. Clear "Up to date" Messaging

Previously: "found 2000, saved 0" (confusing — implies failure).
Now: "Up to date: all 537 filings already processed (0.2min)" — when there
are genuinely no new filings on EDGAR since the last run.

### 3. Automatic Metadata Backfill on Upgrade

When you first run v1.1.34+ on an existing database, it automatically
regenerates Type/Purpose/Holdings for all existing filings using the new
per-filing template system. No re-scraping required.

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
  config.py           Version, cache settings, API keys, keywords, forms
  database.py         4-table normalized schema + v_filings_display view
  scraper.py          Combined OR-query search + parallel processing
  extractor.py        3-pattern extraction pipeline with confidence scoring
  summarizer.py       Claude Haiku (primary) + template (fallback)
  filing_metadata.py  Per-filing Type/Purpose/Holdings template engine
  app.py              Flask routes incl. /api/reprocess for no-rescrape
  templates/          Jinja2 HTML (dashboard, filings, settings, detail)
  static/css/         Dark theme
  data/               SQLite + edgar_cache/ (gitignored)
run.py                Entry point
test_extraction.py    19 unit tests + 5 live tests
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
| **1** | SEC Filing Tracker | v1.1.37 current |
| 2 | Regulatory Action Tracker (SEC/CFTC litigation, no-action letters) | Planned |
| 3 | Real-Time Alerts (RSS monitors + push notifications) | Planned |
| 4 | Full SEC Coverage (beyond crypto) | Planned |
| 5 | User accounts + UI configuration + API | Planned |
