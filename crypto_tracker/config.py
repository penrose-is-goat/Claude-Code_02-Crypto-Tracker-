"""
Configuration for Crypto SEC Filing Tracker v1.1.37.

v1.1.37 changes:
- Raw filing source is now stored durably IN the database (gzip-compressed),
  not only in a deletable filesystem cache. The DB is the single source of
  truth: extraction can be re-derived offline with zero SEC traffic.
- Per-filing extraction_version stamps let an update regenerate only the
  filings a newer extractor would actually change, instead of all-or-nothing.
- Filing overview + "what's new" (diffed against the prior filing of the
  same company/form) alongside the risk-factor summary.
- FRED-inspired light UI replacing the dark retro theme.

v1.1.36 changes:
- edgartools fallback is now bounded: low concurrency gate, its own rate
  limit, longer HTTP/pool timeouts, and retry on transient timeouts.
  Fixes hour-long runs full of PoolTimeout('') errors when many worker
  threads piled onto edgartools' shared throttled HTTP client.
- Empty fetched text (0 chars) is a retryable failure, not a terminal
  skip — transient network failures no longer permanently drop filings.
- Missing doc URLs are resolved via the direct archive index before
  falling back to edgartools, keeping the hot path on plain requests.

v1.1.35 changes:
- Faster direct SEC submissions discovery for known CIKs
- Exact risk-section extraction with source hashes/offset provenance
- Version-aware text-cleaning cache so extractor fixes are not hidden by
  stale cleaned HTML from older releases

v1.1.34 changes:
- Per-filing contextual metadata (Type/Purpose/Holdings now derived from
  entity classification + form type instead of static per-company strings)
- Clear "Up to date" messaging when all filings already exist in DB
  (previously showed misleading "found 2000, saved 0")
- entity_type column added to schema (e.g. "Bitcoin Miner — Annual report")
- Backfill regenerates metadata for all existing filings using new templates

Carried forward from v1.1.33/v1.1.33.1:
- Parallel CIK search, EFTS response cache
- Expanded COMPANY_DETAILS (54 entities)
- Export buttons, init_db lock-race fix
"""
import os
from datetime import datetime

# ─── Version ──────────────────────────────────────────────────────────────
VERSION = "1.1.37"
VERSION_NAME = "Batch 1 — SEC Filing Tracker (database-derived, FRED UI)"

# ─── Paths ───────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Shared DB lives OUTSIDE the version directory so v1.1.30 inherits
# v1.1.29's 700 filings instantly without re-processing.
_SHARED_DATA = os.environ.get(
    "CRYPTO_TRACKER_DATA_DIR",
    os.path.join(os.path.expanduser("~"), ".crypto_tracker_data"),
)
DATA_DIR = _SHARED_DATA
DB_PATH = os.path.join(_SHARED_DATA, "crypto_filings.db")
EXPORT_DIR = os.path.join(_SHARED_DATA, "exports")

# Cache goes to ~/.crypto_tracker_cache (short path, avoids Windows MAX_PATH)
EDGAR_CACHE_DIR = os.environ.get(
    "CRYPTO_TRACKER_CACHE_DIR",
    os.path.join(os.path.expanduser("~"), ".crypto_tracker_cache"),
)
RAW_DOC_CACHE_DIR = os.path.join(EDGAR_CACHE_DIR, "sec_archive_docs")

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
IO_THREADS = 12             # Network/extraction concurrency; SEC HTTP is paced below
CPU_WORKERS = 2             # BS4/extraction concurrency (GIL-bound — more threads = slower)
MAX_PER_KEYWORD = 25        # Max filings per keyword per form type
HTTP_TIMEOUT = 30
REQUEST_DELAY = 0.10
FILING_TIMEOUT = 120        # Per-filing timeout in seconds
DB_BATCH_SIZE = 100         # Commit filing/skip writes in bounded batches

# ─── SEC fair-access pacing ──────────────────────────────────────────────
# SEC allows up to 10 requests/sec, but bursts from parallel workers were
# causing 429s and then silently dropping filings. Pace all SEC HTTP calls
# through one gate and retry 429s with a cooldown.
SEC_TARGET_REQUESTS_PER_SECOND = 8.0
SEC_REQUEST_MIN_INTERVAL = 1.0 / SEC_TARGET_REQUESTS_PER_SECOND
SEC_REQUEST_MAX_INTERVAL = 1.0
SEC_MAX_RETRIES = 4
SEC_BACKOFF_BASE = 2.0
SEC_BACKOFF_MAX = 60.0
SEC_429_COOLDOWN = 10.0

