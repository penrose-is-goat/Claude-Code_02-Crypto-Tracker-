"""
SEC EDGAR Scraper v1.1.32.

v1.1.32 changes:
- Bypass edgartools.search_filings (PoolTimeout hangs each call for 60s)
- Direct EFTS HTTP via `requests` with 15s timeout, 1 retry
- Progress callback during search phase (UI no longer stuck on "Starting")
- Fail-fast: abort EFTS phase after N straight failures, skip to CIK + reprocess

v1.1.31 carry-over: purpose + holdings, skip obj() for prospectuses
v1.1.30 foundations: lxml parser, text-cleaning cache, shared DB
"""
import os
import re
import time
import threading
import traceback
import unicodedata
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests

from edgar import set_identity, get_by_accession_number

# Company API is the canonical way to pull a single issuer's filings by CIK.
try:
    from edgar import Company
    HAS_COMPANY = True
except ImportError:
    HAS_COMPANY = False

from . import config
from . import database as db
from .extractor import (
    fetch_filing_text,
    extract_all_candidates,
    extract_fund_documents,
    get_filing_url,
    CRYPTO_RE,
)
from .summarizer import build_summary


# ─── One-time setup ──────────────────────────────────────────────────────
set_identity(config.SEC_IDENTITY)

# Enable edgartools local storage cache (v1.1.27 speed fix).
# All subsequent Filing.text()/.html()/.attachments calls hit disk after
# the first fetch. Returning to these filings costs ~0 instead of ~5 seconds.
if config.USE_EDGAR_LOCAL_CACHE:
    try:
        os.environ.setdefault("EDGAR_USE_LOCAL_DATA", "True")
        os.environ.setdefault("EDGAR_LOCAL_DATA_DIR", config.EDGAR_CACHE_DIR)
        # Newer edgartools also supports direct API — best-effort
        from edgar import use_local_storage
        use_local_storage(True, local_path=config.EDGAR_CACHE_DIR)
    except Exception:
        pass  # env-var fallback still works for older versions

_lock = threading.Lock()


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


def _efts_search(query: str, form_type: str, limit: int = 100):
    """Direct EFTS JSON API call. Returns list of hit dicts.

    Replaces edgartools.search_filings, which hangs with PoolTimeout on this
    user's machine. We use our own requests.Session with a short timeout so
    failures fail fast instead of stalling the whole scraper.
    """
    params = {
        "q": query,
        "forms": form_type,
        "dateRange": "custom",
        "startdt": config.START_DATE,
        "enddt": config.END_DATE,
    }
    last_exc = None
    for attempt in range(config.EFTS_RETRIES + 1):
        try:
            r = _efts_session.get(
                config.EFTS_URL, params=params, timeout=config.EFTS_TIMEOUT,
            )
            r.raise_for_status()
            data = r.json()
            return data.get("hits", {}).get("hits", [])[:limit]
        except Exception as e:
            last_exc = e
            if attempt < config.EFTS_RETRIES:
                time.sleep(1)
    raise last_exc


def _add_search_hit(hit, form_type, seen, todo) -> bool:
    """Convert a single EFTS hit dict into a todo tuple. Returns True
    if added (i.e. not a duplicate)."""
    src = hit.get("_source", {}) if isinstance(hit, dict) else {}
    acc = src.get("adsh") or ""
    if not acc:
        # Some hits encode adsh in _id like "0001234567-25-000001:file.htm"
        hid = hit.get("_id", "") if isinstance(hit, dict) else ""
        if ":" in hid:
            acc = hid.split(":", 1)[0]
    if not acc or acc in seen:
        return False
    seen.add(acc)

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

    matched_kw = _pick_matched_keyword(company_name)

    todo.append((acc, ck, company_name, ticker, fa, rf, fd, tier_val, matched_kw))
    return True


