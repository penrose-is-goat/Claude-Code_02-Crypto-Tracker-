"""
SEC EDGAR Scraper v1.1.36.

v1.1.36 changes:
- edgartools fallback bounded by a concurrency gate + its own low rate
  limit + longer pool timeout; transient timeouts retried (PoolTimeout fix)
- Empty fetched text is a retryable failure, never a terminal skip
- Missing doc URLs resolved via direct archive index before edgartools

v1.1.35 changes:
- Prefers direct SEC submissions/archive HTTP for faster discovery and fetch
- Stores exact source-backed risk text before analysis
- Skips summary work when the exact extracted text hash is unchanged
- Keeps edgartools optional as a fallback for objects/attachments

v1.1.34 changes:
- Per-filing contextual metadata (Type/Purpose/Holdings derived from entity
  classification + form type instead of static per-company strings)
- Fixed misleading "found 2000, saved 0" messaging — now clearly reports
  when all found filings already exist in DB
- Clear EFTS cache on "Update Filings" so stale cached results don't mask
  new SEC submissions

v1.1.33: Parallel CIK search, EFTS response cache, backfill purpose/holdings
v1.1.32: Direct EFTS HTTP (bypass edgartools PoolTimeout), live search progress
v1.1.31: purpose/holdings fields, skip obj() for prospectuses
v1.1.30: lxml parser, text-cleaning cache, shared DB
"""
import hashlib
import json
import os
import re
import shutil
import time
import threading
import traceback
import unicodedata
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from . import config
from . import database as db

if config.USE_EDGAR_LOCAL_CACHE:
    os.environ.setdefault("EDGAR_USE_LOCAL_DATA", "True")
    os.environ.setdefault("EDGAR_LOCAL_DATA_DIR", config.EDGAR_CACHE_DIR)
    os.environ.setdefault("EDGAR_DATA_DIR", config.EDGAR_CACHE_DIR)

# edgartools runs its own client-side throttle (default 9 req/s). Our direct
# requests already pace at SEC_TARGET_REQUESTS_PER_SECOND, so left at its
# default the combined rate can exceed SEC's 10/s and trigger 429 storms.
# Must be set before `import edgar` — the limiter is built at import time.
os.environ.setdefault(
    "EDGAR_RATE_LIMIT_PER_SEC",
    str(int(getattr(config, "EDGARTOOLS_RATE_LIMIT_PER_SEC", 2))),
)

try:
    from edgar import set_identity, get_by_accession_number
    HAS_EDGAR = True
except Exception as e:
    set_identity = None
    get_by_accession_number = None
    HAS_EDGAR = False
    _EDGAR_IMPORT_ERROR = e
else:
    _EDGAR_IMPORT_ERROR = None

# Company API is the canonical way to pull a single issuer's filings by CIK.
try:
    from edgar import Company
    HAS_COMPANY = True
except Exception:
    Company = None
    HAS_COMPANY = False

from .extractor import (
    fetch_filing_text,
    extract_all_candidates,
    extract_fund_documents,
    get_filing_url,
    CRYPTO_RE,
)
from .summarizer import build_summary, build_overview, build_whats_new


# ─── One-time setup ──────────────────────────────────────────────────────
if HAS_EDGAR:
    set_identity(config.SEC_IDENTITY)
    # edgartools' httpx client defaults to a 5s pool timeout. Under load,
    # waiting for its throttled connection pool routinely exceeds that and
    # every call dies with PoolTimeout(''). Raise all timeouts to match ours.
    try:
        from edgar.httpclient import configure_http
        configure_http(timeout=float(getattr(config, "HTTP_TIMEOUT", 30)))
    except Exception:
        pass  # older edgartools without configure_http — gate still protects us

# Enable edgartools local storage cache (v1.1.27 speed fix).
# All subsequent Filing.text()/.html()/.attachments calls hit disk after
# the first fetch. Returning to these filings costs ~0 instead of ~5 seconds.
if HAS_EDGAR and config.USE_EDGAR_LOCAL_CACHE:
    try:
        os.environ.setdefault("EDGAR_USE_LOCAL_DATA", "True")
        os.environ.setdefault("EDGAR_LOCAL_DATA_DIR", config.EDGAR_CACHE_DIR)
        # Newer edgartools also supports direct API — best-effort
        from edgar import use_local_storage
        use_local_storage(True, local_path=config.EDGAR_CACHE_DIR)
    except Exception:
        pass  # env-var fallback still works for older versions
elif not HAS_EDGAR:
    _msg = str(_EDGAR_IMPORT_ERROR).strip() if _EDGAR_IMPORT_ERROR else ""
    _label = type(_EDGAR_IMPORT_ERROR).__name__ if _EDGAR_IMPORT_ERROR else "ImportError"
    print(f"  edgartools unavailable: using direct SEC HTTP where possible ({_label}: {_msg[:120]})")

_lock = threading.RLock()

# Gate around ALL edgartools network calls. edgartools shares one throttled
# httpx client across threads; letting IO_THREADS workers queue on it is what
# produced the PoolTimeout('') storms and 0.1 filings/sec in v1.1.34 runs.
_edgar_gate = threading.BoundedSemaphore(
    max(1, int(getattr(config, "EDGARTOOLS_MAX_CONCURRENCY", 2)))
)


def _is_transient_net_error(e: Exception) -> bool:
    name = type(e).__name__
    return "Timeout" in name or name in (
        "ConnectError", "ReadError", "RemoteProtocolError", "PoolTimeout",
    )


def _edgar_call(fn, *args, **kwargs):
    """Run an edgartools call under the concurrency gate, retrying
    transient timeouts (PoolTimeout/ReadTimeout/...) before giving up."""
    retries = max(0, int(getattr(config, "EDGARTOOLS_RETRIES", 2)))
    delay = float(getattr(config, "EDGARTOOLS_RETRY_DELAY", 3.0))
    last = None
    for attempt in range(retries + 1):
        try:
            with _edgar_gate:
                return fn(*args, **kwargs)
        except Exception as e:
            last = e
            if not _is_transient_net_error(e) or attempt >= retries:
                raise
            time.sleep(delay * (attempt + 1))
    raise last


def _candidate_sort_key(c):
    method = c.get("method", "")
    if method.startswith("exact_"):
        start = c.get("start_offset")
        return (0, start is None, start or 0, -c.get("confidence", 0))
    return (1, -c.get("confidence", 0), c.get("start_offset") or 0)


def _clean_text(text):
    if not text:
        return text
    normalized = unicodedata.normalize("NFKD", str(text))
    cleaned = "".join(
        c for c in normalized if ord(c) < 128 or c in ['"', "'", "-"]
    )
    return re.sub(r"\s+", " ", cleaned).strip()


def _detect_crypto_connection(text, root_form):
    if not text:
        return "Crypto-related filing."
    tokens = list(set(t.lower() for t in CRYPTO_RE.findall(text[:5000])))[:5]
    if not tokens:
        return "Crypto-related filing."
    joined = " or ".join(tokens)
    if root_form in config.ETF_FUND_FORMS:
        return f"Proposes {joined} ETF, fund, or security offering."
    elif root_form in ("10-K", "10-Q"):
        return f"Reports {joined} operations with risk disclosures."
    elif root_form == "8-K":
        return f"Discloses {joined} material event or corporate action."
    elif root_form == "D":
        return f"Exempt offering related to {joined}."
    return f"Filing related to {joined}."


def _detect_filing_pdf_url(filing):
    try:
        attachments = filing.attachments
        if attachments and attachments.documents:
            for doc in attachments.documents:
                if hasattr(doc, "url") and doc.url and doc.url.endswith(".pdf"):
                    return doc.url
    except Exception:
        pass
    return ""


# ═══════════════════════════════════════════════════════════════════════════
# SEARCH — one combined OR-query per form (99 calls → 9)
# ═══════════════════════════════════════════════════════════════════════════

def _build_keyword_batches(max_chars: int = 60) -> list:
    """Split keywords into OR-joined batches whose URL-encoded form stays
    well under Windows MAX_PATH (260) when combined with the rest of the
    EFTS URL + cache prefix. Each batch query <= max_chars keeps total
    path comfortably below the limit.
    """
    batches = []
    current = []
    current_len = 0
    for kw in config.KEYWORDS:
        token = f'"{kw}"' if " " in kw else kw
        added = len(token) + (4 if current else 0)  # " OR " between tokens
        if current and current_len + added > max_chars:
            batches.append(" OR ".join(current))
            current = [token]
            current_len = len(token)
        else:
            current.append(token)
            current_len += added
    if current:
        batches.append(" OR ".join(current))
    return batches


def _fmt_exc(e: Exception) -> str:
    """Format an exception so empty-message errors are still diagnosable."""
    name = type(e).__name__
    msg = str(e).strip()
    if msg:
        return f"{name}: {msg[:120]}"
    # Empty message: try repr, then fall back to module-qualified name
    r = repr(e).strip()
    if r and r != f"{name}()":
        return r[:140]
    return name


# Reusable HTTP session for EFTS (connection pooling + correct UA header)
_efts_session = requests.Session()
_efts_session.headers.update({
    "User-Agent": config.SEC_IDENTITY,
    "Accept": "application/json",
})

_sec_session = requests.Session()
_sec_session.headers.update({
    "User-Agent": config.SEC_IDENTITY,
    "Accept": "application/json,text/html,text/plain,*/*",
})

_sec_request_lock = threading.Lock()
_last_sec_request_at = 0.0
_sec_current_interval = float(getattr(config, "SEC_REQUEST_MIN_INTERVAL", 0.25) or 0)
_metrics_lock = threading.Lock()
_active_metrics = None


class SecRateLimitError(RuntimeError):
    """Raised after SEC 429 responses persist through configured retries."""


def _new_run_metrics() -> dict:
    now = datetime.now().isoformat()
    return {
        "started_at": now,
        "finished_at": "",
        "timings": {},
        "counters": {
            "sec_requests": 0,
            "sec_429": 0,
            "efts_cache_hits": 0,
            "efts_cache_misses": 0,
            "submissions_cache_hits": 0,
            "submissions_cache_misses": 0,
            "raw_doc_cache_hits": 0,
            "raw_doc_cache_misses": 0,
            "analysis_cache_hits": 0,
            "analysis_generated": 0,
            "analysis_deferred": 0,
            "analysis_rejected": 0,
            "raw_source_db_hits": 0,
            "raw_source_stored": 0,
            "regenerated": 0,
            "db_writes": 0,
            "skip_state_writes": 0,
            "candidate_universe": 0,
            "already_terminal": 0,
            "duplicate_candidates": 0,
            "scope_filtered": 0,
            "non_risk_amendments": 0,
            "fixed_point_complete": 0,
            "exact_sections_saved": 0,
            "cik_corrections": 0,
            "cik_identity_mismatches": 0,
            "primary_doc_resolutions": 0,
        },
        "skip_reasons": {},
        "failure_reasons": {},
        "scope": getattr(config, "DEFAULT_SCRAPE_SCOPE", "risk_default"),
        "mode": getattr(config, "DEFAULT_SCRAPE_MODE", "exact_only"),
    }


def _set_active_metrics(metrics: dict = None):
    global _active_metrics
    with _metrics_lock:
        _active_metrics = metrics