# ─── edgartools fallback guard (v1.1.36) ─────────────────────────────────
# edgartools is only a fallback (no direct doc URL / attachments needed),
# but its shared httpx client has its own throttle and a short default
# pool timeout. Unbounded, IO_THREADS workers pile onto it and every call
# dies with PoolTimeout(''). Bound its concurrency, slow its rate so the
# combined direct+edgartools request rate stays under SEC's 10/s, and
# retry transient timeouts instead of dropping the filing.
EDGARTOOLS_MAX_CONCURRENCY = 2   # Max worker threads inside edgartools at once
EDGARTOOLS_RATE_LIMIT_PER_SEC = 2  # edgartools' own limiter (EDGAR_RATE_LIMIT_PER_SEC)
EDGARTOOLS_RETRIES = 2           # Extra attempts on Timeout/PoolTimeout errors
EDGARTOOLS_RETRY_DELAY = 3.0     # Seconds between fallback retries

# ─── EFTS direct-HTTP search (bypasses edgartools' PoolTimeout) ──────────
EFTS_URL = "https://efts.sec.gov/LATEST/search-index"
EFTS_TIMEOUT = 15           # Per-search timeout, much shorter than 60s default
EFTS_RETRIES = 1            # Try once, retry once; bail fast on failure
EFTS_MAX_CONSECUTIVE_FAILURES = 3  # Abort EFTS phase after N straight failures
EFTS_PAGE_SIZE = 100
EFTS_MAX_PAGES_PER_QUERY = 1  # Complete first page per query; fixed-point tested

# ─── EFTS response cache (v1.1.33: skip re-querying same searches) ───────
EFTS_CACHE_DIR = os.path.join(_SHARED_DATA, "efts_cache")
EFTS_CACHE_TTL = 3600       # 1 hour — fresh enough for "click Update again"

# ─── SEC submissions JSON cache (one pull per known CIK) ────────────────
SUBMISSIONS_CACHE_TTL = 21600     # 6 hours: repeated Update clicks should not re-pull every CIK
SUBMISSIONS_TIMEOUT = 15
SEC_TICKER_MAP_CACHE_TTL = 604800  # 7 days: catches stale hardcoded ticker/CIK pairs

# ─── Parallel CIK search (v1.1.33: was sequential, 30 cos × 5 forms) ─────
CIK_SEARCH_THREADS = 6      # Sub-IO_THREADS to leave room for processing

# ─── Text-cleaning cache ────────────────────────────────────────────────
# sha256(raw_html) → cleaned text. Skips BS4 entirely on reprocessing.
TEXT_CACHE_DIR = os.path.join(_SHARED_DATA, "text_cache")
TEXT_CACHE_VERSION = "htmlclean-v3-fast-large-html"
FAST_HTML_CLEAN_BYTES = 750_000

# Bump when discovery/extraction skip decisions need to be re-audited.
PROCESSOR_VERSION = f"{VERSION}-{TEXT_CACHE_VERSION}-skipstate-v2-cik-validated"

# ─── Durable raw source + offline regeneration (v1.1.37) ─────────────────
# Raw filing HTML is stored gzip-compressed in the DB so re-extraction never
# needs SEC. RAW_DOC_CACHE_DIR remains a fast read-through cache, but it is
# now expendable: wiping it costs speed, not data.
STORE_RAW_SOURCE_IN_DB = True
RAW_SOURCE_GZIP_LEVEL = 6         # ~10-15% of original size for SEC HTML
RAW_SOURCE_MAX_BYTES = 25_000_000  # Skip absurd outliers rather than bloat the DB

# EXTRACTION_VERSION stamps each stored section. An update run regenerates
# only filings whose stamp is behind — from local raw source, no network.
# Bump this (not VERSION) whenever extraction logic changes what gets stored.
EXTRACTION_VERSION = f"{VERSION}-exact-v1"