def _search_by_company_cik(seen, todo) -> int:
    """Pull filings for known crypto operating companies directly by CIK.

    The EFTS keyword search is biased toward fund prospectuses that use
    the literal word "cryptocurrency" / "bitcoin" in marketing copy.
    Operating companies (exchanges, miners, treasury holders) often
    disclose crypto exposure inside risk factors and footnotes — those
    don't always rank high in EFTS. Going CIK-by-CIK guarantees we get
    Coinbase / Marathon / MicroStrategy / Riot / etc.
    """
    if not HAS_COMPANY:
        print("  CIK search: edgar.Company unavailable, skipping")
        return 0

    companies = getattr(config, "CRYPTO_COMPANIES", {})
    if not companies:
        return 0

    forms = getattr(config, "COMPANY_FORM_TYPES", ["10-K", "10-Q", "8-K"])
    per_form = getattr(config, "COMPANY_FILINGS_PER_FORM", 20)

    print(f"\n  CIK search: {len(companies)} crypto operating companies "
          f"x {len(forms)} forms (up to {per_form} each)")

    added_total = 0
    for cik, (name, ticker, category) in companies.items():
        added_for_co = 0
        try:
            company = Company(cik)
        except Exception as e:
            print(f"    {name[:30]:30s}: lookup ERROR — {_fmt_exc(e)}")
            continue

        for form in forms:
            try:
                filings = company.get_filings(form=form)
                if filings is None:
                    continue
                # filings is a Filings object; iterate the head
                count = 0
                for f in filings:
                    if count >= per_form:
                        break
                    count += 1

                    acc = getattr(f, "accession_no", None) or getattr(f, "accession_number", None)
                    if not acc or acc in seen:
                        continue
                    seen.add(acc)

                    fa = getattr(f, "form", form) or form
                    rf = re.sub(r"/A$", "", fa)
                    fd = ""
                    fd_attr = getattr(f, "filing_date", None) or getattr(f, "filed", None)
                    if fd_attr:
                        fd = str(fd_attr)
                    tier_val = 1 if rf in config.ETF_FUND_FORMS else 2
                    ck = str(cik).lstrip("0")

                    todo.append((acc, ck, name, ticker, fa, rf, fd,
                                 tier_val, "company:" + category))
                    added_for_co += 1
                    added_total += 1
            except Exception as e:
                print(f"    {name[:30]:30s} {form:8s}: ERROR — {_fmt_exc(e)}")
                continue

        if added_for_co:
            print(f"    {name[:30]:30s} ({ticker:6s}): +{added_for_co} {category}")

    print(f"  CIK search added {added_total} operating-company filings")
    return added_total


def search_edgar_filings(progress_callback=None):
    """Search EDGAR via direct EFTS HTTP, plus CIK lookups for known crypto
    operating companies.

    v1.1.32: bypasses edgartools.search_filings (PoolTimeout hangs forever).
    Calls EFTS JSON API directly with our own requests.Session, short timeout,
    and fail-fast on chronic errors.

    Returns list of (accession_no, cik, company_name, ticker, form_type,
                    root_form, filing_date, tier, matched_keyword).
    """
    existing = db.get_existing_accessions()
    seen = set(existing)
    todo = []
    form_counts = {}

    batches = _build_keyword_batches()
    total_steps = len(config.FORM_TYPES) * len(batches)
    step = 0

    print(f"\n  DB: {len(existing)} filings already processed")
    print(f"  Search: {config.START_DATE} -> {config.END_DATE}")
    print(f"  Keyword batches: {len(batches)} ({sum(len(b) for b in batches)} chars total)")
    for i, b in enumerate(batches, 1):
        print(f"    [{i}] {b}")

    consecutive_failures = 0
    abort_efts = False

    if progress_callback:
        progress_callback(0, total_steps, 0, 0, message="Searching EDGAR (EFTS)...")

    for form_type in config.FORM_TYPES:
        added = 0
        max_per_form = 100
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
            if added >= max_per_form:
                step += 1
                continue
            step += 1
            if progress_callback:
                progress_callback(
                    step, total_steps, len(todo), 0,
                    message=f"EFTS {form_type} batch {batch_idx}/{len(batches)}",
                )
            try:
                results = _efts_search(query, form_type)
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
                    if added >= max_per_form:
                        break
                    if _add_search_hit(hit, form_type, seen, todo):
                        added += 1
            except Exception as e:
                batch_errors += 1
                print(f"    {form_type:8s} batch {batch_idx}: iter ERROR — {_fmt_exc(e)}")

        form_counts[form_type] = added
        suffix = f" ({batch_errors} batch errors)" if batch_errors else ""
        print(f"    {form_type:8s}: {added} new ({len(batches)} batches){suffix}")

    # ─── CIK-based search for known operating companies ─────────────────
    if progress_callback:
        progress_callback(
            total_steps, total_steps, len(todo), 0,
            message="Searching by CIK (operating companies)...",
        )
    _search_by_company_cik(seen, todo)

    t1 = sum(1 for t in todo if t[7] == 1)
    t2 = sum(1 for t in todo if t[7] == 2)
    print(f"\n  {len(todo)} new filings to process ({len(existing)} skipped)")
    print(f"  ETF/Fund: {t1} | Operating Co: {t2}")
    return todo


