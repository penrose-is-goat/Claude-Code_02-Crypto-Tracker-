"""
SEC EDGAR Scraper for Crypto Filing Tracker.

Searches EDGAR full-text search for crypto-related filings,
downloads filing documents, extracts risk sections, and stores in database.
"""
import re
import time
import threading
import unicodedata
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup
from edgar import set_identity, search_filings

from . import config
from . import database as db
from .extractor import (
    html_to_structured_text,
    extract_risk_section,
    extract_risk_section_quick,
    get_filing_pdf_url,
    CRYPTO_RE,
    _score_crypto_risk,
)
from .summarizer import build_summary


set_identity(config.SEC_IDENTITY)

_lock = threading.Lock()


def _download_url(url):
    """Download a single document URL. Returns (raw_html, structured_text) or (None, None)."""
    try:
        r = requests.get(url, headers=config.SEC_HEADERS, timeout=config.HTTP_TIMEOUT)
        if r.status_code == 200 and len(r.text) > 500:
            raw_html = r.text
            text = html_to_structured_text(raw_html)
            return raw_html, text
    except Exception:
        pass
    return None, None


def _get_index_urls(filing_url):
    """Get all document URLs from a filing index page."""
    urls = []
    try:
        r = requests.get(filing_url, headers=config.SEC_HEADERS, timeout=15)
        if r.status_code != 200:
            return urls
        soup = BeautifulSoup(r.text, "html.parser")
        for row in soup.find_all("tr"):
            a = row.find("a")
            if a:
                href = a.get("href", "")
                if (
                    href.endswith((".htm", ".html"))
                    and "index" not in href.lower()
                    and "R1" not in href
                ):
                    full_url = (
                        f"https://www.sec.gov{href}" if href.startswith("/") else href
                    )
                    urls.append(full_url)
    except Exception:
        pass
    return urls


def _get_document_urls(filing_url):
    """Get document URLs including PDF links from a filing index page."""
    doc_urls = []
    pdf_url = ""
    try:
        r = requests.get(filing_url, headers=config.SEC_HEADERS, timeout=15)
        if r.status_code != 200:
            return doc_urls, pdf_url
        soup = BeautifulSoup(r.text, "html.parser")
        for row in soup.find_all("tr"):
            a = row.find("a")
            if not a:
                continue
            href = a.get("href", "")
            full_url = f"https://www.sec.gov{href}" if href.startswith("/") else href

            if href.endswith(".pdf") and not pdf_url:
                pdf_url = full_url
            elif (
                href.endswith((".htm", ".html"))
                and "index" not in href.lower()
                and "R1" not in href
            ):
                doc_urls.append(full_url)
    except Exception:
        pass
    return doc_urls, pdf_url


def _download_best_document(cik, accession_no):
    """Download filing documents and select the one with the best crypto risk section.

    KEY IMPROVEMENT: Scores each document individually by crypto*risk density.
    In multi-fund filings, the crypto fund's prospectus scores highest.
    Returns (best_raw_html, best_text, all_text_combined, pdf_url).
    """
    cik_clean = cik.lstrip("0")
    acc_clean = accession_no.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{cik_clean}/{acc_clean}"
    filing_url = f"{base}/{accession_no}-index.htm"

    doc_urls, pdf_url = _get_document_urls(filing_url)
    if not doc_urls:
        return None, None, None, pdf_url

    best_html = None
    best_text = None
    best_score = 0
    all_texts = []

    for url in doc_urls[: config.MAX_DOCS_PER_FILING]:
        raw_html, text = _download_url(url)
        if not text or len(text) < 300:
            continue
        all_texts.append(text)

        # Score this document's risk section
        risk_sec = extract_risk_section_quick(text)
        if risk_sec and len(risk_sec) > 800:
            score = _score_crypto_risk(risk_sec)
            if score > best_score:
                best_score = score
                best_html = raw_html
                best_text = text

        time.sleep(config.DOWNLOAD_DELAY)

    if not all_texts:
        return None, None, None, pdf_url

    # If we found a clearly best document, use it
    if best_text and best_score > 5:
        combined = "\n\n".join(all_texts)
        return best_html, best_text, combined, pdf_url

    # Otherwise combine all documents
    combined = "\n\n".join(all_texts)
    return None, combined, combined, pdf_url


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
    """Generate a short description of how this filing relates to crypto."""
    if not text:
        return "Crypto-related filing."
    tokens = list(set(t.lower() for t in CRYPTO_RE.findall(text[:3000])))[:5]
    if not tokens:
        return "Crypto-related filing."

    joined = " or ".join(tokens)
    if root_form in config.ETF_FUND_FORMS:
        return f"Proposes {joined} ETF, fund, or security offering."
    elif root_form in ("10-K", "10-Q"):
        return f"Reports {joined} operations with risk disclosures."
    else:
        return f"Discloses {joined} material event."