def _inc_metric(name: str, amount: int = 1):
    with _metrics_lock:
        if _active_metrics is not None:
            _active_metrics["counters"][name] = (
                _active_metrics["counters"].get(name, 0) + amount
            )


def _inc_reason(bucket: str, reason: str):
    reason = reason or "unknown"
    with _metrics_lock:
        if _active_metrics is not None:
            reasons = _active_metrics.setdefault(bucket, {})
            reasons[reason] = reasons.get(reason, 0) + 1


def _benchmark_log_path() -> str:
    return os.path.join(config.DATA_DIR, "update_filings_runs.jsonl")


def _append_run_log(result: dict, metrics: dict):
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        payload = {
            "version": config.VERSION,
            "result": {k: v for k, v in result.items() if k != "metrics"},
            "metrics": metrics,
        }
        with open(_benchmark_log_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, sort_keys=True) + "\n")
    except Exception as e:
        print(f"  Benchmark log write skipped: {_fmt_exc(e)}")


def _benchmark_markdown_path() -> str:
    return os.path.join(os.path.dirname(config.BASE_DIR), "update_filings_benchmark.md")


def _append_benchmark_markdown(result: dict, metrics: dict):
    try:
        path = _benchmark_markdown_path()
        counters = metrics.get("counters", {})
        timings = metrics.get("timings", {})
        now = datetime.now().isoformat(timespec="seconds")
        fixed = "yes" if counters.get("fixed_point_complete") else "no"
        lines = [
            "",
            f"## Benchmark Run - {now}",
            "",
            f"- Version: {config.VERSION}",
            f"- Mode / scope: `{metrics.get('mode')}` / `{metrics.get('scope')}`",
            f"- Duration: {float(result.get('duration_seconds') or 0):.1f}s "
            f"({float(result.get('duration_seconds') or 0) / 60:.1f}min)",
            f"- Candidate universe: {counters.get('candidate_universe', 0)}",
            f"- Already terminal: {counters.get('already_terminal', 0)}",
            f"- Scope-filtered candidates: {counters.get('scope_filtered', 0)}",
            f"- Non-risk amendments filtered: {counters.get('non_risk_amendments', 0)}",
            f"- New found / saved / skipped / failed: "
            f"{result.get('new_found', 0)} / {result.get('saved', 0)} / "
            f"{result.get('skipped', 0)} / {result.get('failed', 0)}",
            f"- Exact sections saved: {counters.get('exact_sections_saved', 0)}",
            f"- Analysis deferred / generated / cache hits: "
            f"{counters.get('analysis_deferred', 0)} / "
            f"{counters.get('analysis_generated', 0)} / "
            f"{counters.get('analysis_cache_hits', 0)}",
            f"- SEC requests / 429s: {counters.get('sec_requests', 0)} / "
            f"{counters.get('sec_429', 0)}",
            f"- CIK corrections / identity skips: {counters.get('cik_corrections', 0)} / "
            f"{counters.get('cik_identity_mismatches', 0)}",
            f"- Primary document resolutions: {counters.get('primary_doc_resolutions', 0)}",
            f"- EFTS cache hits / misses: {counters.get('efts_cache_hits', 0)} / "
            f"{counters.get('efts_cache_misses', 0)}",
            f"- Raw doc cache hits / misses: {counters.get('raw_doc_cache_hits', 0)} / "
            f"{counters.get('raw_doc_cache_misses', 0)}",
            f"- Timings: search {timings.get('search_seconds', 0)}s, "
            f"process {timings.get('process_seconds', 0)}s, "
            f"DB writes {timings.get('db_write_seconds', 0)}s",
            f"- Fixed-point complete: {fixed}",
        ]
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        print(f"  Benchmark markdown write skipped: {_fmt_exc(e)}")


def _complete_run(result: dict, metrics: dict, benchmark: bool = False) -> dict:
    metrics["finished_at"] = datetime.now().isoformat()
    metrics["timings"].setdefault(
        "total_seconds",
        round(float(result.get("duration_seconds") or 0), 3),
    )
    result["metrics"] = metrics
    _append_run_log(result, metrics)
    if benchmark:
        _append_benchmark_markdown(result, metrics)
    _set_active_metrics(None)
    return result


def _retry_after_seconds(response) -> float:
    value = response.headers.get("Retry-After", "") if response is not None else ""
    if not value:
        return 0.0
    try:
        return max(0.0, float(value))
    except ValueError:
        return 0.0


def _pace_sec_request():
    """Serialize SEC HTTP starts so parallel workers do not burst above policy."""
    global _last_sec_request_at
    min_interval = max(0.0, float(_sec_current_interval or 0))
    if min_interval <= 0:
        return
    with _sec_request_lock:
        now = time.monotonic()
        wait = (_last_sec_request_at + min_interval) - now
        if wait > 0:
            time.sleep(wait)
        _last_sec_request_at = time.monotonic()


def _target_sec_interval() -> float:
    target_rps = float(getattr(config, "SEC_TARGET_REQUESTS_PER_SECOND", 0) or 0)
    if target_rps > 0:
        return 1.0 / target_rps
    return float(getattr(config, "SEC_REQUEST_MIN_INTERVAL", 0.25) or 0.25)


def _note_sec_success():
    """Recover gently toward the configured target after successful SEC calls."""
    global _sec_current_interval
    target = _target_sec_interval()
    with _sec_request_lock:
        if _sec_current_interval > target:
            _sec_current_interval = max(target, _sec_current_interval * 0.95)


def _note_sec_429():
    """Back off all workers after a SEC throttle response."""
    global _sec_current_interval
    max_interval = float(getattr(config, "SEC_REQUEST_MAX_INTERVAL", 1.0) or 1.0)
    target = _target_sec_interval()
    with _sec_request_lock:
        _sec_current_interval = min(max_interval, max(target, _sec_current_interval) * 2.0)


def _sec_get(session, url: str, **kwargs):
    """GET a SEC URL with fair-access pacing and 429 retry/backoff."""
    max_retries = int(getattr(config, "SEC_MAX_RETRIES", 4))
    base = float(getattr(config, "SEC_BACKOFF_BASE", 2.0))
    backoff_max = float(getattr(config, "SEC_BACKOFF_MAX", 60.0))
    cooldown = float(getattr(config, "SEC_429_COOLDOWN", 10.0))
    last_exc = None

    for attempt in range(max_retries + 1):
        _pace_sec_request()
        try:
            _inc_metric("sec_requests")
            response = session.get(url, **kwargs)
            if response.status_code == 429:
                _inc_metric("sec_429")
                _note_sec_429()
                retry_after = _retry_after_seconds(response)
                delay = max(retry_after, cooldown, min(backoff_max, base * (2 ** attempt)))
                last_exc = SecRateLimitError(
                    f"SEC 429 after request to {url} (attempt {attempt + 1}/{max_retries + 1})"
                )
                if attempt < max_retries:
                    print(f"    SEC 429 throttled; sleeping {delay:.0f}s before retry")
                    time.sleep(delay)
                    continue
                raise last_exc
            response.raise_for_status()
            _note_sec_success()
            return response
        except SecRateLimitError:
            raise
        except requests.RequestException as e:
            last_exc = e
            if attempt < max_retries:
                delay = min(backoff_max, base * (2 ** attempt))
                time.sleep(delay)
                continue
            raise

    raise last_exc


