"""
Risk Section Extractor v1.1.26 — Complete rewrite using edgartools.

v24/v25 FAILURE DIAGNOSIS:
- Used raw HTTP to download filing documents, often getting the wrong document
- Converted HTML to flat text with regex, losing all structural information
- Used string matching on headers, missing risk sections or grabbing fee schedules
- For multi-fund filings, couldn't distinguish crypto fund from bond fund sections

v1.1.26 APPROACH:
1. Use edgartools Filing object (Filing.html(), Filing.sections(), Filing.search())
   for proper document parsing instead of raw HTTP
2. For multi-fund filings, iterate ALL attachments and score by crypto relevance
3. Use Filing.sections() to get clean text chunks, then find risk-related sections
4. Validate extracted risk section actually discusses crypto before accepting
5. For non-risk filings (8-K, D, agreements), extract the full document body
6. Store raw HTML for the risk section to preserve formatting
"""
import re
import math
import html as html_mod
from typing import Optional, Tuple, List

# ─── Compiled Regex Patterns ─────────────────────────────────────────────

CRYPTO_RE = re.compile(
    r"\b("
    r"cryptocurrency|cryptocurrencies|blockchain|digital asset|digital assets|"
    r"bitcoin|btc|ethereum|eth|XRP|stablecoin|stablecoins|crypto|DeFi|NFT|"
    r"mining pool|proof.of.work|proof.of.stake|decentralized finance|"
    r"virtual currency|virtual currencies|digital currency|"
    r"crypto.?asset|crypto.?assets|digital token|tokenized|"
    r"distributed ledger|smart contract|altcoin|satoshi|"
    r"binance|coinbase|kraken|gemini|bitfinex"
    r")\b", re.I
)

RISK_RE = re.compile(
    r"\b("
    r"risk|loss|adverse|volatile|volatility|uncertain|decline|fail|failure|"
    r"liability|penalty|harm|impair|no assurance|could result|may not|"
    r"subject to|negatively|negative impact|material adverse|you could lose|"
    r"speculative|cybersecurity|hack|theft|fraud|manipulation|regulatory|"
    r"compliance|forfeiture|prohibit|restrict|enforce|sanctions|"
    r"illiquid|default|lawsuit|litigation|investigation"
    r")\b", re.I
)

BOILERPLATE_RE = re.compile(
    r"("
    r"table of contents|date of this prospectus|criminal offense|"
    r"approved or disapproved|bookrunner|delivery of the securities|"
    r"incorporation by reference|exhibits? \d|EX-\d|pursuant to|"
    r"filed herewith|XBRL|iXBRL|EDGAR Online|LIVE \d{10}"
    r")", re.I
)

# Risk section header patterns — what we're looking for
RISK_HEADER_RE = re.compile(
    r"(?:^|\n)\s*(?:"
    r"RISK\s*FACTORS"
    r"|PRINCIPAL\s+(?:INVESTMENT\s+)?RISKS"
    r"|INVESTMENT\s+RISKS"
    r"|KEY\s+RISK\s+FACTORS"
    r"|RISKS?\s+(?:RELATED\s+TO|OF\s+INVESTING)"
    r"|SUMMARY\s+OF\s+PRINCIPAL\s+RISKS"
    r")\b",
    re.I
)

