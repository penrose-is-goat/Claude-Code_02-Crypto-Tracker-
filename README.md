# Crypto SEC Filing Tracker

Multi-batch project tracking SEC EDGAR filings and regulatory actions related to digital assets, cryptocurrency, and blockchain.

## Versions

Each version lives in its own subfolder. To run a specific version:

```bash
cd <version-folder>
pip install -r requirements.txt
python run.py
# Open http://127.0.0.1:5000
```

| Version | Folder | Status | Notes |
|---------|--------|--------|-------|
| **v1.1.27** | [`batch-1-v1.1.27/`](batch-1-v1.1.27/) | Current — ready to test | Major rewrite: normalized DB (4 tables), edgartools local cache, collapsed OR-query search, boundary-pair + prospectus-header extraction, Claude Haiku summaries. 19/19 unit tests pass. |
| v1.1.26 | [`batch-1-v1.1.26/`](batch-1-v1.1.26/) | Superseded | Took 2 hours to run; extraction bugs persisted across TOC, fee schedules, and multi-fund filings. |
| v1.1.25 | [`batch-1-v1.1.25/`](batch-1-v1.1.25/) | Superseded | Initial Flask rebuild. Extraction had issues (wrong sections, fee schedules, partial results). |
| Pre-1.0 (v24) | `crypto_tracker_v24.py` | Reference only | Original Colab notebook — monolithic script producing static HTML. |

## Project Roadmap

| Batch | Description | Status |
|-------|-------------|--------|
| **1** | SEC Filing Tracker — Crypto filings from EDGAR with risk sections, summaries, interactive dashboard | In Progress (v1.1.26) |
| **2** | Regulatory Action Tracker — Statements and regulations from SEC, CFTC, Congress, Treasury, state regulators | Planned |
| **3** | Real-Time Alerts — Push notifications for new filings/regulations, searchable/downloadable sections, email/text alerts | Planned |
| **4** | Full SEC Coverage — Expand beyond crypto to all SEC filings and regulatory actions | Planned |
| **5** | Configuration & Polish — User accounts, configurable settings via UI, API access | Planned |

## Version Naming Convention

`batch-<N>-v<major>.<minor>.<patch>` — e.g., `batch-1-v1.1.26` = Batch 1, minor revision 1, patch 26.

- Batch 1 versions: `v1.1.x`
- Batch 2 versions: `v1.2.x`
- Batch 3 versions: `v1.3.x`

Each version folder is self-contained with its own `crypto_tracker/`, `requirements.txt`, `run.py`, and README. Run whichever version you want independently.

## Which Version Should I Run?

**Run the highest version number that's marked "ready to test" or "stable".** Currently: `batch-1-v1.1.27/`.