def _efts_cache_key(query: str, form_type: str, offset: int = 0,
                    size: int = None, scope: str = "") -> str:
    """Stable hash for (query, form, date range, page, scope)."""
    raw = (
        f"{query}|{form_type}|{config.START_DATE}|{config.END_DATE}|"
        f"{int(offset or 0)}|{int(size or 0)}|{scope or ''}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _efts_cache_load(key: str):
    """Return cached EFTS hits if fresh, else None."""
    cache_dir = getattr(config, "EFTS_CACHE_DIR", "")
    ttl = getattr(config, "EFTS_CACHE_TTL", 0)
    if not cache_dir or not ttl:
        return None
    path = os.path.join(cache_dir, key + ".json")
    if not os.path.exists(path):
        return None
    if (time.time() - os.path.getmtime(path)) > ttl:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _efts_cache_save(key: str, hits):
    cache_dir = getattr(config, "EFTS_CACHE_DIR", "")
    if not cache_dir:
        return
    try:
        os.makedirs(cache_dir, exist_ok=True)
        with open(os.path.join(cache_dir, key + ".json"), "w", encoding="utf-8") as f:
            json.dump(hits, f)
    except Exception:
        pass


def _efts_search(query: str, form_type: str, limit: int = None,
                 offset: int = 0, scope: str = ""):
    """Direct EFTS JSON API call with file cache. Returns list of hit dicts.

    Replaces edgartools.search_filings, which hangs with PoolTimeout on this
    user's machine. v1.1.33 adds a sha256-keyed file cache with 1h TTL so
    repeated "Update Filings" clicks within the same hour are instant.
    """
    limit = int(limit or getattr(config, "EFTS_PAGE_SIZE", 100))
    offset = int(offset or 0)
    key = _efts_cache_key(query, form_type, offset=offset, size=limit, scope=scope)
    cached = _efts_cache_load(key)
    if cached is not None:
        _inc_metric("efts_cache_hits")
        return cached[:limit]
    _inc_metric("efts_cache_misses")

    params = {
        "q": query,
        "forms": form_type,
        "dateRange": "custom",
        "startdt": config.START_DATE,
        "enddt": config.END_DATE,
        "from": offset,
        "size": limit,
    }
    last_exc = None
    for attempt in range(config.EFTS_RETRIES + 1):
        try:
            r = _sec_get(
                _efts_session, config.EFTS_URL,
                params=params, timeout=config.EFTS_TIMEOUT,
            )
            data = r.json()
            hits = data.get("hits", {}).get("hits", [])
            _efts_cache_save(key, hits)
            return hits[:limit]
        except Exception as e:
            last_exc = e
            if attempt < config.EFTS_RETRIES:
                time.sleep(1)
    raise last_exc


def _hit_identity(hit) -> str:
    if not isinstance(hit, dict):
        return str(hit)
    src = hit.get("_source", {}) or {}
    hid = hit.get("_id", "") or ""
    return src.get("adsh") or hid


def _efts_search_all(query: str, form_type: str, scope: str = ""):
    """Return all configured EFTS pages for a query/form pair.

    The SEC endpoint returns ranked pages. We keep a bounded page count to keep
    initial ingestion under the hour target, but no longer stop because earlier
    pages were already in the DB.
    """
    page_size = int(getattr(config, "EFTS_PAGE_SIZE", 100) or 100)
    max_pages = max(1, int(getattr(config, "EFTS_MAX_PAGES_PER_QUERY", 1) or 1))
    all_hits = []
    seen = set()

    for page in range(max_pages):
        offset = page * page_size
        hits = _efts_search(query, form_type, limit=page_size, offset=offset, scope=scope)
        if not hits:
            break
        new_in_page = 0
        for hit in hits:
            ident = _hit_identity(hit)
            if ident in seen:
                continue
            seen.add(ident)
            all_hits.append(hit)
            new_in_page += 1
        if len(hits) < page_size or new_in_page == 0:
            break
    return all_hits


def _add_search_hit(hit, form_type, seen, todo) -> bool:
    """Convert a single EFTS hit dict into a todo tuple. Returns True
    if added (i.e. not a duplicate)."""
    src = hit.get("_source", {}) if isinstance(hit, dict) else {}
    acc = src.get("adsh") or ""
    doc_name = ""
    hid = hit.get("_id", "") if isinstance(hit, dict) else ""
    if ":" in hid:
        hid_acc, hid_doc = hid.split(":", 1)
        doc_name = hid_doc.strip()
        if not acc:
            acc = hid_acc
    if not acc:
        # Some hits encode adsh in _id like "0001234567-25-000001:file.htm"
        acc = hid
    if not acc:
        return False

    display_names = src.get("display_names") or []
    raw_name = display_names[0] if display_names else "Unknown"
    ticker = ""
    tm = re.search(r"\(([A-Z]{1,5})\)", raw_name)
    if tm and tm.group(1) not in ("CIK", "DE", "NV", "CA", "NY", "FL", "TX"):
        ticker = tm.group(1)

    company_name = re.sub(r"\s*\([A-Z]{1,5}\)\s*", " ", raw_name)
    company_name = re.sub(r"\s*\(CIK \d+\)\s*", "", company_name).strip()

    fa = src.get("form") or form_type
    rf = re.sub(r"/A$", "", fa)
    tier_val = 1 if rf in config.ETF_FUND_FORMS else 2
    ciks = src.get("ciks") or []
    ck = str(ciks[0]).lstrip("0") if ciks else ""
    fd = src.get("file_date") or ""
    doc_url = _archive_doc_url(ck, acc, doc_name)

    matched_kw = _pick_matched_keyword(company_name)

    candidate = (acc, ck, company_name, ticker, fa, rf, fd, tier_val, matched_kw, doc_url, doc_name)
    if acc in seen:
        _replace_candidate_if_better(todo, candidate)
        return False
    seen.add(acc)
    todo.append(candidate)
    return True


def _candidate_doc_rank(item) -> tuple:
    """Rank candidate documents for the same accession.

    EFTS can return exhibits before the actual filing document. The accession
    should still be deduped, but the retained document must be the source filing
    body, not an exhibit, fee table, opinion letter, or advisory schedule.
    """
    root_form = (item[5] if len(item) > 5 else "").upper()
    doc_name = (item[10] if len(item) > 10 else "").lower()
    if not doc_name:
        return (90, "")
    compact = re.sub(r"[^a-z0-9]+", "", doc_name)
    exhibitish = bool(re.search(
        r"(?:^|[_/.-])(?:ex|exhibit|fee|fees|schedule|opinion|consent|"
        r"advisory|agreement|custody|distribution|license|powers?ofattorney)",
        doc_name,
    )) or bool(re.search(r"\bex(?:hibit)?\s*[-_.]?\s*\d", doc_name, re.I))
    exhibitish = exhibitish or bool(re.search(r"ex(?:hibit)?\d{1,3}", compact))
    score = 50 if exhibitish else 10
    form_token = re.sub(r"[^a-z0-9]+", "", root_form.lower())
    if form_token and form_token in compact and not exhibitish:
        score = 0
    if root_form in ("S-1", "S-1/A") and re.search(r"(?:^|[_-])s1a?(?:[_\\.-]|$)", doc_name) and not exhibitish:
        score = 0
    if root_form in ("485APOS", "485BPOS") and root_form.lower() in doc_name and not exhibitish:
        score = 0
    if root_form == "N-1A" and ("n1a" in compact or "n1" in compact) and not exhibitish:
        score = 0
    return (score, doc_name)


def _replace_candidate_if_better(todo, candidate) -> bool:
    acc = candidate[0]
    for idx, existing in enumerate(todo):
        if existing[0] != acc:
            continue
        if _candidate_doc_rank(candidate) < _candidate_doc_rank(existing):
            todo[idx] = candidate
            return True
        return False
    return False


def _metadata_text_for_item(item) -> str:
    return " ".join(str(part or "") for part in (
        item[2] if len(item) > 2 else "",
        item[3] if len(item) > 3 else "",
        item[8] if len(item) > 8 else "",
        item[10] if len(item) > 10 else "",
    ))


_RISK_HEADING_RE = re.compile(
    r"\b(?:Risk\s+Factors|Principal\s+Investment\s+Risks|Principal\s+Risks|"
    r"Risks\s+of\s+Investing|Investment\s+Risks)\b",
    re.I,
)


def _is_non_risk_amendment_text(text: str) -> bool:
    """True when an amendment says the prospectus/risk section is omitted.

    These filings are not risk-bearing source documents. They often amend only
    Part II exhibit/fee items while incorporating the prospectus from another
    filing, so keeping them in risk_default inflates the exact-section
    denominator with documents that have no risk section to extract.
    """
    if not text:
        return False
    low = text[:30000].lower()
    amendment_only = (
        "pre-effective amendment" in low
        and (
            "filed solely to amend item 16" in low
            or "preliminary prospectus has been omitted" in low
            or "does not modify any provision of the preliminary prospectus" in low
            or "prospectus contained in part i" in low and "has been omitted" in low
        )
    )
    return amendment_only and not _RISK_HEADING_RE.search(text)


def _is_non_risk_amendment_candidate(item, scope: str) -> bool:
    if scope != "risk_default":
        return False
    form_type = (item[4] if len(item) > 4 else "").upper()
    if "/A" not in form_type and form_type not in {"485APOS", "485BPOS"}:
        return False
    doc_url = item[9] if len(item) > 9 else ""
    if not doc_url:
        return False
    try:
        full_text, _ = fetch_direct_document_text(doc_url)
    except Exception:
        return False
    if _is_non_risk_amendment_text(full_text):
        _inc_metric("non_risk_amendments")
        return True
    return False


def _is_scope_relevant_candidate(item, scope: str) -> bool:
    """Keep default discovery focused on crypto-risk fund documents.

    EFTS can match crypto words in exhibit indexes attached to unrelated
    multi-fund amendments. Those documents are not crypto-risk filings and
    should not inflate the denominator for Update Filings.
    """
    scope = _normalize_scope(scope)
    if scope in ("all", "event_risk"):
        return True
    root_form = item[5] if len(item) > 5 else ""
    if root_form not in getattr(config, "ETF_FUND_FORMS", set()):
        return True
    if not _METADATA_CRYPTO_RE.search(_metadata_text_for_item(item)):
        return False
    return not _is_non_risk_amendment_candidate(item, scope)


def _submissions_cache_path(cik: str) -> str:
    cache_dir = os.path.join(config.DATA_DIR, "submissions_cache")
    return os.path.join(cache_dir, f"CIK{str(cik).zfill(10)}.json")


def _load_submissions_cache(cik: str):
    path = _submissions_cache_path(cik)
    ttl = getattr(config, "SUBMISSIONS_CACHE_TTL", 1800)
    if not os.path.exists(path):
        return None
    if time.time() - os.path.getmtime(path) > ttl:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_submissions_cache(cik: str, data: dict):
    try:
        path = _submissions_cache_path(cik)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


def _fetch_company_submissions(cik: str):
    cached = _load_submissions_cache(cik)
    if cached is not None:
        _inc_metric("submissions_cache_hits")
        return cached
    _inc_metric("submissions_cache_misses")
    url = f"https://data.sec.gov/submissions/CIK{str(cik).zfill(10)}.json"
    r = _sec_get(_sec_session, url, timeout=getattr(config, "SUBMISSIONS_TIMEOUT", 15))
    data = r.json()
    _save_submissions_cache(cik, data)
    return data


def _sec_ticker_map_cache_path() -> str:
    return os.path.join(config.DATA_DIR, "sec_company_tickers.json")


def _load_sec_ticker_map_cache():
    path = _sec_ticker_map_cache_path()
    ttl = int(getattr(config, "SEC_TICKER_MAP_CACHE_TTL", 0) or 0)
    if not ttl or not os.path.exists(path):
        return None
    if time.time() - os.path.getmtime(path) > ttl:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_sec_ticker_map_cache(rows: dict):
    try:
        os.makedirs(config.DATA_DIR, exist_ok=True)
        with open(_sec_ticker_map_cache_path(), "w", encoding="utf-8") as f:
            json.dump(rows, f)
    except Exception:
        pass


def _fetch_sec_ticker_map() -> dict:
    """Return SEC's current ticker -> CIK map, cached outside batch folders."""
    cached = _load_sec_ticker_map_cache()
    if cached is not None:
        return cached
    url = "https://www.sec.gov/files/company_tickers.json"
    r = _sec_get(_sec_session, url, timeout=getattr(config, "SUBMISSIONS_TIMEOUT", 15))
    data = r.json()
    rows = {}
    for value in data.values():
        ticker = str(value.get("ticker") or "").upper().strip()
        cik = str(value.get("cik_str") or "").strip()
        if ticker and cik:
            rows[ticker] = {
                "cik": str(int(cik)),
                "title": str(value.get("title") or "").strip(),
            }
    _save_sec_ticker_map_cache(rows)
    return rows


_COMPANY_SUFFIX_RE = re.compile(
    r"\b(?:incorporated|inc|corp|corporation|co|company|ltd|limited|plc|llc|"
    r"holdings?|holding|group|the|de|tx)\b"
)

_METADATA_CRYPTO_RE = re.compile(
    r"\b(?:crypto(?:currency|asset| token| fund| etf)?|bitcoin|btc|ethereum|"
    r"ether|eth|xrp|solana|stablecoin|blockchain|digital asset|defi|"
    r"grayscale|bitwise|coinshares|valkyrie|hashdex|21shares|ishares bitcoin|"
    r"ishares ethereum|wise origin bitcoin)\b",
    re.I,
)


def _identity_tokens(value: str) -> set:
    value = unicodedata.normalize("NFKD", value or "").lower()
    value = re.sub(r"[^a-z0-9]+", " ", value)
    value = _COMPANY_SUFFIX_RE.sub(" ", value)
    return {tok for tok in value.split() if len(tok) >= 3}


def _names_look_related(expected_name: str, sec_name: str) -> bool:
    expected = _identity_tokens(expected_name)
    actual = _identity_tokens(sec_name)
    if not expected or not actual:
        return False
    overlap = expected & actual
    return bool(overlap) and (
        len(overlap) >= 2
        or len(overlap) >= min(len(expected), len(actual))
        or any(tok in actual for tok in expected if len(tok) >= 7)
    )


def _company_identity_matches(data: dict, expected_name: str, ticker: str) -> bool:
    sec_name = str(data.get("name") or "")
    sec_tickers = {str(t).upper() for t in (data.get("tickers") or []) if t}
    expected_ticker = (ticker or "").upper().strip()
    if expected_ticker and expected_ticker in sec_tickers:
        return True
    return _names_look_related(expected_name, sec_name)


def _resolved_crypto_companies() -> dict:
    """Correct current ticker/CIK drift before CIK-based discovery.

    Hardcoded CIKs are still the cold-start fallback, but SEC occasionally
    reassigns tickers or issuers change CIKs through restructurings. Trusting a
    stale CIK pollutes discovery with unrelated filings and creates false
    no-extraction skips, so current tickers are used to correct the map when SEC
    publishes one.
    """
    companies = getattr(config, "CRYPTO_COMPANIES", {})
    if not companies:
        return {}
    try:
        ticker_map = _fetch_sec_ticker_map()
    except Exception as e:
        print(f"  SEC ticker map unavailable; validating hardcoded CIKs only — {_fmt_exc(e)}")
        ticker_map = {}

    resolved = {}
    for cik, (name, ticker, cat) in companies.items():
        clean_cik = str(cik).lstrip("0")
        clean_ticker = (ticker or "").upper().strip()
        if clean_ticker and clean_ticker in ticker_map:
            mapped = ticker_map[clean_ticker]
            mapped_cik = str(mapped.get("cik") or "").lstrip("0")
            if mapped_cik and mapped_cik != clean_cik:
                _inc_metric("cik_corrections")
                print(
                    f"    CIK corrected: {name[:30]:30s} "
                    f"{clean_cik} -> {mapped_cik} via ticker {clean_ticker}"
                )
                clean_cik = mapped_cik
        resolved[clean_cik] = (name, clean_ticker, cat)
    return resolved


def _archive_doc_url(cik: str, accession_no: str, primary_doc: str = "") -> str:
    if not cik or not accession_no or not primary_doc:
        return ""
    cik_clean = str(cik).lstrip("0")
    acc_clean = accession_no.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik_clean}/{acc_clean}/{primary_doc}"


