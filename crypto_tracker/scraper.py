"""
SEC EDGAR Scraper v1.1.26 — Rebuilt to use edgartools Filing objects.

v24/v25 APPROACH (broken):
- Used raw HTTP to download documents from filing index pages
- Manually parsed HTML and searched for risk sections with regex
- Frequently downloaded wrong documents or missed the primary filing

v1.1.26 APPROACH:
- Uses edgartools' search_filings() for discovery (same as before)
- Uses get_by_accession_number() to get a proper Filing object
- Uses Filing.text(), Filing.sections(), Filing.search() for extraction
- Uses Filing.attachments for multi-document filings
- Stores full document text + risk section + summary in SQLite
- Incremental: only processes new filings not already in the database
"""
import re
import time
import threading
import traceback
import unicodedata
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

from edgar import set_identity, search_filings, get_by_accession_number

from . import config
from . import database as db
from .extractor import (
    extract_risk_from_filing,
    get_filing_url,
    CRYPTO_RE,
    _has_sufficient_crypto_content,
    _extract_risk_from_text,
    _extract_document_body,
    _html_to_text,
)
from .summarizer import build_summary


set_identity(config.SEC_IDENTITY)

_lock = threading.Lock()


def _clean_text(text):
    """Normalize unicode and collapse whitespace."""
    if not text:
        return text
    normalized = unicodedata.normalize("NFKD", str(text))
    cleaned = "".join(
        c for c in normalized if ord(c) < 128 or c in ['"', "'", "-"]
    )
    return re.sub(r"\s+", " ", cleaned).strip()


def _detect_crypto_connection(text, root_form):
    """Generate a description of how this filing relates to crypto."""
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
    else:
        return f"Filing related to {joined}."


def _detect_filing_pdf_url(filing):
    """Try to find a PDF attachment URL for the filing."""
    try:
        attachments = filing.attachments
        if attachments and attachments.documents:
            for doc in attachments.documents:
                if hasattr(doc, 'url') and doc.url and doc.url.endswith('.pdf'):
                    return doc.url
    except Exception:
        pass
    return ""


def process_one_filing(item):
    """Process a single filing using edgartools Filing object.

    This is the core improvement: instead of raw HTTP downloads and regex parsing,
    we use edgartools' built-in document access and section parsing.

    Args:
        item: tuple of (accession_no, cik, company_name, ticker, form_type,
              root_form, filing_date, tier, keyword)

    Returns:
        dict ready for database insertion, or None on failure.
    """
    acc, cik, company_name, ticker, form_type, root_form, filed, tier, kw = item

    try:
        # Get the Filing object from edgartools
        filing = get_by_accession_number(acc)
        if filing is None:
            return None

        # Extract risk section using the new edgartools-based extractor
        risk_section, full_text = extract_risk_from_filing(filing)

        if not full_text or len(full_text) < 200:
            return None

        # Build summary from risk section (or full text for non-risk filings)
        source_text = risk_section if (risk_section and len(risk_section) > 200) else full_text
        summary = build_summary(source_text, form_type=form_type, company_name=company_name)
        if not summary:
            return None

        # Guarantee some content for the dropdown
        if not risk_section or len(risk_section) < 100:
            risk_section = _extract_document_body(full_text)

        crypto_connection = _detect_crypto_connection(full_text, root_form)
        sec_url = get_filing_url(cik, acc)
        pdf_url = _detect_filing_pdf_url(filing)
        category = "ETF/Fund" if root_form in config.ETF_FUND_FORMS else "Operating Co."

        return {
            "accession_no": acc,
            "cik": cik,
            "company_name": _clean_text(company_name),
            "ticker": ticker,
            "form_type": form_type,
            "root_form": root_form,
            "filing_date": filed,
            "filing_category": category,
            "tier": tier,
            "text_length": len(full_text),
            "risk_summary": _clean_text(summary),
            "risk_section": risk_section[:100000],
            "crypto_connection": _clean_text(crypto_connection),
            "sec_url": sec_url,
            "filing_pdf_url": pdf_url,
            "search_keyword": kw,
            "n_risk_paras": risk_section.count("\n\n") + 1 if risk_section else 0,
            "processed_at": datetime.now().isoformat(),
        }

    except Exception as e:
        # Log the error for debugging
        try:
            print(f"    ERROR processing {acc}: {str(e)[:100]}")
        except Exception:
            pass
        return None


