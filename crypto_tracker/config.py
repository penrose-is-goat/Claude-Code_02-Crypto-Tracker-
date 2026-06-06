"""
Configuration for Crypto SEC Filing Tracker v1.1.32.

Changes from v1.1.31:
- Bypass edgartools.search_filings (PoolTimeout hangs forever)
- Direct EFTS HTTP calls via requests with 15s timeout, 1 retry
- Progress callback during search phase (UI no longer stuck on "Starting")
- Fail-fast: abort EFTS search after 3 consecutive failures, fall through to CIK
"""
import os
from datetime import datetime

# ─── Version ──────────────────────────────────────────────────────────────
VERSION = "1.1.32"
VERSION_NAME = "Batch 1 — SEC Filing Tracker (EFTS fix + UI updates)"

# ─── Paths ───────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Shared DB lives OUTSIDE the version directory so v1.1.30 inherits
# v1.1.29's 700 filings instantly without re-processing.
_SHARED_DATA = os.path.join(os.path.expanduser("~"), ".crypto_tracker_data")
DATA_DIR = _SHARED_DATA
DB_PATH = os.path.join(_SHARED_DATA, "crypto_filings.db")
EXPORT_DIR = os.path.join(_SHARED_DATA, "exports")

# Cache goes to ~/.crypto_tracker_cache (short path, avoids Windows MAX_PATH)
EDGAR_CACHE_DIR = os.path.join(os.path.expanduser("~"), ".crypto_tracker_cache")