def _archive_index_url(cik: str, accession_no: str) -> str:
    if not cik or not accession_no:
        return ""
    cik_clean = str(cik).lstrip("0")
    acc_clean = accession_no.replace("-", "")
    return f"https://www.sec.gov/Archives/edgar/data/{cik_clean}/{acc_clean}/index.json"


def _choose_primary_doc_from_index(cik: str, accession_no: str,
                                   root_form: str, index_data: dict) -> tuple:
    items = index_data.get("directory", {}).get("item", []) if isinstance(index_data, dict) else []
    ranked = []
    for entry in items:
        name = str(entry.get("name") or "")
        low = name.lower()
        if not low.endswith((".htm", ".html")):
            continue
        if "-index" in low or low.startswith("r") and low[1:].split(".", 1)[0].isdigit():
            continue
        try:
            size = int(entry.get("size") or 0)
        except Exception:
            size = 0
        pseudo = (
            accession_no, str(cik).lstrip("0"), "", "", root_form,
            re.sub(r"/A$", "", root_form), "", 0, "", "", name,
        )
        ranked.append((_candidate_doc_rank(pseudo)[0], -size, name))
    if not ranked:
        return "", ""
    ranked.sort()
    name = ranked[0][2]
    return _archive_doc_url(cik, accession_no, name), name


def _resolve_primary_doc_url(cik: str, accession_no: str,
                             root_form: str) -> tuple:
    index_url = _archive_index_url(cik, accession_no)
    if not index_url:
        return "", ""
    r = _sec_get(_sec_session, index_url, timeout=getattr(config, "HTTP_TIMEOUT", 30))
    url, name = _choose_primary_doc_from_index(cik, accession_no, root_form, r.json())
    if url:
        _inc_metric("primary_doc_resolutions")
    return url, name


def _raw_doc_cache_path(doc_url: str) -> str:
    cache_dir = getattr(config, "RAW_DOC_CACHE_DIR", "")
    if not cache_dir or not doc_url:
        return ""
    h = hashlib.sha256(doc_url.encode("utf-8")).hexdigest()
    return os.path.join(cache_dir, h[:2], h + ".txt")


def _load_raw_doc_cache(doc_url: str):
    path = _raw_doc_cache_path(doc_url)
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return None


def _save_raw_doc_cache(doc_url: str, raw_text: str):
    path = _raw_doc_cache_path(doc_url)
    if not path or not raw_text:
        return
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(raw_text)
    except Exception:
        pass


def _fetch_company_filings(cik: str, name: str, ticker: str, category: str,
                           forms: list, per_form: int):
    """Worker: pull all filings for ONE company.

    Fast path uses one SEC submissions JSON request per CIK. edgartools Company
    remains a fallback only when the direct SEC path fails.
    """
    out = []
    try:
        data = _fetch_company_submissions(cik)
        if not _company_identity_matches(data, name, ticker):
            sec_name = data.get("name") or "unknown issuer"
            sec_tickers = ",".join(data.get("tickers") or [])
            _inc_metric("cik_identity_mismatches")
            return (
                "identity_mismatch",
                name,
                f"CIK {cik} resolves to {sec_name} ({sec_tickers})",
                out,
            )
        recent = data.get("filings", {}).get("recent", {})
        accessions = recent.get("accessionNumber", [])
        form_values = recent.get("form", [])
        filing_dates = recent.get("filingDate", [])
        primary_docs = recent.get("primaryDocument", [])
        counts = {form: 0 for form in forms}
        for idx, acc in enumerate(accessions):
            fa = form_values[idx] if idx < len(form_values) else ""
            fd = filing_dates[idx] if idx < len(filing_dates) else ""
            if fa not in forms or not _filing_date_in_range(fd) or counts.get(fa, 0) >= per_form:
                continue
            counts[fa] = counts.get(fa, 0) + 1
            rf = re.sub(r"/A$", "", fa)
            primary_doc = primary_docs[idx] if idx < len(primary_docs) else ""
            doc_url = _archive_doc_url(cik, acc, primary_doc)
            tier_val = 1 if rf in config.ETF_FUND_FORMS else 2
            out.append((acc, str(cik).lstrip("0"), name, ticker, fa, rf, fd,
                        tier_val, "company:" + category, doc_url, primary_doc))
        return ("ok", name, [], out)
    except Exception as direct_err:
        direct_error = _fmt_exc(direct_err)

    if not HAS_COMPANY:
        return ("lookup_error", name, direct_error, out)

    try:
        company = _edgar_call(Company, cik)
    except Exception as e:
        return ("lookup_error", name, _fmt_exc(e), out)

    err_forms = []
    for form in forms:
        try:
            filings = _edgar_call(company.get_filings, form=form)
            if filings is None:
                continue
            count = 0
            for f in filings:
                if count >= per_form:
                    break
                count += 1
                acc = (getattr(f, "accession_no", None)
                       or getattr(f, "accession_number", None))
                if not acc:
                    continue
                fa = getattr(f, "form", form) or form
                rf = re.sub(r"/A$", "", fa)
                fd_attr = (getattr(f, "filing_date", None)
                           or getattr(f, "filed", None))
                fd = str(fd_attr) if fd_attr else ""
                if not _filing_date_in_range(fd):
                    continue
                tier_val = 1 if rf in config.ETF_FUND_FORMS else 2
                ck = str(cik).lstrip("0")
                out.append((acc, ck, name, ticker, fa, rf, fd,
                            tier_val, "company:" + category, "", ""))
        except Exception as e:
            err_forms.append((form, _fmt_exc(e)))
            continue
    return ("ok", name, err_forms, out)


def _normalize_mode(mode: str = None) -> str:
    mode = mode or getattr(config, "DEFAULT_SCRAPE_MODE", "exact_only")
    valid = getattr(config, "SCRAPE_MODES", {"exact_only"})
    if mode not in valid:
        raise ValueError(f"Unsupported scrape mode '{mode}'. Expected one of: {sorted(valid)}")
    return mode


def _normalize_scope(scope: str = None) -> str:
    scope = scope or getattr(config, "DEFAULT_SCRAPE_SCOPE", "risk_default")
    valid = getattr(config, "SCRAPE_SCOPES", {"risk_default"})
    if scope not in valid:
        raise ValueError(f"Unsupported scrape scope '{scope}'. Expected one of: {sorted(valid)}")
    return scope


def _forms_for_scope(scope: str) -> list:
    scope = _normalize_scope(scope)
    if scope == "core":
        return list(getattr(config, "CORE_FORM_TYPES", ["10-K", "10-Q"]))
    if scope == "event_risk":
        return list(getattr(config, "EVENT_RISK_FORM_TYPES", getattr(config, "FORM_TYPES", [])))
    if scope == "all":
        return list(getattr(config, "ALL_FORM_TYPES", getattr(config, "FORM_TYPES", [])))
    return list(getattr(config, "RISK_DEFAULT_FORM_TYPES", getattr(config, "FORM_TYPES", [])))


def _efts_forms_for_scope(scope: str) -> list:
    scope = _normalize_scope(scope)
    if scope == "core":
        return list(getattr(config, "CORE_EFTS_FORM_TYPES", _forms_for_scope(scope)))
    if scope == "event_risk":
        return list(getattr(config, "EVENT_RISK_EFTS_FORM_TYPES", _forms_for_scope(scope)))
    if scope == "all":
        return list(getattr(config, "ALL_EFTS_FORM_TYPES", _forms_for_scope(scope)))
    return list(getattr(config, "RISK_DEFAULT_EFTS_FORM_TYPES", _forms_for_scope(scope)))


def _company_forms_for_scope(scope: str) -> list:
    scope = _normalize_scope(scope)
    if scope == "core":
        return list(getattr(config, "CORE_COMPANY_FORM_TYPES", ["10-K", "10-Q"]))
    if scope == "event_risk":
        return list(getattr(config, "EVENT_RISK_COMPANY_FORM_TYPES", getattr(config, "COMPANY_FORM_TYPES", [])))
    if scope == "all":
        return list(getattr(config, "ALL_COMPANY_FORM_TYPES", getattr(config, "COMPANY_FORM_TYPES", [])))
    return list(getattr(config, "RISK_DEFAULT_COMPANY_FORM_TYPES", getattr(config, "COMPANY_FORM_TYPES", [])))


def _filing_date_in_range(filing_date: str) -> bool:
    if not filing_date:
        return True
    start = getattr(config, "START_DATE", "") or ""
    end = getattr(config, "END_DATE", "") or ""
    return (not start or filing_date >= start) and (not end or filing_date <= end)


def _search_by_company_cik(seen, todo, progress_callback=None, scope: str = None) -> int:
    """Pull filings for known crypto operating companies in parallel.

    v1.1.33: was sequential (~30 companies × ~5 forms = ~150 serial HTTP calls).
    Now uses ThreadPoolExecutor with CIK_SEARCH_THREADS workers. Dedup against
    `seen` and append to `todo` under a lock to keep state consistent.

    The EFTS keyword search is biased toward fund prospectuses that use the
    literal word "cryptocurrency" / "bitcoin" in marketing copy. Operating
    companies often disclose crypto exposure inside risk factors and footnotes
    — those don't always rank high in EFTS. Going CIK-by-CIK guarantees we get
    Coinbase / Marathon / MicroStrategy / Riot / etc.
    """
    companies = _resolved_crypto_companies()
    if not companies:
        return 0

    forms = _company_forms_for_scope(scope)
    per_form = getattr(config, "COMPANY_FILINGS_PER_FORM", 20)
    workers = getattr(config, "CIK_SEARCH_THREADS", 6)

    mode = "direct SEC submissions"
    if HAS_COMPANY:
        mode += " + edgartools fallback"
    print(f"\n  CIK search: {len(companies)} companies × {len(forms)} forms "
          f"({workers} parallel workers, up to {per_form} each, {mode})")

    added_total = 0
    done = 0
    total = len(companies)
    lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=workers) as ex:
        future_to_name = {
            ex.submit(_fetch_company_filings, cik, name, ticker, cat,
                      forms, per_form): (name, ticker, cat)
            for cik, (name, ticker, cat) in companies.items()
        }
        for fut in as_completed(future_to_name):
            done += 1
            name, ticker, cat = future_to_name[fut]
            try:
                status, _, info, results = fut.result()
            except Exception as e:
                print(f"    {name[:30]:30s}: worker ERROR — {_fmt_exc(e)}")
                continue

            if status == "lookup_error":
                print(f"    {name[:30]:30s}: lookup ERROR — {info}")
                continue
            if status == "identity_mismatch":
                print(f"    {name[:30]:30s}: skipped — {info}")
                continue

            added_for_co = 0
            with lock:
                for hit in results:
                    acc = hit[0]
                    if acc in seen:
                        continue
                    seen.add(acc)
                    todo.append(hit)
                    added_for_co += 1
                    added_total += 1

            for form, err in info:
                print(f"    {name[:30]:30s} {form:8s}: ERROR — {err}")
            if added_for_co:
                print(f"    {name[:30]:30s} ({ticker:6s}): +{added_for_co} {cat}")

            if progress_callback:
                progress_callback(
                    done, total, added_total, 0,
                    message=f"CIK search {done}/{total} (+{added_total} filings)",
                )

    print(f"  CIK search added {added_total} operating-company filings")
    return added_total