# Section headers that mark the END of a risk section
SECTION_END_RE = re.compile(
    r"(?:^|\n)\s*(?:"
    r"Item\s*\d|ITEM\s*\d"
    r"|PART\s*(?:II|III|IV)"
    r"|USE OF PROCEEDS"
    r"|DESCRIPTION OF (?:THE |CAPITAL)"
    r"|MANAGEMENT.S DISCUSSION"
    r"|SECURITY OWNERSHIP"
    r"|PLAN OF DISTRIBUTION"
    r"|EXPERTS|LEGAL MATTERS"
    r"|INDEX TO FINANCIAL"
    r"|PURCHASE AND (?:SALE|REDEMPTION) OF (?:FUND |TRUST )?SHARES"
    r"|TAX INFORMATION|TAX CONSEQUENCES"
    r"|PAYMENTS TO BROKER.DEALERS|PAYMENTS TO FINANCIAL INTERMEDIARIES"
    r"|FINANCIAL HIGHLIGHTS|FEE TABLE|FEES AND EXPENSES"
    r"|SHAREHOLDER INFORMATION"
    r"|HOW TO (?:PURCHASE|BUY|SELL|REDEEM)"
    r"|INVESTMENT OBJECTIVE|INVESTMENT STRATEG"
    r"|FUND SUMMARY|FUND PERFORMANCE"
    r"|ADDITIONAL INFORMATION (?:ABOUT|REGARDING)"
    r"|DISTRIBUTION AND SERVICING|DISTRIBUTION PLAN"
    r"|PORTFOLIO HOLDINGS|PORTFOLIO TURNOVER"
    r"|CREATION AND REDEMPTION|CREATION UNITS"
    r"|MANAGEMENT OF THE (?:FUND|TRUST)"
    r"|ORGANIZATION OF THE (?:FUND|TRUST)"
    r"|DETERMINATION OF NET ASSET VALUE"
    r"|DIVIDENDS.? DISTRIBUTIONS"
    r"|ABOUT THE (?:FUND|TRUST|INDEX)"
    r"|INVESTMENT (?:POLICIES|RESTRICTIONS|LIMITATIONS)"
    r"|DESCRIPTION OF THE (?:INDEX|BENCHMARK)"
    r"|SIGNATURES|EXHIBIT INDEX|EXHIBITS?"
    r"|REPORT OF INDEPENDENT|FINANCIAL STATEMENTS"
    r"|NOTES TO (?:THE )?(?:CONSOLIDATED )?FINANCIAL"
    r"|SELECTED FINANCIAL DATA"
    r"|PROPERTIES|BUSINESS OVERVIEW"
    r")\b",
    re.I | re.MULTILINE
)

# Fee/service schedule indicators — NOT risk sections
FEE_SCHEDULE_RE = re.compile(
    r"\b("
    r"fee schedule|fee table|base fee|transaction fee|"
    r"annual fee|monthly fee|service fee|custody fee|"
    r"pricing schedule|compensation schedule|"
    r"rate schedule|billing"
    r")\b", re.I
)


def _score_crypto_risk(text, sample_size=20000):
    """Score text by CRYPTO * RISK keyword density.
    Returns 0 for sections with no crypto keywords."""
    if not text:
        return 0
    sample = text[:sample_size].lower()
    crypto_hits = len(CRYPTO_RE.findall(sample))
    risk_hits = len(RISK_RE.findall(sample))
    boilerplate_hits = len(BOILERPLATE_RE.findall(sample))
    fee_hits = len(FEE_SCHEDULE_RE.findall(sample))

    if crypto_hits == 0:
        return 0
    # Heavily penalize fee schedules
    penalty = (boilerplate_hits * 2) + (fee_hits * 10)
    return crypto_hits * max(risk_hits - penalty, 1)


def _has_sufficient_crypto_content(text, threshold=3):
    """Check if text actually discusses crypto (not just a passing mention)."""
    if not text:
        return False
    sample = text[:30000].lower()
    return len(CRYPTO_RE.findall(sample)) >= threshold


def _is_fee_schedule(text):
    """Check if text is a fee schedule rather than a risk section."""
    if not text:
        return False
    sample = text[:5000].lower()
    fee_hits = len(FEE_SCHEDULE_RE.findall(sample))
    risk_hits = len(RISK_RE.findall(sample))
    return fee_hits > risk_hits


# ═══════════════════════════════════════════════════════════════════════════
# PRIMARY APPROACH: Use edgartools Filing object
# ═══════════════════════════════════════════════════════════════════════════

