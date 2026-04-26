"""
Configuration for Crypto SEC Filing Tracker v1.1.28.

Changes from v1.1.27:
- Update Filings auto-reprocesses cached filings when nothing new to scrape
- No more silent "crash" when DB already has filings
- Reprocess shows live progress in status bar
"""
import os
from datetime import datetime

# ─── Version ──────────────────────────────────────────────────────────────
VERSION = "1.1.28"
VERSION_NAME = "Batch 1 — SEC Filing Tracker (auto-reprocess fix)"

# ─── Paths ───────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "crypto_filings.db")
EDGAR_CACHE_DIR = os.path.join(DATA_DIR, "edgar_cache")
EXPORT_DIR = os.path.join(DATA_DIR, "exports")

# ─── SEC EDGAR Identity (required by SEC fair-access policy) ─────────────
SEC_IDENTITY = "PenroseHayek crypto.hedger23@gmail.com"
SEC_HEADERS = {
    "User-Agent": SEC_IDENTITY,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ─── edgartools local cache (v1.1.27 — major speed fix) ──────────────────
# When enabled, all Filing.text()/.html()/.sections()/.attachments calls
# write to disk on first fetch and serve from disk on subsequent runs.
# Re-runs become 5-10x faster; eliminates rate-limit timeouts.
USE_EDGAR_LOCAL_CACHE = True

# ─── Scraping Parameters ─────────────────────────────────────────────────
START_DATE = "2019-01-01"
END_DATE = datetime.now().strftime("%Y-%m-%d")
NUM_THREADS = 8             # Safe to run high with local cache enabled
MAX_PER_KEYWORD = 25        # Max filings per keyword per form type
HTTP_TIMEOUT = 30
REQUEST_DELAY = 0.10
FILING_TIMEOUT = 120        # Per-filing timeout in seconds

# ─── Search Keywords ─────────────────────────────────────────────────────
KEYWORDS = [
    "cryptocurrency", "blockchain", "digital asset", "bitcoin", "ethereum",
    "XRP", "stablecoin", "crypto ETF", "crypto fund", "crypto token", "DeFi",
]

# ─── SEC Form Types to Search ────────────────────────────────────────────
FORM_TYPES = ["S-1", "S-1/A", "N-1A", "485APOS", "485BPOS", "10-K", "10-Q", "8-K", "D"]

# Forms that represent ETF/Fund offerings (Tier 1)
ETF_FUND_FORMS = {"485APOS", "485BPOS", "N-1A", "S-1", "D"}

# Forms where multiple sub-funds typically share one filing (must pick the crypto one)
MULTI_FUND_FORMS = {"485APOS", "485BPOS", "N-1A", "N-1A/A"}

FORM_DESCRIPTIONS = {
    "10-K": "Annual Report",
    "10-Q": "Quarterly Report",
    "8-K": "Current Report",
    "8-K/A": "Current Report (Amend.)",
    "485APOS": "ETF/Fund Amendment",
    "485BPOS": "ETF/Fund Amendment",
    "N-1A": "ETF/Fund Registration",
    "N-1A/A": "ETF/Fund Reg. (Amend.)",
    "S-1": "Security Offering Reg.",
    "S-1/A": "Security Offering Reg. (Amend.)",
    "D": "Exempt Offering",
    "D/A": "Exempt Offering (Amend.)",
}

# ─── AI Summarization ─────────────────────────────────────────────────────
# v1.1.27: Claude Haiku is the PRIMARY summarizer when API key is set.
# Template summarizer is ONLY used as fallback.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Use Claude when key available; set to False to force template-only
USE_CLAUDE_SUMMARIES = bool(ANTHROPIC_API_KEY)

# Haiku 4.5 is fast and cheap — ~$0.001 per summary. For 2000 filings,
# total cost is ~$2 and each request takes ~2 seconds.
SUMMARY_MODEL = "claude-haiku-4-5-20251001"
SUMMARY_MAX_TOKENS = 400
SUMMARY_TIMEOUT = 30

# ─── Flask ────────────────────────────────────────────────────────────────
FLASK_HOST = "127.0.0.1"
FLASK_PORT = 5000
FLASK_DEBUG = True
SECRET_KEY = os.environ.get("SECRET_KEY", "crypto-tracker-dev-key-change-in-prod")