def search_edgar_filings(progress_callback=None, scope: str = None):
    """Search EDGAR via direct EFTS HTTP, plus CIK lookups for known crypto
    operating companies.

    v1.1.32: bypasses edgartools.search_filings (PoolTimeout hangs forever).
    Calls EFTS JSON API directly with our own requests.Session, short timeout,
    and fail-fast on chronic errors.

    Returns list of (accession_no, cik, company_name, ticker, form_type,
                    root_form, filing_date, tier, matched_keyword).
    """
    scope = _normalize_scope(scope)
    existing = db.get_existing_accessions()
    seen = set()
    universe = []
    form_counts = {}
    forms = _efts_forms_for_scope(scope)

    batches = _build_keyword_batches()
    total_steps = len(forms) * len(batches)
    step = 0

    print(f"\n  DB: {len(existing)} terminal filings already processed")
    print(f"  Scope: {scope}")
    print(f"  EFTS forms: {', '.join(forms)}")
    print(f"  CIK forms: {', '.join(_company_forms_for_scope(scope))}")
    print(f"  Search: {config.START_DATE} -> {config.END_DATE}")
    print(f"  Keyword batches: {len(batches)} ({sum(len(b) for b in batches)} chars total)")
    for i, b in enumerate(batches, 1):
        print(f"    [{i}] {b}")

    consecutive_failures = 0
    abort_efts = False

    if progress_callback:
        progress_callback(0, total_steps, 0, 0, message="Searching EDGAR (EFTS)...")

    for form_type in forms:
        added = 0
        batch_errors = 0

        if abort_efts:
            step += len(batches)
            if progress_callback:
                progress_callback(
                    step, total_steps, len(todo), 0,
                    message=f"EFTS aborted — skipped {form_type}",
                )
            continue

        for batch_idx, query in enumerate(batches, 1):
            step += 1
            if progress_callback:
                progress_callback(
                    step, total_steps, len(universe), 0,
                    message=f"EFTS {form_type} batch {batch_idx}/{len(batches)}",
                )
            try:
                results = _efts_search_all(query, form_type, scope=scope)
                consecutive_failures = 0
            except Exception as e:
                batch_errors += 1
                consecutive_failures += 1
                print(f"    {form_type:8s} batch {batch_idx}: ERROR — {_fmt_exc(e)}")
                if consecutive_failures >= config.EFTS_MAX_CONSECUTIVE_FAILURES:
                    print(f"\n  EFTS aborted: {consecutive_failures} consecutive failures "
                          f"(EDGAR may be slow/down — falling back to CIK search)")
                    abort_efts = True
                    break
                continue

            try:
                for hit in results:
                    if _add_search_hit(hit, form_type, seen, universe):
                        added += 1
                    else:
                        _inc_metric("duplicate_candidates")
            except Exception as e:
                batch_errors += 1
                print(f"    {form_type:8s} batch {batch_idx}: iter ERROR — {_fmt_exc(e)}")

        form_counts[form_type] = added
        suffix = f" ({batch_errors} batch errors)" if batch_errors else ""
        print(f"    {form_type:8s}: {added} new ({len(batches)} batches){suffix}")

    # ─── CIK-based search for known operating companies ─────────────────
    if progress_callback:
        progress_callback(
            total_steps, total_steps, len(universe), 0,
            message="Searching by CIK (operating companies)...",
    )
    _search_by_company_cik(seen, universe, progress_callback=progress_callback, scope=scope)

    before_scope_filter = len(universe)
    universe = [item for item in universe if _is_scope_relevant_candidate(item, scope)]
    scope_filtered = before_scope_filter - len(universe)
    if scope_filtered:
        _inc_metric("scope_filtered", scope_filtered)
        print(f"  Scope filter removed {scope_filtered} non-crypto fund candidates")

    todo = [item for item in universe if item[0] not in existing]
    already_terminal = len(universe) - len(todo)
    _inc_metric("candidate_universe", len(universe))
    _inc_metric("already_terminal", already_terminal)
    if not todo and universe:
        _inc_metric("fixed_point_complete")

    t1 = sum(1 for t in todo if t[7] == 1)
    t2 = sum(1 for t in todo if t[7] == 2)
    print(f"\n  Candidate universe: {len(universe)}")
    print(f"  {len(todo)} filings to process ({already_terminal} already terminal)")
    print(f"  ETF/Fund: {t1} | Operating Co: {t2}")
    return todo


def _pick_matched_keyword(company_name: str) -> str:
    low = company_name.lower()
    for kw in config.KEYWORDS:
        if kw.lower() in low:
            return kw
    return ""


def _unpack_item(item):
    return {
        "accession_no": item[0],
        "cik": item[1],
        "company_name": item[2],
        "ticker": item[3],
        "form_type": item[4],
        "root_form": item[5],
        "filing_date": item[6],
        "tier": item[7],
        "keyword": item[8],
        "doc_url": item[9] if len(item) > 9 else "",
        "doc_name": item[10] if len(item) > 10 else "",
    }


def fetch_direct_document_text(doc_url: str):
    if not doc_url:
        return "", ""
    cached_raw = _load_raw_doc_cache(doc_url)
    if cached_raw is not None:
        _inc_metric("raw_doc_cache_hits")
        raw = cached_raw
    else:
        _inc_metric("raw_doc_cache_misses")
        r = _sec_get(_sec_session, doc_url, timeout=getattr(config, "HTTP_TIMEOUT", 30))
        raw = r.text or ""
        _save_raw_doc_cache(doc_url, raw)
    return _raw_to_text(raw), raw


def _raw_to_text(raw: str) -> str:
    """Convert raw source (HTML or plain text) to clean text. No network."""
    if not raw:
        return ""
    raw_head = raw[:5000].lower()
    if (
        "<html" in raw_head
        or "<document" in raw_head
        or "<xbrl" in raw_head
        or "<ix:" in raw_head
        or re.search(r"<(?:p|div|table|span|font|tr|td|body)\b", raw_head)
    ):
        from .extractor import clean_html_to_text
        return clean_html_to_text(raw)
    return raw


def _skip_result(accession_no: str, reason: str) -> dict:
    return {
        "_status": "skipped",
        "accession_no": accession_no,
        "reason": reason,
    }


def _fail_result(accession_no: str, reason: str) -> dict:
    return {
        "_status": "failed",
        "accession_no": accession_no,
        "reason": reason,
    }


# ═══════════════════════════════════════════════════════════════════════════
# PROCESS ONE FILING
# ═══════════════════════════════════════════════════════════════════════════