def extract_risk_from_filing(filing) -> Tuple[str, str]:
    """Extract risk section from an edgartools Filing object.

    Uses Filing.sections() for section parsing and Filing.search() for
    finding risk-related content. Falls back to text-based extraction.

    Args:
        filing: edgartools Filing object

    Returns:
        Tuple of (risk_section_text, full_document_text)
    """
    full_text = ""
    risk_text = ""

    try:
        # Get the full text of the primary document
        full_text = filing.text() or ""
    except Exception:
        pass

    if not full_text or len(full_text) < 200:
        # Try getting HTML and converting
        try:
            html_content = filing.html()
            if html_content:
                full_text = _html_to_text(html_content)
        except Exception:
            pass

    if not full_text or len(full_text) < 200:
        return "", ""

    # APPROACH 1: Use Filing.sections() to find risk section
    try:
        sections = filing.sections()
        if sections:
            risk_text = _find_risk_in_sections(sections)
    except Exception:
        pass

    # APPROACH 2: Use Filing.search() to find risk content
    if not risk_text or len(risk_text) < 500:
        try:
            search_results = filing.search("risk factors principal investment risks")
            if search_results:
                # search returns ranked results - take top ones
                risk_chunks = []
                for result in search_results[:10]:
                    chunk = str(result) if not isinstance(result, str) else result
                    if _has_sufficient_crypto_content(chunk):
                        risk_chunks.append(chunk)
                if risk_chunks:
                    risk_text = "\n\n".join(risk_chunks)
        except Exception:
            pass

    # APPROACH 3: Text-based extraction (fallback)
    if not risk_text or len(risk_text) < 500:
        risk_text = _extract_risk_from_text(full_text)

    # APPROACH 4: For multi-document filings, check other attachments
    if not risk_text or len(risk_text) < 500 or not _has_sufficient_crypto_content(risk_text):
        try:
            alt_risk, alt_full = _search_attachments_for_risk(filing)
            if alt_risk and len(alt_risk) > len(risk_text or ""):
                if _has_sufficient_crypto_content(alt_risk):
                    risk_text = alt_risk
                    if alt_full:
                        full_text = alt_full
        except Exception:
            pass

    # VALIDATION: Does the extracted risk section actually discuss crypto?
    if risk_text and not _has_sufficient_crypto_content(risk_text, threshold=2):
        # This might be the wrong fund's risk section (e.g., rare earth mining)
        # Try paragraph-level extraction instead
        crypto_paragraphs = _extract_crypto_paragraphs(full_text)
        if crypto_paragraphs and len(crypto_paragraphs) > 200:
            risk_text = crypto_paragraphs

    # If we still have nothing, check if the filing is a non-risk document
    # (8-K, D form, agreement) and extract the body instead
    if not risk_text or len(risk_text) < 100:
        risk_text = _extract_document_body(full_text)

    return risk_text, full_text


def _search_attachments_for_risk(filing) -> Tuple[str, str]:
    """Search through ALL filing attachments for the one with the best
    crypto risk section. Critical for multi-fund filings (485APOS, N-1A)
    where the crypto fund may not be the primary document."""
    best_risk = ""
    best_full = ""
    best_score = 0

    try:
        attachments = filing.attachments
        if not attachments:
            return "", ""

        documents = attachments.documents
        if not documents:
            return "", ""

        for doc in documents[:12]:  # Check up to 12 documents
            try:
                if doc.is_binary:
                    continue

                text = doc.text()
                if not text or len(text) < 500:
                    continue

                # Extract risk section from this document
                risk = _extract_risk_from_text(text)
                if risk and len(risk) > 500:
                    score = _score_crypto_risk(risk)
                    if score > best_score and not _is_fee_schedule(risk):
                        best_score = score
                        best_risk = risk
                        best_full = text

            except Exception:
                continue

    except Exception:
        pass

    return best_risk, best_full