# Cap per regeneration pass so a version bump can't turn one click into an
# unbounded job. 0 = no cap.
REGENERATE_BATCH_LIMIT = 0
# Regeneration is local-only by default: a filing with no stored raw source
# is left alone rather than silently re-downloaded from SEC.
REGENERATE_ALLOW_NETWORK = False

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
    "1820302": ("Bakkt Holdings Inc",           "BKKT",  "exchange"),
    "1859392": ("Galaxy Digital Holdings",      "GLXY",  "broker"),
    "1783879": ("Robinhood Markets Inc",        "HOOD",  "broker"),

    # Miners
    "1507605": ("Marathon Digital Holdings",    "MARA",  "miner"),
    "1167419": ("Riot Platforms Inc",           "RIOT",  "miner"),
    "827876":  ("CleanSpark Inc",               "CLSK",  "miner"),
    "1964789": ("Hut 8 Corp",                   "HUT",   "miner"),
    "1083301": ("TeraWulf Inc",                 "WULF",  "miner"),
    "1878848": ("Iris Energy Ltd",              "IREN",  "miner"),
    "1819989": ("Cipher Digital Inc",           "CIFR",  "miner"),
    "1720424": ("HIVE Digital Technologies",    "HIVE",  "miner"),
    "1610520": ("Canaan Inc",                   "CAN",   "miner"),
    "1799290": ("Ebang International Holdings", "EBON",  "miner"),
    "1844971": ("Greenidge Generation Holdings","GREE",  "miner"),
    "1841675": ("Argo Blockchain plc",          "ARBK",  "miner"),
    "1710350": ("Bit Digital Inc",              "BTBT",  "miner"),
    "1839341": ("Core Scientific Inc",          "CORZ",  "miner"),
    "64463":   ("Soluna Holdings Inc",          "SLNH",  "miner"),
    "1144879": ("Applied Digital Corporation",  "APLD",  "miner"),
    "1591956": ("Sphere 3D Corp",               "ANY",   "miner"),

    # Treasury holders
    "1050446": ("MicroStrategy Inc",            "MSTR",  "treasury"),
    "1318605": ("Tesla Inc",                    "TSLA",  "treasury"),
    "1436229": ("BTCS Inc",                     "BTCS",  "treasury"),

    # Payments / fintech with crypto exposure
    "1512673": ("Block Inc",                    "XYZ",   "payments"),
    "1633917": ("PayPal Holdings Inc",          "PYPL",  "payments"),

    # Other (banking partners, e-commerce, etc.)
    "1312109": ("Silvergate Capital Corp",      "",      "other"),

    # ─── Spot Bitcoin ETF issuers (v1.1.33 additions) ────────────────────
    "1980994": ("iShares Bitcoin Trust",          "IBIT",  "etf_issuer"),
    "1852317": ("Fidelity Wise Origin Bitcoin",   "FBTC",  "etf_issuer"),
    "1869699": ("ARK 21Shares Bitcoin ETF",       "ARKB",  "etf_issuer"),
    "1763415": ("Bitwise Bitcoin ETF",            "BITB",  "etf_issuer"),
    "1838028": ("VanEck Bitcoin Trust",           "HODL",  "etf_issuer"),
    "1855781": ("Invesco Galaxy Bitcoin ETF",     "BTCO",  "etf_issuer"),
    "1850391": ("WisdomTree Bitcoin Fund",        "BTCW",  "etf_issuer"),
    "1841175": ("CoinShares Valkyrie Bitcoin",    "BRRR",  "etf_issuer"),
    "1992870": ("Franklin Bitcoin ETF",           "EZBC",  "etf_issuer"),
    "1588489": ("Grayscale Bitcoin Trust",        "GBTC",  "etf_issuer"),
    "1985840": ("Hashdex Bitcoin ETF",            "DEFI",  "etf_issuer"),

    # ─── Spot Ethereum ETF issuers (v1.1.33 additions) ───────────────────
    "2000638": ("iShares Ethereum Trust",         "ETHA",  "etf_issuer"),
    "2000046": ("Fidelity Ethereum Fund",         "FETH",  "etf_issuer"),
    "1725210": ("Grayscale Ethereum Trust",       "ETHE",  "etf_issuer"),
    "2020455": ("Grayscale Ethereum Mini Trust",  "ETH",   "etf_issuer"),
    "2013744": ("Bitwise Ethereum ETF",           "ETHW",  "etf_issuer"),
    "2011535": ("Franklin Ethereum ETF",          "EZET",  "etf_issuer"),
    "1860788": ("VanEck Ethereum ETF",            "ETHV",  "etf_issuer"),

    # ─── Treasury / payments / mining (v1.1.33 additions) ────────────────
    "1876042": ("Circle Internet Group",          "CRCL",  "payments"),
    "1829311": ("BitMine Immersion Technologies", "BMNR",  "treasury"),
    "1846839": ("SOL Strategies Inc",             "STKE",  "treasury"),
    "1899123": ("Bitdeer Technologies Group",     "BTDR",  "miner"),
}