def _pick_matched_keyword(company_name: str) -> str:
    low = company_name.lower()
    for kw in config.KEYWORDS:
        if kw.lower() in low:
            return kw
    return ""


# ═══════════════════════════════════════════════════════════════════════════
# PROCESS ONE FILING
# ═══════════════════════════════════════════════════════════════════════════

def process_one_filing(item):
    """Fetch, extract, summarize. Writes to 4 normalized tables."""
    acc, cik, company_name, ticker, form_type, root_form, filed, tier, kw = item

    try:
        filing = get_by_accession_number(acc)
        if filing is None:
            print(f"    SKIP {acc}: filing not found on SEC")
            return None

        # 1) Fetch text + html (cached locally by edgartools on first run)
        full_text, raw_html = fetch_filing_text(filing)
        if not full_text or len(full_text) < 200:
            print(f"    SKIP {acc}: text too short ({len(full_text) or 0} chars)")
            return None

        # 2) Multi-fund: also fetch and score individual documents
        fund_docs = []
        if root_form in config.MULTI_FUND_FORMS:
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
                candidates = sorted(
                    candidates + doc_candidates, key=lambda c: -c["confidence"],
                )[:5]

        # 4) Still nothing? Drop this filing
        if not candidates:
            print(f"    SKIP {acc}: no extraction candidates found")
            return None

        # 5) Build summary from the best candidate's text
        primary = candidates[0]
        summary_result = build_summary(
            primary["text"], form_type=form_type, company_name=company_name,
        )

        # 6) Determine purpose + holdings (lookup table first, then Claude/template)
        details = getattr(config, "COMPANY_DETAILS", {})
        if cik in details:
            purpose, holdings = details[cik]
        else:
            purpose = summary_result.get("purpose", "")
            holdings = summary_result.get("top_holdings", "")

        # 7) Compose records for the normalized tables
        if not kw:
            kw = _pick_matched_keyword(full_text[:5000])
        meta = {
            "accession_no": acc,
            "cik": cik,
            "company_name": _clean_text(company_name),
            "ticker": ticker,
            "form_type": form_type,
            "root_form": root_form,
            "filing_date": filed,
            "filing_category": "ETF/Fund" if root_form in config.ETF_FUND_FORMS else "Operating Co.",
            "tier": tier,
            "sec_url": get_filing_url(cik, acc),
            "filing_pdf_url": _detect_filing_pdf_url(filing),
            "crypto_connection": _clean_text(_detect_crypto_connection(full_text, root_form)),
            "search_keyword": kw,
            "purpose": purpose,
            "top_holdings": holdings,
            "fetched_at": datetime.now().isoformat(),
            "processed_at": datetime.now().isoformat(),
        }

        return {
            "meta": meta,
            "fund_docs": fund_docs,
            "candidates": candidates,
            "summary": summary_result.get("summary", ""),
            "summary_model": summary_result.get("model", ""),
        }

    except Exception as e:
        print(f"    ERROR {acc}: {_fmt_exc(e)}")
        return None


def _write_filing_to_db(record):
    """Atomic multi-table write for one filing."""
    meta = record["meta"]
    acc = meta["accession_no"]

    with _lock:
        db.upsert_filing(meta)

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
                "text": c["text"][:200000],
                "confidence": c["confidence"],
            }
            for c in record["candidates"]
        ])

        # Summary (linked to primary section)
        if record["summary"]:
            section_id = db.get_primary_section_id(acc)
            db.upsert_summary(
                acc, record["summary"], record["summary_model"],
                section_id=section_id,
            )


# ═══════════════════════════════════════════════════════════════════════════
# MAIN DRIVER
# ═══════════════════════════════════════════════════════════════════════════