def _find_risk_in_sections(sections) -> str:
    """Find the risk section among edgartools-parsed sections."""
    if not sections:
        return ""

    candidates = []

    for i, section in enumerate(sections):
        if not isinstance(section, str):
            section = str(section)

        # Check if this section contains a risk header
        if not RISK_HEADER_RE.search(section[:500]):
            continue

        # Skip fee schedules
        if _is_fee_schedule(section):
            continue

        score = _score_crypto_risk(section)
        if score > 0:
            candidates.append((score, section))

    if not candidates:
        # No section had a risk header + crypto content
        # Try looking for sections with high crypto+risk density
        for section in sections:
            if not isinstance(section, str):
                section = str(section)
            if len(section) < 500:
                continue
            score = _score_crypto_risk(section)
            if score > 10 and not _is_fee_schedule(section):
                candidates.append((score, section))

    if candidates:
        candidates.sort(key=lambda x: -x[0])
        return candidates[0][1]

    return ""


# ═══════════════════════════════════════════════════════════════════════════
# TEXT-BASED EXTRACTION (fallback for when edgartools methods fail)
# ═══════════════════════════════════════════════════════════════════════════

def _extract_risk_from_text(text) -> str:
    """Find risk sections in plain text using header matching.
    Improved with crypto validation and fee schedule rejection."""
    if not text or len(text) < 500:
        return ""

    candidates = []
    risk_headers = [
        "RISK FACTORS", "Risk Factors",
        "PRINCIPAL INVESTMENT RISKS", "Principal Investment Risks",
        "PRINCIPAL RISKS", "Principal Risks",
        "INVESTMENT RISKS", "Investment Risks",
        "KEY RISK FACTORS", "Key Risk Factors",
        "Risks Related to", "RISKS RELATED TO",
        "Risks of Investing in the Fund", "Risks of Investing in the Trust",
        "SUMMARY OF PRINCIPAL RISKS", "Summary of Principal Risks",
        "RISKS OF INVESTING",
    ]

    for hdr in risk_headers:
        pos = 0
        while True:
            pos = text.find(hdr, pos)
            if pos == -1:
                break
            content_start = pos + len(hdr)

            # Skip cross-references
            context_before = text[max(0, pos - 80):pos].lower()
            if "see " in context_before or "refer to" in context_before:
                pos = content_start
                continue
            context_after = text[content_start:content_start + 120].lower()
            if "beginning on page" in context_after or "on page" in context_after:
                pos = content_start
                continue

            # Find section end
            end_match = SECTION_END_RE.search(text[content_start + 500:])
            if end_match:
                content_end = content_start + 500 + end_match.start()
            else:
                content_end = min(content_start + 120000, len(text))

            section = text[content_start:content_end].strip()
            pos = content_start

            if len(section) < 500:
                continue

            # Skip fee schedules
            if _is_fee_schedule(section):
                continue

            score = _score_crypto_risk(section)
            if score > 0:
                candidates.append((score, section))

    if candidates:
        candidates.sort(key=lambda x: -x[0])
        return candidates[0][1]

    return ""


def _extract_crypto_paragraphs(text, max_chars=80000) -> str:
    """Extract paragraphs containing both crypto AND risk language."""
    paragraphs = re.split(r"\n\s*\n", text)
    if len(paragraphs) < 10:
        sentences = re.split(r"(?<=[.!?])\s+", text)
        paragraphs = []
        chunk = ""
        for s in sentences:
            chunk += s + " "
            if len(chunk) > 400:
                paragraphs.append(chunk.strip())
                chunk = ""
        if chunk.strip():
            paragraphs.append(chunk.strip())

    scored = []
    total = 0
    for p in paragraphs:
        p = p.strip()
        if len(p) < 50 or BOILERPLATE_RE.search(p):
            continue
        c = len(CRYPTO_RE.findall(p))
        r = len(RISK_RE.findall(p))
        if c > 0 and r > 0:
            scored.append((c * r, p))
            total += len(p)
        if total > max_chars:
            break

    scored.sort(key=lambda x: -x[0])
    return "\n\n".join(p for _, p in scored) if scored else ""