# ─── Company Details Lookup (purpose + holdings, instant) ────────────────
# For known companies, no extraction needed — these are always accurate.
# Format: CIK -> (purpose, top_holdings)
COMPANY_DETAILS = {
    "1679788": ("Cryptocurrency exchange and financial services", "BTC, ETH, USDT, SOL, DOGE"),
    "1820302": ("Digital asset marketplace and custody platform", "BTC, ETH"),
    "1859392": ("Digital asset financial services and investment management", "BTC, ETH, SOL"),
    "1783879": ("Commission-free trading platform with crypto trading", "BTC, ETH, DOGE"),
    "1507605": ("Bitcoin mining and digital asset operations", "BTC"),
    "1167419": ("Bitcoin mining and data center hosting", "BTC"),
    "827876":  ("Bitcoin mining using low-carbon energy", "BTC"),
    "1964789": ("Bitcoin mining and high-performance computing", "BTC"),
    "1083301": ("Bitcoin mining powered by nuclear energy", "BTC"),
    "1878848": ("Bitcoin mining powered by renewable energy", "BTC"),
    "1819989": ("Bitcoin mining and data center development", "BTC"),
    "1720424": ("Bitcoin mining and HPC infrastructure", "BTC"),
    "1610520": ("Design and manufacture of Bitcoin mining ASICs", "BTC"),
    "1799290": ("Bitcoin mining hardware manufacturer", "BTC"),
    "1844971": ("Bitcoin mining and power generation", "BTC"),
    "1841675": ("Bitcoin mining and data center operations", "BTC"),
    "1710350": ("Bitcoin mining and digital asset staking", "BTC, ETH"),
    "1839341": ("Bitcoin mining and AI/HPC data center operations", "BTC"),
    "64463":   ("Green data center hosting and Bitcoin mining", "BTC"),
    "1144879": ("High-performance computing and Bitcoin mining hosting", "BTC"),
    "1591956": ("Bitcoin mining and blockchain infrastructure", "BTC"),
    "1050446": ("Enterprise analytics; Bitcoin treasury reserve strategy", "BTC"),
    "1318605": ("Electric vehicle manufacturer with Bitcoin treasury", "BTC"),
    "1436229": ("Blockchain technology and digital asset treasury", "BTC, ETH"),
    "1512673": ("Financial services and payments with Bitcoin", "BTC"),
    "1633917": ("Digital payments with crypto buying, selling, holding", "BTC, ETH, LTC, BCH"),
    "1312109": ("Digital currency banking infrastructure (wound down)", "BTC, ETH"),

    # ─── Spot Bitcoin ETFs (v1.1.33) ─────────────────────────────────────
    "1980994": ("Spot Bitcoin ETF issued by BlackRock iShares",          "BTC"),
    "1852317": ("Spot Bitcoin ETF issued by Fidelity",                   "BTC"),
    "1869699": ("Spot Bitcoin ETF from ARK Invest and 21Shares",         "BTC"),
    "1763415": ("Spot Bitcoin ETF issued by Bitwise",                    "BTC"),
    "1838028": ("Spot Bitcoin ETF issued by VanEck",                     "BTC"),
    "1855781": ("Spot Bitcoin ETF from Invesco and Galaxy",              "BTC"),
    "1850391": ("Spot Bitcoin ETF issued by WisdomTree",                 "BTC"),
    "1841175": ("Spot Bitcoin ETF from CoinShares (formerly Valkyrie)",  "BTC"),
    "1992870": ("Spot Bitcoin ETF issued by Franklin Templeton",         "BTC"),
    "1588489": ("Spot Bitcoin ETF issued by Grayscale",                  "BTC"),
    "1985840": ("Spot Bitcoin ETF issued by Hashdex",                    "BTC"),

    # ─── Spot Ethereum ETFs (v1.1.33) ────────────────────────────────────
    "2000638": ("Spot Ethereum ETF issued by BlackRock iShares",         "ETH"),
    "2000046": ("Spot Ethereum ETF issued by Fidelity",                  "ETH"),
    "1725210": ("Spot Ethereum ETF issued by Grayscale",                 "ETH"),
    "2020455": ("Spot Ethereum ETF (mini) issued by Grayscale",          "ETH"),
    "2013744": ("Spot Ethereum ETF issued by Bitwise",                   "ETH"),
    "2011535": ("Spot Ethereum ETF issued by Franklin Templeton",        "ETH"),
    "1860788": ("Spot Ethereum ETF issued by VanEck",                    "ETH"),

    # ─── Treasury / payments / mining (v1.1.33) ──────────────────────────
    "1876042": ("Issuer of USDC stablecoin and crypto payments platform","USDC"),
    "1829311": ("Public company with Ethereum treasury strategy",        "ETH"),
    "1846839": ("Solana validator and treasury company",                 "SOL"),
    "1899123": ("Bitcoin mining and AI cloud infrastructure",            "BTC"),
}