def process_one_filing(item, mode: str = None):
    """Fetch and extract one filing. Summary generation is mode-gated."""
    mode = _normalize_mode(mode)
    unpacked = _unpack_item(item)
    acc = unpacked["accession_no"]
    cik = unpacked["cik"]
    company_name = unpacked["company_name"]
    ticker = unpacked["ticker"]
    form_type = unpacked["form_type"]
    root_form = unpacked["root_form"]
    filed = unpacked["filing_date"]
    tier = unpacked["tier"]
    kw = unpacked["keyword"]
    doc_url = unpacked["doc_url"]
    doc_name = unpacked["doc_name"]

    try:
        filing = None
        raw_html = ""
        full_text = ""
        direct_fetch_error = None
        current_doc_rank = _candidate_doc_rank(item)[0]
        # Resolve the primary document via the direct archive index when the
        # candidate has no doc URL at all (keeps the filing on the direct
        # requests path instead of the edgartools fallback), or when the URL
        # we have looks low-quality.
        # v1.1.37: the DB is the source of truth. If this filing's raw source
        # is already stored, re-derive everything from it and skip the network
        # entirely — including the primary-doc index.json lookup below, which
        # was the last thing keeping "reprocess" from being offline.
        stored_raw = None
        if getattr(config, "STORE_RAW_SOURCE_IN_DB", True):
            try:
                stored_raw = db.get_raw_source(acc)
            except Exception:
                stored_raw = None
        if stored_raw and stored_raw.get("text"):
            raw_html = stored_raw["text"]
            full_text = _raw_to_text(raw_html)
            doc_url = doc_url or stored_raw.get("doc_url", "")
            doc_name = doc_name or stored_raw.get("doc_name", "")
            _inc_metric("raw_source_db_hits")

        should_resolve_primary = (not full_text) and (
            (not doc_url and cik)
            or (doc_url and (current_doc_rank >= 50 or root_form in config.ETF_FUND_FORMS))
        )
        if should_resolve_primary:
            try:
                primary_url, primary_name = _resolve_primary_doc_url(cik, acc, root_form)
                replacement = (
                    acc, cik, company_name, ticker, form_type, root_form,
                    filed, tier, kw, primary_url, primary_name,
                )
                replacement_rank = _candidate_doc_rank(replacement)[0]
                if primary_url and (
                    not doc_url
                    or replacement_rank < current_doc_rank
                    or (replacement_rank == current_doc_rank and primary_url != doc_url)
                ):
                    doc_url = primary_url
                    doc_name = primary_name
            except Exception as e:
                print(f"    primary doc lookup fallback {acc}: {_fmt_exc(e)}")

        if not full_text and doc_url:
            try:
                full_text, raw_html = fetch_direct_document_text(doc_url)
            except Exception as e:
                direct_fetch_error = e
                print(f"    direct fetch fallback {acc}: {_fmt_exc(e)}")

        if not full_text and HAS_EDGAR:
            try:
                filing = _edgar_call(get_by_accession_number, acc)
            except Exception as e:
                if isinstance(e, SecRateLimitError):
                    return _fail_result(acc, "sec_rate_limited")
                return _fail_result(acc, f"edgartools_fetch_error:{_fmt_exc(e)}")
            if filing is None:
                print(f"    SKIP {acc}: filing not found on SEC")
                return _skip_result(acc, "filing_not_found")

            # 1) Fetch text + html (cached locally by edgartools on first run)
            try:
                full_text, raw_html = _edgar_call(fetch_filing_text, filing)
            except Exception as e:
                if isinstance(e, SecRateLimitError):
                    return _fail_result(acc, "sec_rate_limited")
                return _fail_result(acc, f"edgartools_text_error:{_fmt_exc(e)}")
        elif not full_text:
            if direct_fetch_error is not None:
                if isinstance(direct_fetch_error, SecRateLimitError):
                    return _fail_result(acc, "sec_rate_limited")
                return _fail_result(acc, f"direct_fetch_error:{_fmt_exc(direct_fetch_error)}")
            print(f"    SKIP {acc}: no direct document URL and edgartools unavailable")
            return _skip_result(acc, "no_direct_document_url")
        if not full_text:
            # Zero chars almost always means a transient fetch failure, not a
            # genuinely empty document. Fail (retried next run) instead of
            # recording a terminal skip that permanently drops the filing.
            print(f"    FAIL {acc}: no document text fetched (will retry next run)")
            return _fail_result(acc, "empty_document_text")
        if len(full_text) < 200:
            print(f"    SKIP {acc}: text too short ({len(full_text)} chars)")
            return _skip_result(acc, "text_too_short")

        # 2) Multi-fund: also fetch and score individual documents
        fund_docs = []
        if root_form in config.MULTI_FUND_FORMS and filing is not None:
            with _edgar_gate:
                fund_docs = extract_fund_documents(filing)

        # 3) Run all extraction strategies, get ranked candidates
        candidates = extract_all_candidates(
            filing, full_text, html=raw_html, form_type=form_type,
        )

        # 3a) If multi-fund & the best candidate lacks crypto, try the
        #     top-scoring fund document's text instead
        if root_form in config.MULTI_FUND_FORMS and fund_docs:
            top_doc = fund_docs[0]
            if top_doc["crypto_score"] >= 3:
                doc_candidates = extract_all_candidates(
                    filing, top_doc["text"], html="", form_type=form_type,
                )
                # Merge — prefer doc candidates if they're more confident
                candidates = sorted(candidates + doc_candidates, key=_candidate_sort_key)[:5]

        # 4) Still nothing? Drop this filing
        if not candidates:
            print(f"    SKIP {acc}: no extraction candidates found")
            return _skip_result(acc, "no_extraction_candidates")

        for c in candidates:
            c.setdefault("source_doc_name", doc_name)
            c.setdefault("source_doc_url", doc_url)

        # 5) Build summary from the best candidate's text only when requested.
        primary = candidates[0]
        primary_hash = primary.get("exact_text_hash", "")
        if mode == "exact_only":
            _inc_metric("analysis_deferred")
            summary_result = {"summary": "", "model": "deferred"}
        elif primary_hash and db.get_current_summary_hash(acc) == primary_hash:
            _inc_metric("analysis_cache_hits")
            summary_result = {"summary": "", "model": "cached"}
        else:
            _inc_metric("analysis_generated")
            summary_result = build_summary(
                primary["text"], form_type=form_type, company_name=company_name,
            )

        # 6) Generate contextual per-filing metadata (entity type + form semantics)
        from .filing_metadata import generate_filing_metadata
        filing_cat = "ETF/Fund" if root_form in config.ETF_FUND_FORMS else "Operating Co."
        crypto_conn = _detect_crypto_connection(full_text[:5000], root_form)
        entity_type, purpose, holdings = generate_filing_metadata(
            company_name, ticker, form_type, filing_cat, crypto_conn, cik,
        )

        # 7) Compose records for the normalized tables
        if not kw:
            kw = _pick_matched_keyword(full_text[:5000])
        filing_pdf_url = ""
        if filing is not None:
            with _edgar_gate:
                filing_pdf_url = _detect_filing_pdf_url(filing)
        meta = {
            "accession_no": acc,
            "cik": cik,
            "company_name": _clean_text(company_name),
            "ticker": ticker,
            "form_type": form_type,
            "root_form": root_form,
            "filing_date": filed,
            "filing_category": filing_cat,
            "tier": tier,
            "sec_url": get_filing_url(cik, acc),
            "filing_pdf_url": filing_pdf_url,
            "crypto_connection": _clean_text(crypto_conn),
            "entity_type": entity_type,
            "purpose": purpose,
            "top_holdings": holdings,
            "search_keyword": kw,
            "fetched_at": datetime.now().isoformat(),
            "processed_at": datetime.now().isoformat(),
        }

        return {
            "meta": meta,
            "fund_docs": fund_docs,
            "candidates": candidates,
            "summary": summary_result.get("summary", ""),
            "summary_model": summary_result.get("model", ""),
            "summary_text_hash": primary_hash,
            # Carried so the DB write can persist the source durably. Only set
            # when freshly fetched — a filing re-derived from stored source
            # doesn't need rewriting.
            "raw_source": "" if stored_raw else (raw_html or ""),
            "raw_source_url": doc_url,
            "raw_source_name": doc_name,
        }

    except Exception as e:
        print(f"    ERROR {acc}: {_fmt_exc(e)}")
        return _fail_result(acc, f"processing_error:{_fmt_exc(e)}")


def _write_filing_to_db(record):
    """Atomic multi-table write for one filing."""
    with _lock:
        _write_filing_to_db_unlocked(record)


def _write_filing_to_db_unlocked(record):
    """Write one filing. Caller owns `_lock` and commit cadence."""
    meta = record["meta"]
    acc = meta["accession_no"]

    db.upsert_filing(meta)

    # v1.1.37: persist raw source durably so this filing can be re-extracted
    # offline forever after. Skipped when it was re-derived from stored source.
    raw_source = record.get("raw_source") or ""
    if raw_source:
        try:
            if db.save_raw_source(
                acc, raw_source,
                doc_url=record.get("raw_source_url", ""),
                doc_name=record.get("raw_source_name", ""),
            ):
                _inc_metric("raw_source_stored")
        except Exception as e:
            print(f"    raw source store skipped {acc}: {_fmt_exc(e)}")

    db.set_extraction_version(
        acc,
        getattr(config, "EXTRACTION_VERSION", config.VERSION),
        getattr(config, "PROCESSOR_VERSION", config.VERSION),
    )

    # Documents (multi-fund scoring info — optional)
    if record["fund_docs"]:
        db.replace_documents(acc, [
            {
                "name": d["name"][:200],
                "type": d["type"][:50],
                "text": d["text"][:500000],  # cap at 500KB per doc
                "crypto_score": d["crypto_score"],
                "fund_name": d["fund_name"],
            }
            for d in record["fund_docs"]
        ])

    # Sections (multiple candidates)
    db.replace_sections(acc, [
            {
                "section_type": c["section_type"],
                "method": c["method"],
                "title": c.get("title", ""),
                "text": c["text"],
                "confidence": c["confidence"],
            "source_doc_name": c.get("source_doc_name", ""),
            "source_doc_url": c.get("source_doc_url", ""),
            "source_hash": c.get("source_hash", ""),
            "exact_text_hash": c.get("exact_text_hash", ""),
            "start_offset": c.get("start_offset"),
            "end_offset": c.get("end_offset"),
        }
        for c in record["candidates"]
    ])
    _inc_metric("exact_sections_saved", 1)

    # Summary (linked to primary section)
    if record["summary"]:
        section_id = db.get_primary_section_id(acc)
        db.upsert_summary(
            acc, record["summary"], record["summary_model"],
            section_id=section_id,
            text_hash=record.get("summary_text_hash", ""),
        )


def _write_skip_to_db(item, result):
    """Persist terminal skip outcomes so repeated updates do not reprocess
    the same no-risk-section documents for this processor version."""
    if item is None:
        return
    reason = result.get("reason", "") if result else ""
    if not reason:
        return
    meta = _unpack_item(item)
    with _lock:
        db.record_filing_attempt(meta, "skipped", reason)
        _inc_metric("skip_state_writes")


def _write_result_batch(successes, skips):
    if not successes and not skips:
        return
    with _lock:
        for record in successes:
            _write_filing_to_db_unlocked(record)
            _inc_metric("db_writes")
        for item, result in skips:
            reason = result.get("reason", "") if result else ""
            if not reason or item is None:
                continue
            db.record_filing_attempt(_unpack_item(item), "skipped", reason)
            _inc_metric("skip_state_writes")
        db.commit()


def clear_filings_data():
    """Delete filing data while preserving schema and configuration."""
    db.init_db()
    conn = db.get_connection()
    with _lock:
        conn.executescript("""
            DELETE FROM filing_summaries;
            DELETE FROM filing_sections;
            DELETE FROM filing_documents;
            DELETE FROM filing_attempts;
            DELETE FROM filings;
        """)
        conn.commit()


def clear_runtime_caches():
    """Clear SEC/cache artifacts used by benchmarks."""
    for path in [
        getattr(config, "EFTS_CACHE_DIR", ""),
        os.path.join(getattr(config, "DATA_DIR", ""), "submissions_cache"),
        getattr(config, "RAW_DOC_CACHE_DIR", ""),
        getattr(config, "TEXT_CACHE_DIR", ""),
    ]:
        if path and os.path.exists(path):
            shutil.rmtree(path, ignore_errors=True)


# ═══════════════════════════════════════════════════════════════════════════
# MAIN DRIVER
# ═══════════════════════════════════════════════════════════════════════════

def regenerate_stale_extractions(limit: int = None, progress_callback=None,
                                 allow_network: bool = None) -> dict:
    """Re-derive filings whose stored extraction predates EXTRACTION_VERSION.

    This is the "new version, better net" path. Because raw source lives in
    the DB, a filing is rebuilt with the current extractor using zero SEC
    traffic. Filings whose stored output is already current are left
    untouched, so old data only changes where the new extractor actually
    produces something different.

    Returns counts; never raises for individual filing failures.
    """
    db.init_db()
    target_version = getattr(config, "EXTRACTION_VERSION", config.VERSION)
    if allow_network is None:
        allow_network = bool(getattr(config, "REGENERATE_ALLOW_NETWORK", False))
    if limit is None:
        limit = int(getattr(config, "REGENERATE_BATCH_LIMIT", 0) or 0)

    rows = db.get_stale_extraction_filings(
        target_version, limit=limit, require_raw_source=not allow_network,
    )
    total = len(rows)
    if not total:
        return {"total": 0, "rebuilt": 0, "unchanged": 0, "failed": 0, "no_source": 0}

    stats = db.get_raw_source_stats()
    print(f"\n  Regenerating {total} filings to extraction {target_version}")
    print(f"  Raw source in DB: {stats['with_raw_source']}/{stats['total_filings']} filings "
          f"({stats['stored_bytes'] / 1e6:.0f} MB stored, "
          f"{stats['raw_bytes'] / 1e6:.0f} MB raw)")
    if not allow_network:
        print(f"  Offline mode — no SEC requests will be made")

    rebuilt = failed = no_source = 0
    for i, r in enumerate(rows, 1):
        acc = r["accession_no"]
        if not allow_network and not db.has_raw_source(acc):
            no_source += 1
            continue
        item = (
            acc, r["cik"], r["company_name"], r["ticker"], r["form_type"],
            r["root_form"], r["filing_date"], r["tier"], r["search_keyword"],
            r["source_doc_url"], r["source_doc_name"],
        )
        try:
            result = process_one_filing(item, mode="exact_only")
        except Exception as e:
            failed += 1
            _inc_reason("failure_reasons", f"regenerate_error:{_fmt_exc(e)}")
            continue

        if result and result.get("meta"):
            try:
                _write_filing_to_db(result)
                rebuilt += 1
                _inc_metric("regenerated")
            except Exception as e:
                failed += 1
                print(f"    regenerate write error {acc}: {_fmt_exc(e)}")
        else:
            # Extraction produced nothing usable. Leave the existing (older)
            # data in place rather than destroying good data with a worse
            # result, but stamp it so we don't retry it every single run.
            failed += 1
            reason = (result or {}).get("reason", "no_result")
            _inc_reason("failure_reasons", f"regenerate_no_candidates:{reason}")
            db.set_extraction_version(
                acc, target_version,
                getattr(config, "PROCESSOR_VERSION", config.VERSION),
            )

        if rebuilt and rebuilt % max(1, int(getattr(config, "DB_BATCH_SIZE", 50))) == 0:
            db.commit()
        if progress_callback:
            progress_callback(
                i, total, rebuilt, failed,
                message=f"Regenerating {i}/{total} (rebuilt: {rebuilt})",
            )
    db.commit()
    print(f"  Regenerated {rebuilt}/{total} ({failed} failed, {no_source} without stored source)")
    return {
        "total": total, "rebuilt": rebuilt, "failed": failed,
        "no_source": no_source, "unchanged": 0,
    }


