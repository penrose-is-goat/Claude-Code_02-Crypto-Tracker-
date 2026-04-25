"""
Risk Section Extractor for SEC filings.

KEY IMPROVEMENT over v24: Instead of converting HTML to flat text and regex-matching
headers, this module works with the HTML DOM structure directly. For multi-fund
filings (485APOS, N-1A), this correctly identifies the crypto fund's risk section
rather than grabbing a bond fund's section or the entire prospectus.

Approach:
1. Parse the HTML and identify structural sections via heading tags
2. Score each section by CRYPTO * RISK keyword density
3. Extract the highest-scoring section with proper boundaries
4. Fall back to text-based extraction for non-standard formats
"""
import re
import math
import html as html_mod
from bs4 import BeautifulSoup, NavigableString, Tag

# ─── Compiled Regex Patterns ─────────────────────────────────────────────

# Crypto-related keywords
CRYPTO_RE = re.compile(
    r"\b("
    r"cryptocurrency|cryptocurrencies|blockchain|digital asset|digital assets|"
    r"bitcoin|btc|ethereum|eth|XRP|stablecoin|stablecoins|crypto|DeFi|NFT|"
    r"mining|proof.of.work|proof.of.stake|decentralized finance|"
    r"virtual currency|virtual currencies|digital currency|"
    r"crypto.?asset|crypto.?assets|digital token|tokenized|"
    r"distributed ledger|smart contract|altcoin|satoshi"
    r")\b", re.I
)

# Risk-related keywords
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

# Boilerplate / non-content indicators
BOILERPLATE_RE = re.compile(
    r"("
    r"table of contents|date of this prospectus|criminal offense|"
    r"approved or disapproved|bookrunner|delivery of the securities|"
    r"incorporation by reference|exhibits? \d|EX-\d|pursuant to|"
    r"filed herewith|XBRL|iXBRL|EDGAR Online|LIVE \d{10}"
    r")", re.I
)

# Risk section header patterns
RISK_HEADERS = [
    "RISK FACTORS", "Risk Factors",
    "PRINCIPAL INVESTMENT RISKS", "Principal Investment Risks",
    "PRINCIPAL RISKS", "Principal Risks",
    "INVESTMENT RISKS", "Investment Risks",
    "KEY RISK FACTORS", "Key Risk Factors",
    "Risks Related to", "RISKS RELATED TO",
    "Risks of Investing in the Fund", "Risks of Investing in the Trust",
    "SUMMARY OF PRINCIPAL RISKS", "Summary of Principal Risks",
    "Principal Investment Risks of an Investment",
    "RISKS OF INVESTING",
]

RISK_HEADER_RE = re.compile(
    r"(?:RISK\s*FACTORS|PRINCIPAL\s+(?:INVESTMENT\s+)?RISKS|"
    r"INVESTMENT\s+RISKS|KEY\s+RISK\s+FACTORS|"
    r"RISKS?\s+(?:RELATED\s+TO|OF\s+INVESTING))",
    re.I
)

# Section boundary patterns — these mark the END of a risk section
SECTION_END_RE = re.compile(
    r"\n\s*(?:"
    # 10-K / S-1 standard sections
    r"Item\s*\d|ITEM\s*\d"
    r"|PART\s*(?:II|III|IV)|Part\s*(?:II|III|IV)"
    r"|USE OF PROCEEDS|DESCRIPTION OF (?:THE |CAPITAL)"
    r"|MANAGEMENT.S DISCUSSION|SECURITY OWNERSHIP"
    r"|SELLING|PLAN OF DISTRIBUTION"
    r"|EXPERTS|LEGAL MATTERS|INDEX TO FINANCIAL"
    r"|UNAUDITED|CAUTIONARY NOTE"
    # N-1A / 485 / Fund-specific sections
    r"|PURCHASE AND (?:SALE|REDEMPTION) OF (?:FUND |TRUST )?SHARES"
    r"|TAX INFORMATION|TAX CONSEQUENCES"
    r"|PAYMENTS TO BROKER.DEALERS|PAYMENTS TO FINANCIAL INTERMEDIARIES"
    r"|FINANCIAL HIGHLIGHTS|FEE TABLE|FEES AND EXPENSES"
    r"|SHAREHOLDER INFORMATION|SHAREHOLDER SERVICES"
    r"|HOW TO (?:PURCHASE|BUY|SELL|REDEEM)"
    r"|INVESTMENT OBJECTIVE|INVESTMENT STRATEG"
    r"|FUND SUMMARY|FUND PERFORMANCE|PERFORMANCE INFORMATION"
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
    r"|OTHER (?:INFORMATION|RISKS|CONSIDERATIONS)"
    # General prospectus sections
    r"|SIGNATURES|EXHIBIT INDEX|EXHIBITS?"
    r"|REPORT OF INDEPENDENT|FINANCIAL STATEMENTS"
    r"|NOTES TO (?:THE )?(?:CONSOLIDATED )?FINANCIAL"
    r"|SELECTED FINANCIAL DATA"
    r"|PROPERTIES|BUSINESS OVERVIEW"
    r")", re.I
)