def _extract_document_body(text, max_chars=50000) -> str:
    """For non-risk filings (8-K, agreements, etc.), extract the document body
    minus cover pages, exhibits, and EDGAR boilerplate."""
    if not text:
        return ""

    lines = text.split("\n")
    body_lines = []
    in_body = False
    skip_count = 0

    for line in lines:
        stripped = line.strip()

        # Skip EDGAR header/cover page content
        if not in_body:
            if any(marker in stripped.upper() for marker in [
                "UNITED STATES SECURITIES", "FORM ", "PURSUANT TO",
                "COMMISSION FILE", "REGISTRANT", "DATE OF REPORT",
            ]):
                skip_count += 1
                if skip_count > 3:
                    in_body = True
                continue
            if len(stripped) > 100 and not BOILERPLATE_RE.search(stripped):
                in_body = True

        if in_body:
            if BOILERPLATE_RE.search(stripped):
                continue
            # Stop at exhibits/signatures
            if re.match(r"^\s*(EXHIBIT|SIGNATURE|EX-\d)", stripped, re.I):
                break
            body_lines.append(line)
            if sum(len(l) for l in body_lines) > max_chars:
                break

    return "\n".join(body_lines).strip() if body_lines else text[:max_chars]


def _html_to_text(raw_html) -> str:
    """Convert HTML to text preserving paragraph structure."""
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(raw_html, "html.parser")
        for tag in soup.find_all(["script", "style", "head", "meta", "link", "noscript"]):
            tag.decompose()
        for tag in soup.find_all(["p", "div", "h1", "h2", "h3", "h4", "h5", "h6",
                                   "li", "tr", "blockquote", "section"]):
            tag.insert_before("\n\n")
            tag.append("\n")
        for br in soup.find_all("br"):
            br.replace_with("\n")
        text = soup.get_text()
        text = re.sub(r"[^\S\n]+", " ", text)
        text = re.sub(r" *\n *", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()
    except Exception:
        return re.sub(r"<[^>]+>", " ", raw_html)


# ═══════════════════════════════════════════════════════════════════════════
# HTML FORMATTING (for display in the web app)
# ═══════════════════════════════════════════════════════════════════════════

def format_risk_for_html(text) -> str:
    """Format risk section text into HTML preserving the filing's structure."""
    if not text:
        return ""

    text = re.sub(r"\n\s*\d{1,3}\s*\n", "\n", text)
    text = re.sub(r"\nTable of Contents\n", "\n", text, flags=re.I)

    paragraphs = re.split(r"\n\s*\n", text)
    html_parts = []

    for p in paragraphs:
        p = p.strip()
        if not p or len(p) < 5:
            continue

        is_header = (
            len(p) < 120
            and not p.endswith(".")
            and not p.endswith(",")
            and re.search(r"[A-Z]", p)
            and not BOILERPLATE_RE.search(p)
            and not FEE_SCHEDULE_RE.search(p)
        )

        escaped = html_mod.escape(p)
        lines = escaped.split("\n")
        if len(lines) > 1:
            has_bullets = sum(
                1 for ln in lines
                if re.match(r"^\s*[\u2022\u25CF\-\*\(\d]", ln.strip())
            ) > len(lines) * 0.3
            if has_bullets:
                escaped = "<br>".join(ln.strip() for ln in lines if ln.strip())
            else:
                escaped = " ".join(ln.strip() for ln in lines if ln.strip())

        if is_header:
            html_parts.append(
                f'<div style="font-weight:700;font-size:11.5px;color:#e2e8f0;'
                f"margin:14px 0 4px 0;padding-bottom:2px;"
                f'border-bottom:1px solid #1e293b;">{escaped}</div>'
            )
        else:
            html_parts.append(
                f'<p style="margin:6px 0;text-indent:0;line-height:1.75;">'
                f"{escaped}</p>"
            )

    return "".join(html_parts)


def get_filing_url(cik, accession_no) -> str:
    """Construct URL to the filing index page on SEC.gov."""
    cik_clean = str(cik).lstrip("0")
    acc_clean = accession_no.replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/"
        f"{cik_clean}/{acc_clean}/{accession_no}-index.htm"
    )