def backfill_raw_source(limit: int = None, progress_callback=None) -> dict:
    """Populate durable raw source for filings stored before v1.1.37.

    One-time catch-up: filings processed by older versions have no raw source
    in the DB, so they cannot be regenerated offline. This fetches from the
    local read-through cache when possible and only reaches SEC when it must.
    """
    db.init_db()
    conn = db.get_connection()
    query = """
        SELECT f.accession_no, f.cik, f.root_form,
               COALESCE(sec.source_doc_url, '') AS source_doc_url,
               COALESCE(sec.source_doc_name, '') AS source_doc_name
        FROM filings f
        LEFT JOIN filing_sections sec
          ON sec.accession_no = f.accession_no AND sec.is_primary = 1
        WHERE NOT EXISTS (
            SELECT 1 FROM filing_raw_source r WHERE r.accession_no = f.accession_no
        )
        ORDER BY f.filing_date DESC
    """
    if limit:
        query += f" LIMIT {int(limit)}"
    rows = conn.execute(query).fetchall()
    total = len(rows)
    print(f"\n  Backfilling durable raw source for {total} filings")

    stored = from_cache = failed = 0
    for i, r in enumerate(rows, 1):
        acc = r["accession_no"]
        doc_url = r["source_doc_url"]
        try:
            raw = _load_raw_doc_cache(doc_url) if doc_url else None
            if raw is not None:
                from_cache += 1
            else:
                if not doc_url:
                    doc_url, _name = _resolve_primary_doc_url(
                        r["cik"], acc, r["root_form"],
                    )
                if not doc_url:
                    failed += 1
                    continue
                resp = _sec_get(
                    _sec_session, doc_url,
                    timeout=getattr(config, "HTTP_TIMEOUT", 30),
                )
                raw = resp.text or ""
                _save_raw_doc_cache(doc_url, raw)
            if raw and db.save_raw_source(
                acc, raw, doc_url=doc_url, doc_name=r["source_doc_name"],
            ):
                stored += 1
        except Exception as e:
            failed += 1
            print(f"    backfill failed {acc}: {_fmt_exc(e)}")
        if stored and stored % 50 == 0:
            db.commit()
        if progress_callback:
            progress_callback(
                i, total, stored, failed,
                message=f"Backfilling raw source {i}/{total} (stored: {stored})",
            )
    db.commit()
    print(f"  Stored {stored}/{total} ({from_cache} from local cache, {failed} failed)")
    return {"total": total, "stored": stored, "from_cache": from_cache, "failed": failed}


def _run_regeneration_phase(progress_callback=None, metrics: dict = None) -> dict:
    """Phase 2 of an update: rebuild filings an improved extractor would change.

    Runs offline against raw source stored in the DB. Contained like the
    analysis phase — a failure here must not lose the run's extraction work.
    """
    t0 = time.perf_counter()
    try:
        result = regenerate_stale_extractions(progress_callback=progress_callback)
    except Exception as e:
        print(f"  Regeneration phase error: {_fmt_exc(e)}")
        _inc_reason("failure_reasons", "regeneration_phase_error")
        result = {"total": 0, "rebuilt": 0, "failed": 0, "no_source": 0}
    if metrics is not None:
        metrics["timings"]["regenerate_seconds"] = round(time.perf_counter() - t0, 3)
    return result


def _run_analysis_phase(progress_callback=None, metrics: dict = None) -> int:
    """Phase 3 of every run: turn saved exact sections into summaries.

    Extraction is network-bound and analysis is CPU-bound, so they stay
    separate passes — a slow SEC fetch never blocks summarization. But
    extraction alone leaves every filing reading "Analysis pending", and
    nothing else in the app triggers analysis, so every run chains it here.
    Analysis is idempotent: it only touches sections whose summary is
    missing or whose text hash has changed.
    """
    t0 = time.perf_counter()
    try:
        count = analyze_pending_sections(progress_callback=progress_callback)
    except Exception as e:
        print(f"  Analysis phase error: {_fmt_exc(e)}")
        _inc_reason("failure_reasons", "analysis_phase_error")
        count = 0
    if metrics is not None:
        metrics["timings"]["analysis_seconds"] = round(time.perf_counter() - t0, 3)
    return count


def run_scraper(progress_callback=None, mode: str = None, scope: str = None,
                benchmark: bool = False):
    start_time = time.time()
    mode = _normalize_mode(mode)
    scope = _normalize_scope(scope)
    metrics = _new_run_metrics()
    metrics["mode"] = mode
    metrics["scope"] = scope
    _set_active_metrics(metrics)

    try:
        db.init_db()

        if mode == "analysis_only":
            count = analyze_pending_sections(progress_callback=progress_callback)
            duration = time.time() - start_time
            total_in_db = db.get_filing_count()
            metrics["timings"]["total_seconds"] = round(duration, 3)
            return _complete_run({
                "new_found": 0, "saved": 0, "failed": 0,
                "skipped": 0, "reprocessed": count,
                "total_in_db": total_in_db, "duration_seconds": duration,
                "mode": mode, "scope": scope,
            }, metrics, benchmark=benchmark)

        if mode == "backfill_source":
            # One-time catch-up so filings stored by older versions become
            # offline-regenerable. This is the only mode that deliberately
            # reaches SEC for filings already in the database.
            back = backfill_raw_source(progress_callback=progress_callback)
            duration = time.time() - start_time
            metrics["timings"]["total_seconds"] = round(duration, 3)
            return _complete_run({
                "new_found": 0, "saved": 0, "failed": back.get("failed", 0),
                "skipped": 0, "reprocessed": back.get("stored", 0),
                "analyzed": 0, "regenerated": 0,
                "raw_source_stored": back.get("stored", 0),
                "total_in_db": db.get_filing_count(), "duration_seconds": duration,
                "mode": mode, "scope": scope,
            }, metrics, benchmark=benchmark)

        if mode == "regenerate":
            regen = _run_regeneration_phase(progress_callback, metrics)
            analyzed = _run_analysis_phase(progress_callback, metrics)
            duration = time.time() - start_time
            metrics["timings"]["total_seconds"] = round(duration, 3)
            return _complete_run({
                "new_found": 0, "saved": 0, "failed": regen.get("failed", 0),
                "skipped": regen.get("no_source", 0), "reprocessed": 0,
                "analyzed": analyzed, "regenerated": regen.get("rebuilt", 0),
                "total_in_db": db.get_filing_count(), "duration_seconds": duration,
                "mode": mode, "scope": scope,
            }, metrics, benchmark=benchmark)

        if mode in ("low_confidence_only", "all_cached"):
            count = reprocess_existing(
                only_low_confidence=(mode == "low_confidence_only"),
                all_cached=(mode == "all_cached"),
                mode="exact_only",
                progress_callback=progress_callback,
            )
            # Re-extraction rewrites exact text (and its hash), so summaries
            # are now stale by definition — analyze before returning.
            analyzed = _run_analysis_phase(progress_callback, metrics)
            duration = time.time() - start_time
            total_in_db = db.get_filing_count()
            metrics["timings"]["total_seconds"] = round(duration, 3)
            return _complete_run({
                "new_found": 0, "saved": 0, "failed": 0,
                "skipped": 0,
                "reprocessed": count,
                "analyzed": analyzed,
                "total_in_db": total_in_db, "duration_seconds": duration,
                "mode": mode, "scope": scope,
            }, metrics, benchmark=benchmark)

        search_t0 = time.perf_counter()
        todo = search_edgar_filings(progress_callback=progress_callback, scope=scope)
        metrics["timings"]["search_seconds"] = round(time.perf_counter() - search_t0, 3)

        if not todo:
            existing_count = db.get_total_filing_count()
            if existing_count > 0:
                print(f"\n  All filings already in database ({existing_count} total).")
                if progress_callback:
                    progress_callback(0, existing_count, 0, 0,
                                      message=f"All {existing_count} filings up to date.")
                # No new filings still leaves regeneration and analysis work,
                # so run those phases rather than returning immediately. This
                # is how a version upgrade improves existing data.
                regen = _run_regeneration_phase(progress_callback, metrics)
                analyzed = _run_analysis_phase(progress_callback, metrics)
                duration = time.time() - start_time
                total_in_db = db.get_filing_count()
                print(f"  No new filings. Regenerated {regen.get('rebuilt', 0)}, "
                      f"analyzed {analyzed} pending sections.")
                print(f"  Re-extraction of cached filings is explicit via /api/reprocess.")
                print(f"  Total with summaries: {total_in_db}")
                print(f"  Duration: {duration:.0f}s ({duration/60:.1f}min)")
                return _complete_run({
                    "new_found": 0, "saved": 0, "failed": 0,
                    "skipped": 0,
                    "reprocessed": 0,
                    "analyzed": analyzed,
                    "regenerated": regen.get("rebuilt", 0),
                    "total_in_db": total_in_db, "duration_seconds": duration,
                    "mode": mode, "scope": scope,
                }, metrics, benchmark=benchmark)
            else:
                duration = time.time() - start_time
                print(f"\n  No filings found at all — database is empty")
                return _complete_run({
                    "new_found": 0, "saved": 0, "failed": 0,
                    "skipped": 0,
                    "reprocessed": 0,
                    "analyzed": 0,
                    "total_in_db": 0, "duration_seconds": duration,
                    "mode": mode, "scope": scope,
                }, metrics, benchmark=benchmark)

        io_threads = getattr(config, "IO_THREADS", 8)
        print(f"\n  Processing {len(todo)} filings ({io_threads} threads, "
              f"local cache={'ON' if config.USE_EDGAR_LOCAL_CACHE else 'OFF'})")

        saved = 0
        failed = 0
        skipped = 0
        process_t0 = time.perf_counter()
        db_t0 = 0.0
        pending_successes = []
        pending_skips = []
        batch_size = max(1, int(getattr(config, "DB_BATCH_SIZE", 50) or 50))

        with ThreadPoolExecutor(max_workers=io_threads) as executor:
            futures = {executor.submit(process_one_filing, item, mode): item for item in todo}
            total = len(futures)

            for i, future in enumerate(as_completed(futures), 1):
                item = futures.get(future)
                try:
                    result = future.result(timeout=config.FILING_TIMEOUT)
                except Exception as e:
                    result = None
                    failed += 1
                    _inc_reason("failure_reasons", "worker_timeout_or_error")
                    acc = item[0] if item else "unknown"
                    print(f"    ERROR {acc}: worker timeout/error — {_fmt_exc(e)}")

                if result and result.get("_status") == "failed":
                    failed += 1
                    reason = result.get("reason", "")
                    _inc_reason("failure_reasons", reason)
                    print(f"    FAIL {result.get('accession_no', '')}: {reason}")
                elif result and result.get("_status") == "skipped":
                    skipped += 1
                    _inc_reason("skip_reasons", result.get("reason", ""))
                    pending_skips.append((item, result))
                elif result:
                    pending_successes.append(result)
                    saved += 1
                elif result is None:
                    skipped += 1
                    _inc_reason("skip_reasons", "empty_worker_result")

                if len(pending_successes) + len(pending_skips) >= batch_size:
                    try:
                        t0 = time.perf_counter()
                        _write_result_batch(pending_successes, pending_skips)
                        db_t0 += time.perf_counter() - t0
                    except Exception as e:
                        print(f"    DB batch write error: {_fmt_exc(e)}")
                        failed += len(pending_successes)
                        _inc_reason("failure_reasons", "db_batch_write_error")
                    finally:
                        pending_successes = []
                        pending_skips = []

                if progress_callback:
                    progress_callback(
                        i, total, saved, failed,
                        message=f"Processing {i}/{total} (saved: {saved}, failed: {failed}, skipped: {skipped})",
                    )

                if i % 20 == 0:
                    elapsed = time.time() - start_time
                    rate = i / elapsed if elapsed else 0
                    eta = (total - i) / rate if rate else 0
                    print(f"    [{i}/{total}] saved={saved} failed={failed} skipped={skipped} "
                          f"({rate:.1f}/s, ETA {eta/60:.1f}min)")

        if pending_successes or pending_skips:
            try:
                t0 = time.perf_counter()
                _write_result_batch(pending_successes, pending_skips)
                db_t0 += time.perf_counter() - t0
            except Exception as e:
                print(f"    DB final batch write error: {_fmt_exc(e)}")
                failed += len(pending_successes)
                _inc_reason("failure_reasons", "db_batch_write_error")

        metrics["timings"]["process_seconds"] = round(time.perf_counter() - process_t0, 3)
        metrics["timings"]["db_write_seconds"] = round(db_t0, 3)
        db.commit()

        # Phase 2 — rebuild older filings this version extracts better, from
        # raw source already in the DB (no SEC traffic).
        regen = _run_regeneration_phase(progress_callback, metrics)

        # Phase 3 — summarize what was just extracted (plus any older backlog).
        analyzed = _run_analysis_phase(progress_callback, metrics)

        duration = time.time() - start_time
        metrics["timings"]["total_seconds"] = round(duration, 3)
        total_in_db = db.get_filing_count()
        print(f"\n  {saved} saved, {failed} failed, {skipped} skipped, "
              f"{regen.get('rebuilt', 0)} regenerated, {analyzed} analyzed")
        print(f"  Total in database: {total_in_db}")
        print(f"  Duration: {duration:.0f}s ({duration/60:.1f}min)")
        print(f"  Metrics: {metrics['counters']}")
        if metrics.get("skip_reasons"):
            print(f"  Skip reasons: {metrics['skip_reasons']}")
        if metrics.get("failure_reasons"):
            print(f"  Failure reasons: {metrics['failure_reasons']}")

        return _complete_run({
            "new_found": len(todo),
            "saved": saved,
            "failed": failed,
            "skipped": skipped,
            "reprocessed": 0,
            "analyzed": analyzed,
            "regenerated": regen.get("rebuilt", 0),
            "total_in_db": total_in_db,
            "duration_seconds": duration,
            "mode": mode,
            "scope": scope,
        }, metrics, benchmark=benchmark)
    except Exception:
        _set_active_metrics(None)
        raise