def process_one_filing(item):
    """Process a single filing: download, extract risk section, build summary.

    Args:
        item: tuple of (accession_no, cik, company_name, ticker, form_type,
              root_form, filing_date, tier, keyword)

    Returns:
        dict of filing data ready for database insertion, or None on failure.
    """
    acc, cik, company_name, ticker, form_type, root_form, filed, tier, kw = item

    try:
        best_html, best_text, all_text, pdf_url = _download_best_document(cik, acc)

        text = best_text or all_text
        if not text or len(text) < 300:
            return None

        # Extract risk section — try HTML-aware first, then text-based
        risk_section = ""
        if best_html:
            risk_section = extract_risk_section(raw_html=best_html)
        if not risk_section or len(risk_section) < 500:
            risk_section = extract_risk_section(plain_text=text)

        # Build summary from risk section (or full text as fallback)
        source_text = risk_section if risk_section else text
        summary = build_summary(source_text)
        if not summary:
            return None

        # Guarantee dropdown content — triple fallback
        if not risk_section or len(risk_section) < 150:
            risk_section = extract_risk_section(plain_text=all_text or text)
        if not risk_section or len(risk_section) < 80:
            match = CRYPTO_RE.search(text)
            if match:
                start = max(0, match.start() - 500)
                risk_section = text[start : start + 10000]
            else:
                risk_section = text[:10000]

        crypto_connection = _detect_crypto_connection(text, root_form)
        sec_url = get_filing_pdf_url(cik, acc)
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
            "text_length": len(text),
            "risk_summary": _clean_text(summary),
            "risk_section": risk_section[:80000],
            "crypto_connection": _clean_text(crypto_connection),
            "sec_url": sec_url,
            "filing_pdf_url": pdf_url or "",
            "search_keyword": kw,
            "n_risk_paras": risk_section.count("\n\n") + 1 if risk_section else 0,
            "processed_at": datetime.now().isoformat(),
        }

    except Exception:
        return None


def search_edgar_filings():
    """Search SEC EDGAR for crypto-related filings across all form types and keywords.

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
                    tier = 1 if rf in config.ETF_FUND_FORMS else 2
                    ck = str(r.cik or "").lstrip("0")
                    fd = str(r.filed) if r.filed else ""

                    todo.append((acc, ck, company_name, ticker, fa, rf, fd, tier, kw))
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

    Args:
        progress_callback: Optional callable(current, total, saved, failed)
            for progress updates.

    Returns:
        dict with stats: {new_found, saved, failed, total_in_db}
    """
    db.init_db()

    # Step 1: Search
    todo = search_edgar_filings()

    if not todo:
        total = db.get_filing_count()
        print(f"\n  No new filings — {total} in database")
        return {"new_found": 0, "saved": 0, "failed": 0, "total_in_db": total}

    # Step 2: Download and process in parallel
    print(f"\n  Downloading + analyzing ({len(todo)} filings, {config.NUM_THREADS} threads)")

    saved = 0
    failed = 0

    with ThreadPoolExecutor(max_workers=config.NUM_THREADS) as executor:
        futures = {executor.submit(process_one_filing, item): item for item in todo}
        total = len(futures)

        for i, future in enumerate(as_completed(futures), 1):
            try:
                result = future.result(timeout=120)
            except Exception:
                result = None
                failed += 1

            if result:
                with _lock:
                    db.insert_filing(result)
                    saved += 1
                    if saved % 50 == 0:
                        db.commit()

            if progress_callback:
                progress_callback(i, total, saved, failed)

            if i % 25 == 0:
                print(f"    [{i}/{total}] saved={saved} failed={failed}")

    db.commit()

    total_in_db = db.get_filing_count()
    print(f"\n  {saved} saved, {failed} failed")
    print(f"  {len(todo) - saved - failed} dropped (no crypto risk content)")
    print(f"  Total in database: {total_in_db}")

    return {
        "new_found": len(todo),
        "saved": saved,
        "failed": failed,
        "total_in_db": total_in_db,
    }