# Forms to pull for each operating company
COMPANY_FORM_TYPES = ["10-K", "10-Q", "8-K", "S-1", "S-1/A"]

# How many recent filings per (company, form) pair
COMPANY_FILINGS_PER_FORM = 20

# ─── SEC Form Types to Search ────────────────────────────────────────────
FORM_TYPES = ["S-1", "S-1/A", "N-1A", "485APOS", "485BPOS", "10-K", "10-Q", "8-K", "D"]

# Default optimized update scope for v1.1.35: risk-bearing forms only.
# Form D is low-yield for exact risk sections; 8-K is opt-in via event_risk/all.
RISK_DEFAULT_FORM_TYPES = ["S-1", "S-1/A", "N-1A", "485APOS", "485BPOS", "10-K", "10-Q"]
CORE_FORM_TYPES = ["10-K", "10-Q", "N-1A", "485APOS", "485BPOS", "S-1", "S-1/A"]
EVENT_RISK_FORM_TYPES = RISK_DEFAULT_FORM_TYPES + ["8-K"]
ALL_FORM_TYPES = FORM_TYPES

# EFTS is high-yield for fund/prospectus/offering forms. For 10-K/10-Q, the
# default run uses known-CIK submissions instead to avoid thousands of broad,
# low-confidence keyword hits that cannot finish under the hour target.
# EFTS is high-yield for fund/prospectus/offering forms. 10-K/10-Q broad
# keyword search remains covered by known-CIK submissions in the default run.
RISK_DEFAULT_EFTS_FORM_TYPES = ["S-1", "S-1/A", "N-1A", "485APOS", "485BPOS"]
CORE_EFTS_FORM_TYPES = ["N-1A", "485APOS", "485BPOS", "S-1", "S-1/A"]
EVENT_RISK_EFTS_FORM_TYPES = RISK_DEFAULT_EFTS_FORM_TYPES + ["8-K"]
ALL_EFTS_FORM_TYPES = FORM_TYPES

RISK_DEFAULT_COMPANY_FORM_TYPES = ["10-K", "10-Q", "S-1", "S-1/A"]
CORE_COMPANY_FORM_TYPES = ["10-K", "10-Q"]
EVENT_RISK_COMPANY_FORM_TYPES = RISK_DEFAULT_COMPANY_FORM_TYPES + ["8-K"]
ALL_COMPANY_FORM_TYPES = COMPANY_FORM_TYPES

SCRAPE_MODES = {
    "exact_only", "analysis_only", "low_confidence_only", "all_cached",
    "backfill_source",   # one-time: populate durable raw source for old filings
    "regenerate",        # offline rebuild of filings behind EXTRACTION_VERSION
}
SCRAPE_SCOPES = {"risk_default", "core", "event_risk", "all"}
DEFAULT_SCRAPE_MODE = "exact_only"
DEFAULT_SCRAPE_SCOPE = "risk_default"

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