# Block-level HTML tags for paragraph-preserving conversion
BLOCK_TAGS = {
    "p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "th", "td",
    "blockquote", "section", "article", "header", "footer", "dt", "dd",
    "figcaption", "caption", "pre", "address", "ol", "ul", "table",
}

HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}


def html_to_structured_text(raw_html):
    """Convert HTML to text preserving paragraph structure.
    Inserts double newlines at block boundaries instead of using get_text()."""
    soup = BeautifulSoup(raw_html, "html.parser")

    for tag in soup.find_all(["script", "style", "head", "meta", "link", "noscript"]):
        tag.decompose()

    for tag in soup.find_all(BLOCK_TAGS):
        tag.insert_before("\n\n")
        tag.append("\n")

    for br in soup.find_all("br"):
        br.replace_with("\n")

    text = soup.get_text()

    # Normalize whitespace while keeping paragraph breaks
    text = re.sub(r"[^\S\n]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = re.sub(r"\n\d{1,3}\n", "\n", text)          # page numbers
    text = re.sub(r"\nTable of Contents\n", "\n", text, flags=re.I)

    return text.strip()


def _score_crypto_risk(text, sample_size=15000):
    """Score a chunk of text by CRYPTO * RISK keyword density.
    Returns 0 for sections with no crypto keywords (e.g. bond fund risk)."""
    sample = text[:sample_size].lower()
    crypto_hits = len(CRYPTO_RE.findall(sample))
    risk_hits = len(RISK_RE.findall(sample))
    boilerplate_hits = len(BOILERPLATE_RE.findall(sample))

    if crypto_hits == 0:
        return 0

    return crypto_hits * max(risk_hits - boilerplate_hits * 2, 1)


# ═══════════════════════════════════════════════════════════════════════════
# APPROACH 1: HTML-AWARE EXTRACTION (preferred)
# Parse the DOM and find heading elements that match risk patterns,
# then extract content until the next same-level or higher heading.
# ═══════════════════════════════════════════════════════════════════════════

def _find_risk_sections_from_html(raw_html):
    """Parse HTML structure to find risk sections using heading tags.
    Returns list of (score, text) tuples."""
    soup = BeautifulSoup(raw_html, "html.parser")

    for tag in soup.find_all(["script", "style", "head", "meta", "link", "noscript"]):
        tag.decompose()

    candidates = []

    # Find all heading tags and bold/strong elements that match risk header patterns
    potential_headers = []

    for tag in soup.find_all(list(HEADING_TAGS) + ["b", "strong", "font"]):
        tag_text = tag.get_text(strip=True)
        if not tag_text or len(tag_text) < 5:
            continue
        if RISK_HEADER_RE.search(tag_text):
            # Determine heading level (h1=1, h2=2, ..., bold=7)
            if tag.name and tag.name.startswith("h"):
                level = int(tag.name[1])
            else:
                level = 7
            potential_headers.append((tag, tag_text, level))

    for header_tag, header_text, level in potential_headers:
        # Skip cross-references like "See Risk Factors on page 12"
        context = header_tag.get_text()
        parent_text = ""
        if header_tag.parent:
            parent_text = header_tag.parent.get_text(strip=True)[:200].lower()
        if "see " in parent_text[:30] or "refer to" in parent_text[:30]:
            continue
        if "beginning on page" in parent_text or "on page" in parent_text[:80]:
            continue

        # Collect all content after this heading until next same-level-or-higher heading
        content_parts = []
        total_len = 0
        max_chars = 120000

        sibling = header_tag
        # First, try to get content from subsequent siblings of the header's parent
        # This handles: <h2>Risk Factors</h2><p>text</p><p>text</p>...
        if header_tag.name in HEADING_TAGS:
            for sib in header_tag.find_next_siblings():
                if isinstance(sib, Tag):
                    # Stop at next heading of same or higher level
                    if sib.name in HEADING_TAGS:
                        sib_level = int(sib.name[1])
                        if sib_level <= level:
                            break
                        # Check if this lower heading is a non-risk section
                        sib_text = sib.get_text(strip=True)
                        if SECTION_END_RE.search("\n" + sib_text):
                            break

                    part_text = sib.get_text(separator="\n", strip=True)
                    if part_text:
                        content_parts.append(part_text)
                        total_len += len(part_text)

                if total_len > max_chars:
                    break
        else:
            # For bold/strong headers, extract text from following elements
            element = header_tag.parent if header_tag.parent else header_tag
            for sib in element.find_next_siblings():
                if isinstance(sib, Tag):
                    sib_text = sib.get_text(strip=True)
                    # Stop if we hit another major section header
                    if sib.name in HEADING_TAGS:
                        break
                    if SECTION_END_RE.search("\n" + sib_text):
                        break
                    if sib_text:
                        content_parts.append(sib_text)
                        total_len += len(sib_text)
                if total_len > max_chars:
                    break

        section_text = "\n\n".join(content_parts)
        if len(section_text) < 500:
            continue

        score = _score_crypto_risk(section_text)
        if score > 0:
            candidates.append((score, section_text))

    return candidates


# ═══════════════════════════════════════════════════════════════════════════
# APPROACH 2: TEXT-BASED EXTRACTION (fallback)
# Convert to text first, then find sections by string matching.
# Improved from v24 with better boundary detection.
# ═══════════════════════════════════════════════════════════════════════════

def _find_risk_sections_from_text(text):
    """Find risk sections in plain text using header string matching.
    Returns list of (score, text) tuples."""
    if not text or len(text) < 500:
        return []

    candidates = []
    for hdr in RISK_HEADERS:
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
            end_match = SECTION_END_RE.search(text, content_start + 300)
            content_end = end_match.start() if end_match else min(content_start + 80000, len(text))

            section = text[content_start:content_end].strip()
            pos = content_start

            if len(section) < 800:
                continue

            score = _score_crypto_risk(section)
            if score > 0:
                # Add small length bonus (diminishing returns)
                length_bonus = min(math.log(len(section) + 1), 12)
                candidates.append((score + length_bonus, section))

    return candidates


def _extract_crypto_paragraphs(text, max_chars=80000):
    """Last-resort fallback: scan document for paragraphs containing both
    crypto AND risk language. Returns them as flowing text."""
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
        if len(p) < 50:
            continue
        if BOILERPLATE_RE.search(p):
            continue
        crypto_hits = len(CRYPTO_RE.findall(p))
        risk_hits = len(RISK_RE.findall(p))
        if crypto_hits > 0 and risk_hits > 0:
            scored.append((crypto_hits * risk_hits, p))
            total += len(p)
        if total > max_chars:
            break

    scored.sort(key=lambda x: -x[0])
    if not scored:
        return ""
    return "\n\n".join(p for _, p in scored)


# ═══════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ═══════════════════════════════════════════════════════════════════════════

def extract_risk_section(raw_html=None, plain_text=None):
    """Extract the crypto-relevant risk section from a filing.

    Tries HTML-aware extraction first (better for structured filings),
    falls back to text-based extraction, then to paragraph scanning.

    Args:
        raw_html: The raw HTML of the filing document
        plain_text: Pre-converted plain text (used if raw_html not available)

    Returns:
        str: The extracted risk section text, or empty string if not found.
    """
    candidates = []

    # Try HTML-aware extraction first (much better for multi-fund filings)
    if raw_html:
        candidates = _find_risk_sections_from_html(raw_html)

    # If HTML extraction didn't find anything, try text-based
    if not candidates:
        text = plain_text or (html_to_structured_text(raw_html) if raw_html else "")
        if text:
            candidates = _find_risk_sections_from_text(text)

    # Pick the best candidate
    if candidates:
        candidates.sort(key=lambda x: -x[0])
        best = candidates[0][1]
        if len(best) > 500:
            return best

    # Last resort: paragraph-level scan
    text = plain_text or (html_to_structured_text(raw_html) if raw_html else "")
    if text:
        return _extract_crypto_paragraphs(text)

    return ""


def extract_risk_section_quick(text):
    """Quick risk section extraction for document scoring during download.
    Text-only (no HTML parsing), used to compare documents."""
    if not text or len(text) < 500:
        return ""
    candidates = _find_risk_sections_from_text(text)
    if candidates:
        candidates.sort(key=lambda x: -x[0])
        return candidates[0][1]
    return ""


def format_risk_for_html(text):
    """Format risk section plain text into styled HTML that mirrors
    the actual filing's structure. Preserves headers, lists, and flow."""
    if not text:
        return ""

    # Clean up noise
    text = re.sub(r"\n\s*\d{1,3}\s*\n", "\n", text)
    text = re.sub(r"\nTable of Contents\n", "\n", text, flags=re.I)

    paragraphs = re.split(r"\n\s*\n", text)
    html_parts = []

    for p in paragraphs:
        p = p.strip()
        if not p or len(p) < 5:
            continue

        # Detect sub-headers: short, no trailing period, has uppercase
        is_header = (
            len(p) < 120
            and not p.endswith(".")
            and not p.endswith(",")
            and re.search(r"[A-Z]", p)
            and not BOILERPLATE_RE.search(p)
        )

        escaped = html_mod.escape(p)

        # Handle line breaks within a paragraph
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


def get_filing_pdf_url(cik, accession_no):
    """Construct URL to the filing's primary document on SEC.gov."""
    cik_clean = str(cik).lstrip("0")
    acc_clean = accession_no.replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/"
        f"{cik_clean}/{acc_clean}/{accession_no}-index.htm"
    )