# ═══════════════════════════════════════════════════════════════════════════
# RE-PROCESS — run extraction/summarization over CACHED filings without
# re-downloading. Useful for iterating on extraction logic.
# ═══════════════════════════════════════════════════════════════════════════

def analyze_pending_sections(limit: int = None, progress_callback=None) -> int:
    """Summarize exact sections whose current summary is missing or stale."""
    db.init_db()
    conn = db.get_connection()
    query = """
        SELECT sec.id, sec.accession_no, sec.text, sec.exact_text_hash,
               f.form_type, f.company_name, f.ticker, f.cik, f.root_form,
               f.filing_date, f.entity_type
        FROM filing_sections sec
        JOIN filings f ON f.accession_no = sec.accession_no
        LEFT JOIN filing_summaries sm
          ON sm.accession_no = sec.accession_no AND sm.is_current = 1
        WHERE sec.is_primary = 1
          AND sec.text != ''
          AND (sm.id IS NULL
               OR COALESCE(sm.text_hash, '') != COALESCE(sec.exact_text_hash, '')
               OR COALESCE(sm.overview, '') = '')
        ORDER BY f.filing_date DESC
    """
    if limit:
        query += f" LIMIT {int(limit)}"
    rows = conn.execute(query).fetchall()
    total = len(rows)
    print(f"Analyzing {total} exact risk sections needing summaries...")
    done = 0
    rejected = 0
    for i, r in enumerate(rows, 1):
        summary_result = build_summary(
            r["text"], form_type=r["form_type"], company_name=r["company_name"],
        )
        summary_text = summary_result.get("summary", "")
        summary_model = summary_result.get("model", "")

        # v1.1.37: plain-English overview + a diff against this filer's prior
        # filing of the same form. Both are derived from data already in the
        # DB, so they cost no network and no API call.
        overview = ""
        whats_new = ""
        try:
            overview = build_overview(
                r["text"], form_type=r["form_type"], company_name=r["company_name"],
                ticker=r["ticker"] or "", entity_type=r["entity_type"] or "",
            )
            prior = db.get_prior_filing_section(
                r["cik"], r["root_form"], r["filing_date"],
                exclude_accession=r["accession_no"],
            )
            if prior:
                whats_new = build_whats_new(
                    r["text"], prior["text"], form_type=r["form_type"],
                    prior_form_type=prior["form_type"], prior_date=prior["filing_date"],
                )
        except Exception as e:
            print(f"    overview/whats-new skipped {r['accession_no']}: {_fmt_exc(e)}")

        if not summary_text:
            # build_summary's crypto-relevance gate rejected this section. Mark
            # the row explicitly: an empty summary with an empty model reads as
            # "analyzed" to the dashboard while the UI still shows "Analysis
            # pending", so the filing looks stuck forever with no explanation.
            summary_model = summary_model or f"no-summary-v{config.VERSION}"
            rejected += 1
            _inc_metric("analysis_rejected")
        db.upsert_summary(
            r["accession_no"],
            summary_text,
            summary_model,
            section_id=r["id"],
            text_hash=r["exact_text_hash"],
            overview=overview,
            whats_new=whats_new,
        )
        _inc_metric("analysis_generated")
        done += 1
        if done % max(1, int(getattr(config, "DB_BATCH_SIZE", 50) or 50)) == 0:
            db.commit()
        if progress_callback:
            progress_callback(
                i, total, done, 0,
                message=f"Analyzing {i}/{total} exact sections",
            )
    db.commit()
    note = f" ({rejected} below crypto-relevance threshold)" if rejected else ""
    print(f"Done. Analyzed {done}/{total} sections{note}.")
    return done


def reprocess_existing(limit: int = None, only_low_confidence: bool = False,
                       all_cached: bool = False, mode: str = "exact_only",
                       progress_callback=None):
    """Re-run extraction + summarization over filings already in the DB.

    v1.1.30: only reprocesses filings that DON'T already have a summary,
    or whose primary section confidence is below 0.5. Avoids re-doing
    work that's already complete.
    """
    db.init_db()
    conn = db.get_connection()
    query = """
        SELECT f.accession_no, f.cik, f.company_name, f.ticker,
               f.form_type, f.root_form, f.filing_date, f.tier, f.search_keyword,
               COALESCE(sec.source_doc_url, '') AS source_doc_url,
               COALESCE(sec.source_doc_name, '') AS source_doc_name
        FROM filings f
        LEFT JOIN filing_summaries s ON s.accession_no = f.accession_no
        LEFT JOIN filing_sections sec ON sec.accession_no = f.accession_no AND sec.is_primary = 1
        WHERE s.accession_no IS NULL OR sec.confidence < 0.5
    """
    if all_cached:
        query = """
            SELECT f.accession_no, f.cik, f.company_name, f.ticker,
                   f.form_type, f.root_form, f.filing_date, f.tier, f.search_keyword,
                   COALESCE(sec.source_doc_url, '') AS source_doc_url,
                   COALESCE(sec.source_doc_name, '') AS source_doc_name
            FROM filings f
            LEFT JOIN filing_sections sec
              ON sec.accession_no = f.accession_no AND sec.is_primary = 1
        """
    elif only_low_confidence:
        query = """
            SELECT f.accession_no, f.cik, f.company_name, f.ticker,
                   f.form_type, f.root_form, f.filing_date, f.tier, f.search_keyword,
                   COALESCE(sec.source_doc_url, '') AS source_doc_url,
                   COALESCE(sec.source_doc_name, '') AS source_doc_name
            FROM filings f
            JOIN filing_sections sec ON sec.accession_no = f.accession_no
            WHERE sec.is_primary = 1 AND sec.confidence < 0.5
        """
    if limit:
        query += f" LIMIT {limit}"

    rows = conn.execute(query).fetchall()
    total = len(rows)
    print(f"Reprocessing {total} filings needing work (skipping already-summarized)...")

    saved = 0
    failed = 0
    skipped = 0
    for i, r in enumerate(rows, 1):
        item = (r["accession_no"], r["cik"], r["company_name"], r["ticker"],
                r["form_type"], r["root_form"], r["filing_date"], r["tier"],
                r["search_keyword"], r["source_doc_url"], r["source_doc_name"])
        try:
            result = process_one_filing(item, mode=mode)
        except Exception:
            result = None
            failed += 1
            _inc_reason("failure_reasons", "reprocess_worker_error")
        if result and result.get("_status") == "failed":
            failed += 1
            reason = result.get("reason", "")
            _inc_reason("failure_reasons", reason)
            print(f"    FAIL {result.get('accession_no', '')}: {reason}")
        elif result and result.get("_status") == "skipped":
            skipped += 1
            _inc_reason("skip_reasons", result.get("reason", ""))
        elif result:
            try:
                _write_filing_to_db(result)
                _inc_metric("db_writes")
                saved += 1
            except Exception:
                failed += 1
                _inc_reason("failure_reasons", "db_write_error")
            if saved % 20 == 0:
                db.commit()
        elif result is None:
            skipped += 1
            _inc_reason("skip_reasons", "empty_worker_result")
        if progress_callback:
            progress_callback(
                i, total, saved, failed,
                message=f"Reprocessing {i}/{total} (saved: {saved}, failed: {failed}, skipped: {skipped})",
            )
        if i % 50 == 0:
            print(f"  [{i}/{total}] saved={saved} failed={failed} skipped={skipped}")
    db.commit()
    print(f"Done. Reprocessed {saved}/{total} filings ({failed} failed, {skipped} skipped).")
    return saved