def search_edgar_filings():
    """Search SEC EDGAR for crypto-related filings.

    Returns:
        list of tuples: (accession_no, cik, company_name, ticker, form_type,
                        root_form, filing_date, tier, keyword)
    """
    existing = db.get_existing_accessions()
    seen = set(existing)
    todo = []
    form_counts = {}

    print(f"\n  DB: {len(existing)} filings already processed")
    print(f"\n  Searching EDGAR | {config.START_DATE} -> {config.END_DATE}")

    for form_type in config.FORM_TYPES:
        form_count = 0
        for kw in config.KEYWORDS:
            if form_count >= config.MAX_PER_KEYWORD * len(config.KEYWORDS):
                break
            try:
                results = search_filings(
                    kw,
                    forms=[form_type],
                    start_date=config.START_DATE,
                    end_date=config.END_DATE,
                )
                added = 0
                for r in results:
                    if added >= config.MAX_PER_KEYWORD:
                        break
                    acc = r.accession_number
                    if acc in seen:
                        continue
                    seen.add(acc)

                    raw_name = r.company or "Unknown"
                    ticker = ""
                    tm = re.search(r"\(([A-Z]{1,5})\)", raw_name)
                    if tm and tm.group(1) not in (
                        "CIK", "DE", "NV", "CA", "NY", "FL", "TX",
                    ):
                        ticker = tm.group(1)

                    company_name = re.sub(r"\s*\([A-Z]{1,5}\)\s*", " ", raw_name)
                    company_name = re.sub(
                        r"\s*\(CIK \d+\)\s*", "", company_name
                    ).strip()

                    fa = r.form or form_type
                    rf = re.sub(r"/A$", "", fa)
                    tier_val = 1 if rf in config.ETF_FUND_FORMS else 2
                    ck = str(r.cik or "").lstrip("0")
                    fd = str(r.filed) if r.filed else ""

                    todo.append((acc, ck, company_name, ticker, fa, rf, fd, tier_val, kw))
                    added += 1
                    form_count += 1

            except Exception:
                pass

            time.sleep(config.REQUEST_DELAY)

        form_counts[form_type] = form_count
        print(f"    {form_type:8s}: {form_count} new")

    t1 = sum(1 for t in todo if t[7] == 1)
    t2 = sum(1 for t in todo if t[7] == 2)
    print(f"\n  {len(todo)} new filings to process ({len(existing)} in DB skipped)")
    print(f"  ETF/Fund: {t1} | Operating Co: {t2}")

    return todo


def run_scraper(progress_callback=None):
    """Run the full scraping pipeline: search, download, extract, save.

    Uses edgartools get_by_accession_number() for each filing instead of
    raw HTTP downloads. This is slower per filing but much more accurate.

    Args:
        progress_callback: Optional callable(current, total, saved, failed)

    Returns:
        dict with stats: {new_found, saved, failed, total_in_db, duration_seconds}
    """
    start_time = time.time()
    db.init_db()

    # Step 1: Search
    todo = search_edgar_filings()

    if not todo:
        total = db.get_filing_count()
        duration = time.time() - start_time
        print(f"\n  No new filings — {total} in database")
        return {
            "new_found": 0, "saved": 0, "failed": 0,
            "total_in_db": total, "duration_seconds": duration,
        }

    # Step 2: Download and process
    # Note: Using fewer threads because edgartools Filing objects do more
    # network requests per filing (sections, search, attachments)
    num_threads = min(config.NUM_THREADS, 4)
    print(f"\n  Processing {len(todo)} filings ({num_threads} threads)")
    print(f"  Using edgartools Filing objects for proper document parsing")

    saved = 0
    failed = 0

    with ThreadPoolExecutor(max_workers=num_threads) as executor:
        futures = {executor.submit(process_one_filing, item): item for item in todo}
        total = len(futures)

        for i, future in enumerate(as_completed(futures), 1):
            try:
                result = future.result(timeout=180)  # 3 min timeout per filing
            except Exception:
                result = None
                failed += 1

            if result:
                with _lock:
                    db.insert_filing(result)
                    saved += 1
                    if saved % 25 == 0:
                        db.commit()

            if progress_callback:
                progress_callback(i, total, saved, failed)

            if i % 20 == 0:
                print(f"    [{i}/{total}] saved={saved} failed={failed}")

    db.commit()

    duration = time.time() - start_time
    total_in_db = db.get_filing_count()
    print(f"\n  {saved} saved, {failed} failed")
    print(f"  {len(todo) - saved - failed} dropped (no crypto risk content)")
    print(f"  Total in database: {total_in_db}")
    print(f"  Duration: {duration:.0f}s ({duration/60:.1f}min)")

    return {
        "new_found": len(todo),
        "saved": saved,
        "failed": failed,
        "total_in_db": total_in_db,
        "duration_seconds": duration,
    }