# ─── SEC EDGAR Identity (required by SEC fair-access policy) ─────────────
SEC_IDENTITY = "PenroseHayek crypto.hedger23@gmail.com"
SEC_HEADERS = {
    "User-Agent": SEC_IDENTITY,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ─── edgartools local cache ──────────────────────────────────────────────
# 5-10x faster re-runs. Cache lives at ~/.crypto_tracker_cache to keep
# the path short and avoid Windows' 260-char MAX_PATH limit.
USE_EDGAR_LOCAL_CACHE = True

# ─── Scraping Parameters ─────────────────────────────────────────────────
START_DATE = "2019-01-01"
END_DATE = datetime.now().strftime("%Y-%m-%d")
IO_THREADS = 8              # Network fetch concurrency (SEC fair-access: 10/s max)
CPU_WORKERS = 2             # BS4/extraction concurrency (GIL-bound — more threads = slower)
MAX_PER_KEYWORD = 25        # Max filings per keyword per form type
HTTP_TIMEOUT = 30
REQUEST_DELAY = 0.10
FILING_TIMEOUT = 120        # Per-filing timeout in seconds

# ─── EFTS direct-HTTP search (bypasses edgartools' PoolTimeout) ──────────
EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
EFTS_TIMEOUT = 15           # Per-search timeout, much shorter than 60s default
EFTS_RETRIES = 1            # Try once, retry once; bail fast on failure
EFTS_MAX_CONSECUTIVE_FAILURES = 3  # Abort EFTS phase after N straight failures

# ─── Text-cleaning cache ────────────────────────────────────────────────
# sha256(raw_html) → cleaned text. Skips BS4 entirely on reprocessing.
TEXT_CACHE_DIR = os.path.join(_SHARED_DATA, "text_cache")

# ─── Search Keywords ─────────────────────────────────────────────────────
KEYWORDS = [
    "cryptocurrency", "blockchain", "digital asset", "bitcoin", "ethereum",
    "XRP", "stablecoin", "crypto ETF", "crypto fund", "crypto token", "DeFi",
]

# ─── Known Crypto Operating Companies (CIK-based search) ─────────────────
# EFTS keyword search alone is biased toward fund prospectuses that mention
# crypto by name in marketing copy. Operating companies (exchanges, miners,
# treasury holders, payment processors) often disclose crypto exposure in
# risk factors / footnotes that don't keyword-match well. We pull their
# filings directly by CIK to guarantee coverage.
#
# Format: CIK -> (Company name, ticker, category)
# Categories: exchange, miner, treasury, payments, broker, other
CRYPTO_COMPANIES = {
    # Exchanges & brokers
    "1679788": ("Coinbase Global Inc",          "COIN",  "exchange"),
    "1828318": ("Bakkt Holdings Inc",           "BKKT",  "exchange"),
    "1722438": ("Galaxy Digital Holdings",      "GLXY",  "broker"),
    "1783879": ("Robinhood Markets Inc",        "HOOD",  "broker"),
    "1604778": ("Voyager Digital Ltd",          "VYGVQ", "broker"),

    # Miners
    "1507605": ("Marathon Digital Holdings",    "MARA",  "miner"),
    "1167419": ("Riot Platforms Inc",           "RIOT",  "miner"),
    "1854963": ("CleanSpark Inc",               "CLSK",  "miner"),
    "1591956": ("Hut 8 Mining Corp",            "HUT",   "miner"),
    "1838359": ("Bitfarms Ltd",                 "BITF",  "miner"),
    "1845815": ("TeraWulf Inc",                 "WULF",  "miner"),
    "1907982": ("Iris Energy Ltd",              "IREN",  "miner"),
    "1825570": ("Cipher Mining Inc",            "CIFR",  "miner"),
    "1768259": ("Hive Digital Technologies",    "HIVE",  "miner"),
    "1610520": ("Canaan Inc",                   "CAN",   "miner"),
    "1576580": ("Ebang International Holdings", "EBON",  "miner"),
    "1709164": ("Greenidge Generation Holdings","GREE",  "miner"),
    "1819395": ("Stronghold Digital Mining",    "SDIG",  "miner"),
    "1744494": ("Argo Blockchain plc",          "ARBK",  "miner"),
    "1761312": ("Bit Digital Inc",              "BTBT",  "miner"),
    "1859285": ("Core Scientific Inc",          "CORZ",  "miner"),
    "1873722": ("Soluna Holdings Inc",          "SLNH",  "miner"),
    "1861974": ("Applied Digital Corporation",  "APLD",  "miner"),

    # Treasury holders
    "1050446": ("MicroStrategy Inc",            "MSTR",  "treasury"),
    "1318605": ("Tesla Inc",                    "TSLA",  "treasury"),
    "1665300": ("BTCS Inc",                     "BTCS",  "treasury"),

    # Payments / fintech with crypto exposure
    "1512673": ("Block Inc",                    "SQ",    "payments"),
    "1633917": ("PayPal Holdings Inc",          "PYPL",  "payments"),
    "1716583": ("Mogo Inc",                     "MOGO",  "payments"),

    # Other (banking partners, e-commerce, etc.)
    "1718512": ("Silvergate Capital Corp",      "SI",    "other"),
    "1411690": ("Overstock.com Inc",            "OSTK",  "other"),
}

# ─── Company Details Lookup (purpose + holdings, instant) ────────────────
# For known companies, no extraction needed — these are always accurate.
# Format: CIK -> (purpose, top_holdings)
COMPANY_DETAILS = {
    "1679788": ("Cryptocurrency exchange and financial services", "BTC, ETH, USDT, SOL, DOGE"),
    "1828318": ("Digital asset marketplace and custody platform", "BTC, ETH"),
    "1722438": ("Digital asset financial services and investment management", "BTC, ETH, SOL"),
    "1783879": ("Commission-free trading platform with crypto trading", "BTC, ETH, DOGE"),
    "1604778": ("Cryptocurrency brokerage and lending platform", "BTC, ETH, USDC"),
    "1507605": ("Bitcoin mining and digital asset operations", "BTC"),
    "1167419": ("Bitcoin mining and data center hosting", "BTC"),
    "1854963": ("Bitcoin mining using low-carbon energy", "BTC"),
    "1591956": ("Bitcoin mining and high-performance computing", "BTC"),
    "1838359": ("Bitcoin mining across multiple global facilities", "BTC"),
    "1845815": ("Bitcoin mining powered by nuclear energy", "BTC"),
    "1907982": ("Bitcoin mining powered by renewable energy", "BTC"),
    "1825570": ("Bitcoin mining and data center development", "BTC"),
    "1768259": ("Bitcoin mining and HPC infrastructure", "BTC"),
    "1610520": ("Design and manufacture of Bitcoin mining ASICs", "BTC"),
    "1576580": ("Bitcoin mining hardware manufacturer", "BTC"),
    "1709164": ("Bitcoin mining and power generation", "BTC"),
    "1819395": ("Bitcoin mining using waste coal energy", "BTC"),
    "1744494": ("Bitcoin mining and data center operations", "BTC"),
    "1761312": ("Bitcoin mining and digital asset staking", "BTC, ETH"),
    "1859285": ("Bitcoin mining and AI/HPC data center operations", "BTC"),
    "1873722": ("Green data center hosting and Bitcoin mining", "BTC"),
    "1861974": ("High-performance computing and Bitcoin mining hosting", "BTC"),
    "1050446": ("Enterprise analytics; Bitcoin treasury reserve strategy", "BTC"),
    "1318605": ("Electric vehicle manufacturer with Bitcoin treasury", "BTC"),
    "1665300": ("Blockchain technology and digital asset treasury", "BTC, ETH"),
    "1512673": ("Financial services and payments with Bitcoin", "BTC"),
    "1633917": ("Digital payments with crypto buying, selling, holding", "BTC, ETH, LTC, BCH"),
    "1716583": ("Digital financial services with Bitcoin rewards", "BTC"),
    "1718512": ("Digital currency banking infrastructure (wound down)", "BTC, ETH"),
    "1411690": ("E-commerce with blockchain investment (tZERO)", "BTC"),
}

# Forms to pull for each operating company
COMPANY_FORM_TYPES = ["10-K", "10-Q", "8-K", "S-1", "S-1/A"]

# How many recent filings per (company, form) pair
COMPANY_FILINGS_PER_FORM = 20

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
