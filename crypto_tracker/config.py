"""
Configuration for Crypto SEC Filing Tracker.
Edit these settings to customize scraping behavior, database location, etc.
"""
import os
from datetime import datetime

# ─── Paths ───────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.path.join(BASE_DIR, "data")
DB_PATH = os.path.join(DATA_DIR, "crypto_filings.db")

# ─── SEC EDGAR Identity (required by SEC fair-access policy) ─────────────
SEC_IDENTITY = "PenroseHayek crypto.hedger23@gmail.com"
SEC_HEADERS = {
    "User-Agent": SEC_IDENTITY,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ─── Scraping Parameters ─────────────────────────────────────────────────
START_DATE = "2019-01-01"
END_DATE = datetime.now().strftime("%Y-%m-%d")
NUM_THREADS = 8
MAX_PER_KEYWORD = 20       # Max filings per keyword per form type
HTTP_TIMEOUT = 30           # Seconds
REQUEST_DELAY = 0.12        # Delay between EDGAR search requests (be polite)
DOWNLOAD_DELAY = 0.05       # Delay between document downloads
MAX_DOCS_PER_FILING = 8    # Max HTML documents to download per filing

# ─── Search Keywords ─────────────────────────────────────────────────────
KEYWORDS = [
    "cryptocurrency", "blockchain", "digital asset", "bitcoin", "ethereum",
    "XRP", "stablecoin", "crypto ETF", "crypto fund", "crypto token", "DeFi",
]

# ─── SEC Form Types to Search ────────────────────────────────────────────
FORM_TYPES = ["S-1", "S-1/A", "N-1A", "485APOS", "485BPOS", "10-K", "10-Q", "8-K", "D"]

# Forms that represent ETF/Fund offerings (Tier 1)
ETF_FUND_FORMS = {"485APOS", "485BPOS", "N-1A", "S-1", "D"}

# Human-readable form type descriptions
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

# ─── AI Summarization (Optional) ─────────────────────────────────────────
# Set your Anthropic API key here or as an environment variable.
# If not set, the app uses extractive summarization (no API calls).
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# Model to use for AI summaries (only used if API key is set)
SUMMARY_MODEL = "claude-sonnet-4-5-20241022"
SUMMARY_MAX_TOKENS = 300

# ─── Flask ────────────────────────────────────────────────────────────────
FLASK_HOST = "127.0.0.1"
FLASK_PORT = 5000
FLASK_DEBUG = True
SECRET_KEY = os.environ.get("SECRET_KEY", "crypto-tracker-dev-key-change-in-prod")

# ─── Export ───────────────────────────────────────────────────────────────
EXPORT_DIR = os.path.join(DATA_DIR, "exports")