def run_scraper(progress_callback=None):
    start_time = time.time()
    db.init_db()

    todo = search_edgar_filings(progress_callback=progress_callback)

    if not todo:
        existing_count = db.get_total_filing_count()
        if existing_count > 0:
            print(f"\n  No new filings. Auto-reprocessing {existing_count} cached filings...")
            if progress_callback:
                progress_callback(0, existing_count, 0, 0)
            reprocessed = reprocess_existing(progress_callback=progress_callback)
            duration = time.time() - start_time
            total_in_db = db.get_filing_count()
            print(f"\n  Reprocessed {reprocessed}/{existing_count} filings")
            print(f"  Total with summaries: {total_in_db}")
            print(f"  Duration: {duration:.0f}s ({duration/60:.1f}min)")
            return {
                "new_found": 0, "saved": 0, "failed": 0,
                "reprocessed": reprocessed,
                "total_in_db": total_in_db, "duration_seconds": duration,
            }
        else:
            duration = time.time() - start_time
            print(f"\n  No filings found at all — database is empty")
            return {
                "new_found": 0, "saved": 0, "failed": 0,
                "reprocessed": 0,
                "total_in_db": 0, "duration_seconds": duration,
            }

    io_threads = getattr(config, "IO_THREADS", 8)
    print(f"\n  Processing {len(todo)} filings ({io_threads} threads, "
          f"local cache={'ON' if config.USE_EDGAR_LOCAL_CACHE else 'OFF'})")

    saved = 0
    failed = 0

    with ThreadPoolExecutor(max_workers=io_threads) as executor:
        futures = {executor.submit(process_one_filing, item): item for item in todo}
        total = len(futures)

        for i, future in enumerate(as_completed(futures), 1):
            try:
                result = future.result(timeout=config.FILING_TIMEOUT)
            except Exception:
                result = None
                failed += 1

            if result:
                try:
                    _write_filing_to_db(result)
                    saved += 1
                    if saved % 25 == 0:
                        db.commit()
                except Exception as e:
                    print(f"    DB write error: {_fmt_exc(e)}")
                    failed += 1

            if progress_callback:
                progress_callback(i, total, saved, failed)

            if i % 20 == 0:
                elapsed = time.time() - start_time
                rate = i / elapsed if elapsed else 0
                eta = (total - i) / rate if rate else 0
                print(f"    [{i}/{total}] saved={saved} failed={failed} "
                      f"({rate:.1f}/s, ETA {eta/60:.1f}min)")

    db.commit()
    duration = time.time() - start_time
    total_in_db = db.get_filing_count()
    print(f"\n  {saved} saved, {failed} failed, {len(todo) - saved - failed} dropped")
    print(f"  Total in database: {total_in_db}")
    print(f"  Duration: {duration:.0f}s ({duration/60:.1f}min)")

    return {
        "new_found": len(todo),
        "saved": saved,
        "failed": failed,
        "reprocessed": 0,
        "total_in_db": total_in_db,
        "duration_seconds": duration,
    }


# ═══════════════════════════════════════════════════════════════════════════
# RE-PROCESS — run extraction/summarization over CACHED filings without
# re-downloading. Useful for iterating on extraction logic.
# ═══════════════════════════════════════════════════════════════════════════

def reprocess_existing(limit: int = None, only_low_confidence: bool = False,
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
               f.form_type, f.root_form, f.filing_date, f.tier, f.search_keyword
        FROM filings f
        LEFT JOIN filing_summaries s ON s.accession_no = f.accession_no
        LEFT JOIN filing_sections sec ON sec.accession_no = f.accession_no AND sec.is_primary = 1
        WHERE s.accession_no IS NULL OR sec.confidence < 0.5
    """
    if only_low_confidence:
        query = """
            SELECT f.accession_no, f.cik, f.company_name, f.ticker,
                   f.form_type, f.root_form, f.filing_date, f.tier, f.search_keyword
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
    for i, r in enumerate(rows, 1):
        item = (r["accession_no"], r["cik"], r["company_name"], r["ticker"],
                r["form_type"], r["root_form"], r["filing_date"], r["tier"],
                r["search_keyword"])
        try:
            result = process_one_filing(item)
        except Exception:
            result = None
            failed += 1
        if result:
            try:
                _write_filing_to_db(result)
                saved += 1
            except Exception:
                failed += 1
            if saved % 20 == 0:
                db.commit()
        if progress_callback:
            progress_callback(i, total, saved, failed)
        if i % 50 == 0:
            print(f"  [{i}/{total}] saved={saved} failed={failed}")
    db.commit()
    print(f"Done. Reprocessed {saved}/{total} filings ({failed} failed).")
    return saved
