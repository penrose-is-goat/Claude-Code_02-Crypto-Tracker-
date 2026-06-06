"""
Risk Section Extractor v1.1.35.

v1.1.35 changes:
- Prefer exact source-text section extraction before edgartools object parsing
- Return source offsets and text hashes for auditability and summary caching
- Preserve filing words/punctuation while still offering display formatting

v1.1.30 performance changes:
- BS4 parser switched from html.parser → lxml (2-3× faster)
- Text-cleaning cache: sha256(html) → cleaned text on disk, so the
  expensive BS4 parse only runs once per unique filing across all versions
- extract_all_candidates no longer re-cleans HTML (was 88% of CPU time)
"""
import hashlib
import os
import re
import html as html_mod
from typing import Optional, Tuple, List, Dict

from . import config

try:
    from bs4 import BeautifulSoup
    HAS_BS4 = True
except ImportError:
    HAS_BS4 = False

# Prefer lxml (C-based, 2-3× faster than html.parser). Fall back if missing.
try:
    import lxml  # noqa: F401
    _BS4_PARSER = "lxml"
except ImportError:
    _BS4_PARSER = "html.parser"


# ═══════════════════════════════════════════════════════════════════════════
# REGEX PATTERNS
# ═══════════════════════════════════════════════════════════════════════════

CRYPTO_RE = re.compile(
    r"\b("
    r"cryptocurrency|cryptocurrencies|blockchain|digital asset|digital assets|"
    r"bitcoin|btc|ethereum|eth|XRP|stablecoin|stablecoins|crypto|DeFi|NFT|"
    r"mining pool|proof.of.work|proof.of.stake|decentralized finance|"
    r"virtual currency|virtual currencies|digital currency|"
    r"crypto.?asset|crypto.?assets|digital token|tokenized|"
    r"distributed ledger|smart contract|altcoin|satoshi|"
    r"binance|coinbase|kraken|gemini|bitfinex|solana|cardano|polkadot|chainlink"
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

FEE_SCHEDULE_RE = re.compile(
    r"\b(fee schedule|fee table|base fee|transaction fee|"
    r"annual fee|monthly fee|service fee|custody fee|"
    r"pricing schedule|compensation schedule|rate schedule|billing)\b", re.I
)

BOILERPLATE_RE = re.compile(
    r"(table of contents|date of this prospectus|criminal offense|"
    r"approved or disapproved|bookrunner|delivery of the securities|"
    r"incorporation by reference|exhibits? \d|EX-\d|pursuant to|"
    r"filed herewith|XBRL|iXBRL|EDGAR Online|LIVE \d{10})", re.I
)

# ─── 10-K / 10-Q boundary pairs (Item 1A → Item 1B, etc.) ────────────────
#   Building item code regex from the edgar-crawler pattern. Handle the
#   "1A" spacing quirk where filers put "Item 1 A" with a space.

def _item_regex(code: str) -> str:
    """Turn '1A' into regex accepting 'Item 1A', 'Item 1 A', 'ITEM 1A.', etc."""
    spaced = code[0] + r"\s*" + code[1] if len(code) > 1 else code
    item_word = r"(?:Item|I\s*t\s*e\s*m)"
    if len(code) > 1:
        bare_spaced = code[0] + r"[^\S\r\n]*" + code[1]
        return rf"(?:{item_word}\s+{spaced}[.\-:\s\(]?|{bare_spaced}[.\-:\s\(])"
    return rf"{item_word}\s+{spaced}[.\-:\s\(]?"


TENK_BOUNDARIES = [
    # (section_label, start_code, end_code)
    ("risk_factors",        "1A", "1B"),
    ("risk_factors",        "1A", "2"),   # some older 10-Ks skip 1B
    ("unresolved_staff",    "1B", "2"),
    ("properties",          "2",  "3"),
]

TENQ_BOUNDARIES = [
    # Part II Item 1A is the risk factors section in 10-Q
    ("risk_factors", "1A", "2"),
    ("risk_factors", "1A", "3"),
]

# ─── Prospectus / fund risk section header patterns ──────────────────────
PROSPECTUS_RISK_HEADERS = [
    "Principal Investment Risks",
    "Principal Risks",
    "Principal Risks of Investing in the Fund",
    "Principal Risks of Investing in the Trust",
    "Summary of Principal Risks",
    "Risk Factors",
    "Risks of Investing in the Fund",
    "Risks Related to",
    "Additional Information About the Fund's Principal Investment Strategies and Risks",
    "More Information About the Fund's Principal Investment Strategies and Risks",
]

PROSPECTUS_FORMS = {
    "S-1", "N-1A", "485APOS", "485BPOS", "424B3", "424B5",
    "424B7", "424I", "POS AM", "D",
}

STRICT_EXACT_FORMS = {"10-K", "10-Q"} | PROSPECTUS_FORMS

# Section-end markers for prospectus-style filings (where there's no 'Item 1B')
PROSPECTUS_END_MARKERS = re.compile(
    r"(?:^|\n)\s*(?:"
    r"Portfolio\s+Turnover|Portfolio\s+Holdings|"
    r"Fund\s+(?:Performance|Management|Summary)|"
    r"Management\s+of\s+the\s+(?:Fund|Trust)|"
    r"Use\s+of\s+Proceeds|"
    r"Overview\s+of\s+the\s+(?:Bitcoin|Digital\s+Asset|Crypto).*?(?:Industry|Market)|"
    r"Business\s+of\s+the\s+Trust|"
    r"Additional\s+Information\s+About\s+the\s+(?:Fund|Trust)|"
    r"The\s+Trust[’']s\s+Service\s+Providers|"
    r"Custody\s+of\s+the\s+Trust[’']s\s+Assets|"
    r"Form\s+of\s+Shares|Transfer\s+of\s+Shares|"
    r"Plan\s+of\s+Distribution|"
    r"Ownership\s+or\s+Beneficial\s+Interest\s+in\s+the\s+Trust|"
    r"Conflicts\s+of\s+Interest|"
    r"Fiduciary\s+and\s+Regulatory\s+Duties\s+of\s+the\s+Sponsor|"
    r"Bitcoin\s*,\s+Bitcoin\s+Market|"
    r"Ether\s*,\s+Ether\s+Market|Ethereum\s*,\s+Ethereum\s+Market|"
    r"XRP\s*,\s+XRP\s+Market|Solana\s*,\s+Solana\s+Market|"
    r"Chainlink\s*,\s+Chainlink\s+Market|"
    r"The\s+(?:Trust|Fund)\s+and\s+(?:Bitcoin|Ether|Ethereum|XRP|Solana|Chainlink|Digital\s+Asset).*?Prices|"
    r"Description\s+of\s+the\s+(?:Shares|Trust|Securities)|"
    r"Purchase\s+and\s+(?:Sale|Redemption)\s+of\s+(?:Fund|Trust)\s+Shares|"
    r"Tax\s+Information|Tax\s+Consequences|"
    r"Special\s+Note\s+Regarding\s+Forward-Looking\s+Statements|"
    r"Organizational\s+Structure|Dividend\s+Policy|Capitalization|"
    r"Dilution|"
    r"Payments?\s+to\s+(?:Broker|Financial\s+Intermediaries)|"
    r"Management[’']?s\s+Discussion\s+and\s+Analysis\s+of\s+Financial\s+Condition\s+and\s+Results\s+of\s+Operations|"
    r"Financial\s+Highlights|"
    r"How\s+to\s+(?:Purchase|Buy|Sell|Redeem)|"
    r"Creation\s+and\s+Redemption|Creation\s+Units|"
    r"Dividends\s+(?:and|&)\s+Distributions|"
    r"Determination\s+of\s+Net\s+Asset\s+Value|"
    r"Investment\s+(?:Policies|Restrictions|Limitations)|"
    r"Description\s+of\s+the\s+(?:Index|Benchmark)|"
    r"Signatures|Exhibit\s+Index"
    r")\b", re.I | re.MULTILINE
)

# ─── Density thresholds for dual-signal validation ───────────────────────
MIN_RISK_KEYWORDS_PER_1K = 2     # "risk", "may", "adversely" per 1000 chars
MIN_CRYPTO_KEYWORDS = 2          # anywhere in the section
MAX_FEE_RISK_RATIO = 0.5         # fee terms can't exceed this fraction of risk terms


# ═══════════════════════════════════════════════════════════════════════════
# STRUCTURAL HTML PREFILTER (Pattern 2)
# ═══════════════════════════════════════════════════════════════════════════

def _text_cache_path(html_hash: str) -> str:
    cache_dir = getattr(config, "TEXT_CACHE_DIR", "")
    if not cache_dir:
        return ""
    cache_version = getattr(config, "TEXT_CACHE_VERSION", "htmlclean-v1")
    return os.path.join(cache_dir, cache_version, html_hash[:2], html_hash + ".txt")


def _is_toc_like_table(text: str) -> bool:
    """Return True for EDGAR navigation/TOC tables that poison section regexes."""
    compact = re.sub(r"\s+", " ", text or "").strip()
    if not compact:
        return False

    item_hits = len(re.findall(r"\bItem\s+\d+\s*[A-Z]?\b", compact, re.I))
    page_hits = len(re.findall(r"\b\d{1,4}\b", compact))
    has_toc_label = bool(re.search(r"\btable\s+of\s+contents\b|\bcontents\b", compact, re.I))

    if has_toc_label and (item_hits >= 1 or page_hits >= 2):
        return True
    if item_hits >= 3 and page_hits >= 3:
        return True

    # SEC TOCs often alternate section labels and dotted/page-number runs.
    dotted_page_hits = len(re.findall(r"\.{2,}\s*\d{1,4}\b", compact))
    return item_hits >= 2 and dotted_page_hits >= 2


def _strip_toc_tables_regex(raw_html: str) -> str:
    """Best-effort TOC table removal for environments without BeautifulSoup."""
    def replace_table(match):
        table_html = match.group(0)
        table_text = re.sub(r"<[^>]+>", " ", table_html)
        table_text = html_mod.unescape(table_text)
        return " " if _is_toc_like_table(table_text) else table_html

    return re.sub(r"<table\b[^>]*>.*?</table>", replace_table, raw_html,
                  flags=re.I | re.S)


def _clean_html_to_text_regex(raw_html: str) -> str:
    """Fast HTML-to-text fallback for multi-megabyte SEC documents.

    BeautifulSoup with lxml is accurate but expensive on 5-10 MB prospectuses.
    This path preserves block boundaries and source words/punctuation while
    avoiding full DOM construction.
    """
    if not raw_html:
        return ""

    text = re.sub(
        r"<(script|style|head|noscript)\b[^>]*>.*?</\1>",
        " ",
        raw_html,
        flags=re.I | re.S,
    )
    text = _strip_toc_tables_regex(text)
    text = re.sub(r"<br\b[^>]*?/?>", "\n", text, flags=re.I)
    text = re.sub(
        r"</(?:p|div|h1|h2|h3|h4|h5|h6|li|tr|blockquote|section|article)\s*>",
        "\n\n",
        text,
        flags=re.I,
    )
    text = re.sub(
        r"<(?:p|div|h1|h2|h3|h4|h5|h6|li|tr|blockquote|section|article)\b[^>]*>",
        "\n",
        text,
        flags=re.I,
    )
    text = re.sub(r"<[^>]+>", " ", text)
    text = html_mod.unescape(text)
    text = re.sub(r"[^\S\n]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def clean_html_to_text(raw_html: str) -> str:
    r"""Strip scripts/styles, drop TOC-like tables, unwrap spans, extract text.

    Results are cached by sha256(html) so the expensive BS4 parse only runs
    once per unique filing across all versions.
    """
    if not raw_html:
        return ""

    html_hash = hashlib.sha256(raw_html.encode("utf-8", errors="replace")).hexdigest()
    cache_path = _text_cache_path(html_hash)
    if cache_path:
        try:
            if os.path.exists(cache_path):
                with open(cache_path, "r", encoding="utf-8") as f:
                    return f.read()
        except Exception:
            pass

    fast_threshold = int(getattr(config, "FAST_HTML_CLEAN_BYTES", 0) or 0)
    use_fast_path = bool(fast_threshold and len(raw_html) >= fast_threshold)

    if use_fast_path or not HAS_BS4:
        text = _clean_html_to_text_regex(raw_html)
        if cache_path and text:
            try:
                os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                with open(cache_path, "w", encoding="utf-8") as f:
                    f.write(text)
            except Exception:
                pass
        return text

    soup = BeautifulSoup(raw_html, _BS4_PARSER)

    for tag in soup.find_all(["script", "style", "head", "meta", "link", "noscript"]):
        tag.decompose()

    # Drop TOC tables before block newlines are inserted. Otherwise the
    # extractor can select an index span instead of the body risk section.
    for table in soup.find_all("table"):
        txt = table.get_text(" ", strip=True)
        if len(txt) < 100000 and _is_toc_like_table(txt):
            table.decompose()
            continue

    # Unwrap inline formatting tags that break regex
    for tag in soup.find_all(["span", "font", "b", "i", "em", "strong", "u"]):
        tag.unwrap()

    # Add newlines around block elements so text-based regex works
    for tag in soup.find_all([
        "p", "div", "h1", "h2", "h3", "h4", "h5", "h6",
        "li", "tr", "blockquote", "section", "article",
    ]):
        tag.insert_before("\n\n")
        tag.append("\n")

    for br in soup.find_all("br"):
        br.replace_with("\n")

    text = soup.get_text()
    # Normalize whitespace — collapse runs, preserve paragraph breaks
    text = re.sub(r"[^\S\n]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    # Unescape any leftover HTML entities
    text = html_mod.unescape(text)
    result = text.strip()

    if cache_path and result:
        try:
            os.makedirs(os.path.dirname(cache_path), exist_ok=True)
            with open(cache_path, "w", encoding="utf-8") as f:
                f.write(result)
        except Exception:
            pass

    return result


# ═══════════════════════════════════════════════════════════════════════════
# BOUNDARY-PAIR EXTRACTION (Pattern 1)
# ═══════════════════════════════════════════════════════════════════════════

def _exact_text_hash(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", errors="replace")).hexdigest()


def _line_start_for_offset(text: str, offset: int) -> int:
    pos = text.rfind("\n", 0, offset)
    return 0 if pos < 0 else pos + 1


def _heading_phrase_regex(header: str) -> str:
    """Match a section heading even when SEC HTML splits words across lines."""
    def fuzzy_word(word: str) -> str:
        # EDGAR inline/XBRL rendering can split headings inside words:
        # "Risk Fa ctors", "RI SK FACTORS". Keep matching strict to headings
        # by applying this only inside line-start heading checks.
        return r"\s*".join(re.escape(ch) for ch in word)

    parts = [fuzzy_word(p) for p in re.split(r"\s+", header.strip()) if p]
    return r"\b" + r"\s+".join(parts) + r"\b"


def _looks_like_toc_span(span_text: str) -> bool:
    sample = span_text[:1200]
    section_start = sample[:220]
    standalone_page_lines = len(re.findall(r"(?m)^\s*(?:[ivxlcdm]+|\d{1,4})\s*$", sample, re.I))
    item_hits = len(re.findall(r"\bItem\s+\d+\s*[A-Z]?\b", sample, re.I))
    body_words = len(re.findall(r"\b(?:risks?|adversely?|volatile|uncertain(?:ty)?|cybersecurity|custody|regulatory)\b", sample, re.I))
    if item_hits >= 2 and standalone_page_lines >= 1 and body_words < 3:
        return True
    prospectus_heading_lines = len(re.findall(
        r"(?im)^\s*(?:"
        r"prospectus\s+summary|"
        r"bitcoin\s*,\s+bitcoin\s+market.*|ether\s*,\s+ether\s+market.*|"
        r"ethereum\s*,\s+ethereum\s+market.*|xrp\s*,\s+xrp\s+market.*|"
        r"solana\s*,\s+solana\s+market.*|chainlink\s*,\s+chainlink\s+market.*|"
        r"use\s+of\s+proceeds|overview\s+of\s+the\s+(?:bitcoin|digital\s+asset|crypto).*|"
        r"business\s+of\s+the\s+trust|description\s+of\s+the\s+(?:shares|trust|securities)|"
        r"the\s+securities\s+depository|plan\s+of\s+distribution|conflicts\s+of\s+interest|"
        r"material\s+contracts|index\s+provider|legal\s+matters|experts|the\s+offering"
        r")\s*$",
        sample,
    ))
    if (
        re.search(r"(?im)^\s*risk\s+factors\s*$", sample[:120])
        and re.search(r"(?m)^\s*\d{1,4}\s*$", sample[:260])
        and prospectus_heading_lines >= 1
    ):
        return True
    if re.search(r"(?im)^\s*risk\s+factors\s*$", sample[:120]) and standalone_page_lines >= 3 and prospectus_heading_lines >= 3:
        return True
    sentence_punct = len(re.findall(r"[.;][\s\r\n]", sample[:700]))
    if re.search(r"(?im)^\s*risk\s+factors\s*$", sample[:120]) and standalone_page_lines >= 3 and sentence_punct < 2:
        return True
    if re.search(r"\b(?:Item\s+\d+\s*[A-Z]?[.\-:\s]*)?Risk\s+Factors\b", section_start, re.I):
        if re.search(r"\b(?:investing|carefully\s+consider|risks?\s+and\s+uncertainties)\b", sample[:700], re.I):
            return False
    dotted_hits = len(re.findall(r"\.{2,}\s*\d{1,4}\b", sample))
    if re.search(r"\btable\s+of\s+contents\b", sample[:350], re.I) and body_words < 3:
        return True
    return item_hits >= 3 and dotted_hits >= 2 and body_words < 3


def extract_boundary_pair(text: str, start_code: str, end_code: str,
                          min_chars: int = 1000) -> List[Dict]:
    """Find all (start_code..end_code) spans and return them ranked by length.

    Multiple spans exist because TOC has one (tiny) and body has one (large).
    Pick the largest. Returns list of {start, end, text, span_length}.
    """
    if not text:
        return []

    start_pat = _item_regex(start_code)
    end_pat = _item_regex(end_code)

    # Compile combined pattern — non-greedy span between start and end
    pattern = re.compile(
        rf"((?:^|\n)[^\S\r\n]*{start_pat}).+?((?:^|\n)[^\S\r\n]*{end_pat})",
        re.IGNORECASE | re.DOTALL,
    )

    candidates = []
    for m in pattern.finditer(text):
        section_start = m.start(1)
        while section_start < len(text) and text[section_start] in "\r\n \t":
            section_start += 1
        span_text = text[section_start:m.start(2)]  # up to start of end_pat
        if len(span_text) < min_chars:
            continue
        if _looks_like_toc_span(span_text):
            continue
        candidates.append({
            "start": section_start,
            "end": m.start(2),
            "text": span_text.rstrip(),
            "span_length": len(span_text.rstrip()),
        })

    # Sort by span length descending — largest span is the body
    candidates.sort(key=lambda x: -x["span_length"])
    return candidates


RISK_REFERENCE_RE = re.compile(
    r"\b(?:you\s+should\s+carefully\s+(?:review|consider)|"
    r"there\s+have\s+been\s+no\s+material\s+changes)\b"
    r".{0,800}?"
    r"\b(?:risk\s+factors?|risks?)\b"
    r".{0,800}?"
    r"\b(?:Item\s+1A|Form\s+10-K|Annual\s+Report)\b",
    re.I | re.S,
)


def extract_10q_risk_references(text: str, min_chars: int = 120) -> List[Dict]:
    """Extract exact no-change risk-reference paragraphs from 10-Qs.

    Some quarterly reports omit Part II Item 1A entirely and instead include a
    source paragraph telling investors to review the prior Form 10-K Item 1A
    risk factors. This is not a substitute Item 1A section, but it is the exact
    risk disclosure available in that filing and is preferable to paragraph
    density snippets.
    """
    if not text:
        return []

    candidates = []
    for m in RISK_REFERENCE_RE.finditer(text):
        start = text.rfind("\n\n", 0, m.start())
        start = 0 if start < 0 else start + 2
        end = text.find("\n\n", m.end())
        end = len(text) if end < 0 else end
        section = text[start:end].strip()
        while start < len(text) and text[start] in "\r\n \t":
            start += 1
        end = start + len(section)
        if len(section) < min_chars:
            continue
        if _looks_like_toc_span(section):
            continue
        candidates.append({
            "start": start,
            "end": end,
            "text": section,
            "span_length": len(section),
        })

    candidates.sort(key=lambda x: -x["span_length"])
    return candidates


def extract_prospectus_risk(text: str, fund_name: Optional[str] = None,
                            min_chars: int = 800) -> List[Dict]:
    """Find prospectus-style risk sections (no Item \\d structure).

    Uses header patterns like 'Principal Investment Risks' as start anchors,
    and prospectus boundary markers (Portfolio Turnover, Fee Table, etc.) as ends.
    """
    if not text:
        return []

    candidates = []
    for header in PROSPECTUS_RISK_HEADERS:
        # Case-variant header search. SEC rendered HTML often turns headings
        # like "RISK FACTORS" into "RISK\nFACTORS"; keep the source text but
        # allow whitespace/newline splits while matching.
        for m in re.finditer(_heading_phrase_regex(header), text, re.I):
            line_start = _line_start_for_offset(text, m.start())
            prefix = text[line_start:m.start()].strip(" \t\"'“”‘’")
            if prefix:
                continue
            # Skip cross-references ("see the Principal Risks section")
            before = text[max(0, m.start() - 80):m.start()].lower()
            if any(x in before for x in ["see ", "refer to ", "beginning on page"]):
                continue
            after = text[m.end():m.end() + 120].lower()
            if header.lower() == "risk factors" and re.match(r"\s+related\s+to\b", after):
                continue
            if re.search(r"\bsee\s+the\s+risks?\s+discussed\b|\bsee\s+.*?\bprospectus\s+before\b", after[:120]):
                continue
            if re.search(r"\b(?:beginning|starts?|starting|appears?)\s+on\s+page\b", after[:90]):
                continue

            content_start = line_start
            # End = next prospectus boundary OR 80K chars
            end_match = PROSPECTUS_END_MARKERS.search(text[m.end() + 300:])
            if end_match:
                content_end = m.end() + 300 + end_match.start()
            else:
                content_end = min(content_start + 80000, len(text))

            section = text[content_start:content_end].rstrip()
            if len(section) < min_chars:
                continue
            if _looks_like_toc_span(section):
                continue

            candidates.append({
                "start": content_start,
                "end": content_end,
                "text": section,
                "span_length": len(section),
                "header": header,
            })

    candidates.sort(key=lambda x: -x["span_length"])
    return candidates


def _candidate_from_span(span: Dict, section_type: str, method: str,
                         title: str, source_text: str,
                         require_crypto: bool = True) -> Optional[Dict]:
    text = span["text"]
    v = validate_section(text, require_crypto=require_crypto)
    if not v["valid"]:
        return None
    return {
        "section_type": section_type,
        "method": method,
        "title": title,
        "text": text,
        "confidence": v["score"],
        "signals": v["signals"],
        "start_offset": span.get("start"),
        "end_offset": span.get("end"),
        "source_hash": _exact_text_hash(source_text),
        "exact_text_hash": _exact_text_hash(text),
    }


def _rank_extraction_candidates(candidates: List[Dict]) -> List[Dict]:
    """Rank exact source-backed candidates before fallback candidates.

    Confidence is still useful for fallback candidates, but an exact
    top-level source section must not lose primary status to a later subsection
    just because the subsection has denser risk vocabulary.
    """
    def key(c: Dict):
        method = c.get("method", "")
        is_exact = method.startswith("exact_")
        if is_exact:
            start = c.get("start_offset")
            return (0, start is None, start or 0, -c.get("confidence", 0))
        return (1, -c.get("confidence", 0), c.get("start_offset") or 0)

    return sorted(candidates, key=key)


def extract_exact_risk_sections(source_text: str, form_type: str = "") -> List[Dict]:
    """Extract exact risk-section text from source filing text with offsets.

    This path intentionally runs before edgartools object parsing. It is built
    for auditability: candidate text comes directly from the filing text passed
    to this function, with start/end offsets when regex boundaries are found.
    """
    root = re.sub(r"/A$", "", form_type or "")
    candidates = []

    if root in ("10-K", "10-Q"):
        boundaries = TENK_BOUNDARIES if root == "10-K" else TENQ_BOUNDARIES
        min_chars = 100 if root == "10-K" else 20
        for section_type, start_code, end_code in boundaries:
            for span in extract_boundary_pair(source_text, start_code, end_code, min_chars=min_chars):
                c = _candidate_from_span(
                    span, section_type, f"exact_boundary_{start_code}_{end_code}",
                    f"Item {start_code}", source_text,
                    require_crypto=False,
                )
                if c:
                    candidates.append(c)
                    break
            if candidates:
                break

        if root == "10-Q" and not candidates:
            for span in extract_10q_risk_references(source_text):
                c = _candidate_from_span(
                    span, "risk_reference", "exact_10q_risk_reference",
                    "10-Q risk factor reference", source_text,
                    require_crypto=False,
                )
                if c:
                    candidates.append(c)
                    break

    if root in PROSPECTUS_FORMS:
        for span in extract_prospectus_risk(source_text, min_chars=200)[:10]:
            c = _candidate_from_span(
                span, "principal_risks", "exact_prospectus_header",
                span.get("header", "Risk Factors"), source_text,
            )
            if c:
                candidates.append(c)

    return _rank_extraction_candidates(candidates)


# ═══════════════════════════════════════════════════════════════════════════
# DUAL-SIGNAL VALIDATION (Pattern 3)
# ═══════════════════════════════════════════════════════════════════════════

def validate_section(text: str, require_crypto: bool = True) -> Dict:
    """Validate a candidate section by density and content signals.

    Returns a dict:
      { valid: bool, score: float (0..1), signals: {...} }

    A section is valid if:
      - >= MIN_CRYPTO_KEYWORDS crypto terms (when require_crypto)
      - risk keyword density >= MIN_RISK_KEYWORDS_PER_1K per 1000 chars
      - not a fee schedule (fee/risk ratio < MAX_FEE_RISK_RATIO)
      - not boilerplate-heavy
    """
    trusted_item_risk_heading = (
        not require_crypto
        and re.search(
            r"\b(?:(?:Item|I\s*t\s*e\s*m)\s+1\s*A|1[^\S\r\n]*A)[.\-:\s]+R\s*I\s*S\s*K\s+Factors\b",
            text[:800] if text else "",
            re.I,
        )
    )
    trusted_prospectus_risk_heading = (
        require_crypto
        and bool(text)
        and any(
            re.search(_heading_phrase_regex(h), text[:800], re.I)
            for h in (
                "Risk Factors",
                "Principal Investment Risks",
                "Principal Risks",
                "Risks Related to",
            )
        )
    )
    if not text or len(text) < 200:
        if trusted_item_risk_heading and len(text or "") >= 25:
            return {
                "valid": True,
                "score": 0.35,
                "signals": {
                    "reason": "trusted item boundary",
                    "char_count": len(text or ""),
                },
            }
        return {"valid": False, "score": 0.0, "signals": {"reason": "too short"}}

    sample = text[:40000]
    char_count = len(sample)
    crypto_hits = len(CRYPTO_RE.findall(sample))
    risk_hits = len(RISK_RE.findall(sample))
    fee_hits = len(FEE_SCHEDULE_RE.findall(sample))
    boiler_hits = len(BOILERPLATE_RE.findall(sample))

    risk_density = risk_hits / (char_count / 1000.0) if char_count else 0
    fee_risk_ratio = fee_hits / risk_hits if risk_hits else 1.0

    signals = {
        "crypto_hits": crypto_hits,
        "risk_hits": risk_hits,
        "fee_hits": fee_hits,
        "boilerplate_hits": boiler_hits,
        "risk_density": round(risk_density, 2),
        "fee_risk_ratio": round(fee_risk_ratio, 2),
        "char_count": char_count,
    }

    if require_crypto and crypto_hits < MIN_CRYPTO_KEYWORDS:
        return {"valid": False, "score": 0.0,
                "signals": {**signals, "reason": "insufficient crypto content"}}
    trusted_low_density = (
        trusted_item_risk_heading
        or (
            trusted_prospectus_risk_heading
            and risk_hits >= 10
            and (not require_crypto or crypto_hits >= MIN_CRYPTO_KEYWORDS)
        )
    )
    if risk_density < MIN_RISK_KEYWORDS_PER_1K and not trusted_low_density:
        return {"valid": False, "score": 0.0,
                "signals": {**signals, "reason": "insufficient risk density"}}
    if fee_risk_ratio > MAX_FEE_RISK_RATIO:
        return {"valid": False, "score": 0.0,
                "signals": {**signals, "reason": "fee schedule"}}

    # Confidence score = weighted combination (0..1)
    # Anchors: 10 crypto + 50 risk hits at good density → ~0.9
    confidence = min(1.0, (
        0.3 * min(crypto_hits / 15.0, 1.0) +
        0.4 * min(risk_density / 15.0, 1.0) +
        0.2 * (1.0 if fee_risk_ratio < 0.2 else 0.5) +
        0.1 * (1.0 if boiler_hits < 5 else 0.5)
    ))
    if trusted_item_risk_heading:
        confidence = max(confidence, 0.55)
    if trusted_prospectus_risk_heading:
        confidence = max(confidence, 0.55)
    return {"valid": True, "score": confidence, "signals": signals}


# ═══════════════════════════════════════════════════════════════════════════
# MAIN EXTRACTION PIPELINE — returns LIST of candidates with confidence
# ═══════════════════════════════════════════════════════════════════════════

def extract_all_candidates(filing, full_text: str, html: str = "",
                           form_type: str = "") -> List[Dict]:
    """Run all extraction strategies and return ranked candidates.

    Strategy order (best-first):
      A) Exact source-text boundary extraction with offsets/provenance.
      B) edgartools typed object (TenK.risk_factors, TenQ['Part II, Item 1A'],
         CurrentReport.sections) — the library's HTMLParser already does smart
         section detection w/ TOC handling, Cross-Reference Index, etc.
      C) Item code boundary-pair regex (10-K/10-Q only) for filings where the
         typed parser comes back empty.
      D) Prospectus header extraction (S-1, N-1A, 485APOS, etc).
      E) Lenient boundary-pair (1A → 2) for 10-K/10-Q.
      F) Paragraph-density crypto filter (last-resort).
    """
    candidates = []
    root = re.sub(r"/A$", "", form_type)

    # v1.1.35: exact source-text extraction is first because stored risk text
    # needs to trace back to the filing text, not an object-model paraphrase.
    candidates.extend(extract_exact_risk_sections(full_text, form_type=form_type))

    # ─── A) edgartools typed object (only for 10-K, 10-Q, 8-K) ──────────
    if root in ("10-K", "10-Q", "8-K") and not candidates:
        typed_candidates = _extract_via_edgartools_obj(filing, root)
        candidates.extend(typed_candidates)

    # ─── B) Boundary-pair regex for 10-K / 10-Q (when A came up empty) ───
    if root in ("10-K", "10-Q") and not candidates:
        boundaries = TENK_BOUNDARIES if root == "10-K" else TENQ_BOUNDARIES
        for section_type, start_code, end_code in boundaries:
            spans = extract_boundary_pair(full_text, start_code, end_code)
            if spans:
                best = spans[0]
                v = validate_section(best["text"])
                if v["valid"]:
                    candidates.append({
                        "section_type": section_type,
                        "method": f"boundary_pair_{start_code}_{end_code}",
                        "title": f"Item {start_code}",
                        "text": best["text"],
                        "confidence": v["score"],
                        "signals": v["signals"],
                        "start_offset": best.get("start"),
                        "end_offset": best.get("end"),
                        "source_hash": _exact_text_hash(full_text),
                        "exact_text_hash": _exact_text_hash(best["text"]),
                    })
                    break

    # ─── C) Prospectus header extraction for fund forms ──────────────────
    if root in PROSPECTUS_FORMS:
        spans = extract_prospectus_risk(full_text)
        for span in spans[:3]:
            v = validate_section(span["text"])
            if v["valid"]:
                candidates.append({
                    "section_type": "principal_risks",
                    "method": "prospectus_header",
                    "title": span.get("header", ""),
                    "text": span["text"],
                    "confidence": v["score"],
                    "signals": v["signals"],
                    "start_offset": span.get("start"),
                    "end_offset": span.get("end"),
                    "source_hash": _exact_text_hash(full_text),
                    "exact_text_hash": _exact_text_hash(span["text"]),
                })

    # ─── D) Lenient boundary-pair fallback ───────────────────────────────
    if not candidates and root in ("10-K", "10-Q"):
        spans = extract_boundary_pair(full_text, "1A", "2", min_chars=500)
        if spans:
            v = validate_section(spans[0]["text"])
            if v["valid"]:
                candidates.append({
                    "section_type": "risk_factors",
                    "method": "boundary_pair_lenient",
                    "title": "Item 1A (lenient)",
                    "text": spans[0]["text"],
                    "confidence": v["score"] * 0.8,
                    "signals": v["signals"],
                    "start_offset": spans[0].get("start"),
                    "end_offset": spans[0].get("end"),
                    "source_hash": _exact_text_hash(full_text),
                    "exact_text_hash": _exact_text_hash(spans[0]["text"]),
                })

    # ─── E) Paragraph-density crypto filter ──────────────────────────────
    if not candidates and root not in STRICT_EXACT_FORMS:
        para_text = extract_crypto_paragraphs(full_text)
        if para_text and len(para_text) > 500:
            v = validate_section(para_text)
            if v["valid"]:
                candidates.append({
                    "section_type": "crypto_paragraphs",
                    "method": "paragraph_density",
                    "title": "Crypto-relevant paragraphs",
                    "text": para_text,
                    "confidence": v["score"] * 0.6,
                    "signals": v["signals"],
                    "start_offset": None,
                    "end_offset": None,
                    "source_hash": _exact_text_hash(full_text),
                    "exact_text_hash": _exact_text_hash(para_text),
                })

    candidates = _rank_extraction_candidates(candidates)

    deduped = []
    for c in candidates:
        is_dup = any(
            _text_overlap(c["text"][:2000], other["text"][:2000]) > 0.8
            for other in deduped
        )
        if not is_dup:
            c.setdefault("source_hash", _exact_text_hash(full_text))
            c.setdefault("exact_text_hash", _exact_text_hash(c.get("text", "")))
            c.setdefault("start_offset", None)
            c.setdefault("end_offset", None)
            deduped.append(c)
    return deduped[:5]


def _extract_via_edgartools_obj(filing, root: str) -> List[Dict]:
    """Use the edgartools typed report object (TenK/TenQ/CurrentReport) to get
    properly-parsed section text. The library's HTMLParser handles Cross-Reference
    Index format (GE-style), combined items (Items 1 and 2), TOC tables, and
    bold-paragraph fallback detection — much better than custom regex.
    """
    out = []
    try:
        obj = filing.obj()
    except Exception:
        return out
    if obj is None:
        return out

    # 10-K: prefer .risk_factors (Item 1A); fall back to MD&A and Business if empty
    if root == "10-K":
        for label, accessor in [
            ("risk_factors", "risk_factors"),
            ("management_discussion", "management_discussion"),
            ("business", "business"),
        ]:
            try:
                text = getattr(obj, accessor, None)
            except Exception:
                text = None
            if not text or not isinstance(text, str) or len(text) < 500:
                continue
            v = validate_section(text)
            if v["valid"]:
                out.append({
                    "section_type": label,
                    "method": f"edgartools_{accessor}",
                    "title": f"TenK.{accessor}",
                    "text": text,
                    "confidence": v["score"],
                    "signals": v["signals"],
                })

    # 10-Q: risk factors live in Part II, Item 1A
    elif root == "10-Q":
        for label, key in [
            ("risk_factors", "Part II, Item 1A"),
            ("legal_proceedings", "Part II, Item 1"),
            ("mda", "Part I, Item 2"),
        ]:
            try:
                text = obj[key] if hasattr(obj, "__getitem__") else None
            except Exception:
                text = None
            if not text or not isinstance(text, str) or len(text) < 500:
                continue
            v = validate_section(text)
            if v["valid"]:
                out.append({
                    "section_type": label,
                    "method": "edgartools_tenq",
                    "title": key,
                    "text": text,
                    "confidence": v["score"],
                    "signals": v["signals"],
                })

    # 8-K: scan all detected items for crypto content (most relevant items are
    # 7.01 Reg FD, 8.01 Other Events, 1.01 Material Agreement, 2.02 Results)
    elif root == "8-K":
        try:
            sections = getattr(obj, "sections", {}) or {}
        except Exception:
            sections = {}
        for key, sec in sections.items():
            try:
                text = sec.text() if hasattr(sec, "text") else str(sec)
            except Exception:
                continue
            if not text or len(text) < 400:
                continue
            v = validate_section(text)
            if v["valid"]:
                out.append({
                    "section_type": "current_report_item",
                    "method": "edgartools_8k_item",
                    "title": key,
                    "text": text,
                    "confidence": v["score"],
                    "signals": v["signals"],
                })

    return out



def _text_overlap(a: str, b: str) -> float:
    """Rough overlap ratio of two strings by word intersection."""
    aw = set(a.lower().split())
    bw = set(b.lower().split())
    if not aw or not bw:
        return 0.0
    return len(aw & bw) / min(len(aw), len(bw))


# ═══════════════════════════════════════════════════════════════════════════
# MULTI-FUND HANDLING — Fund/Series API
# ═══════════════════════════════════════════════════════════════════════════

def extract_fund_documents(filing) -> List[Dict]:
    """For multi-fund filings, return one document per attachment, each scored
    for crypto relevance. Highest-scoring doc is the crypto fund's prospectus.
    """
    docs = []
    try:
        attachments = filing.attachments
        if not attachments or not attachments.documents:
            return []

        for i, doc in enumerate(attachments.documents[:5]):
            try:
                if hasattr(doc, "is_binary") and doc.is_binary:
                    continue
                text = doc.text() if hasattr(doc, "text") else ""
                if not text or len(text) < 500:
                    continue
                # Score by crypto presence in first 20K chars
                crypto_hits = len(CRYPTO_RE.findall(text[:20000]))
                # Try to read a fund name out of the first few lines
                fund_name = _guess_fund_name(text)
                docs.append({
                    "seq": i,
                    "name": getattr(doc, "document", "") or getattr(doc, "url", "")[-80:],
                    "type": getattr(doc, "document_type", "") or "",
                    "text": text,
                    "crypto_score": crypto_hits,
                    "fund_name": fund_name,
                })
            except Exception:
                continue
    except Exception:
        pass

    docs.sort(key=lambda d: -d["crypto_score"])
    return docs


def _guess_fund_name(text: str) -> str:
    """Pull a fund/trust name out of the first few paragraphs."""
    head = text[:3000]
    patterns = [
        r"\b([A-Z][A-Za-z&.\s]{4,60}(?:Trust|Fund|ETF))\b",
        r"\b([A-Z][A-Za-z&.\s]{4,60}Bitcoin[A-Za-z\s]{0,20}(?:Trust|Fund|ETF))\b",
        r"\b([A-Z][A-Za-z&.\s]{4,60}(?:Crypto|Digital|Ethereum)[A-Za-z\s]{0,20}(?:Trust|Fund|ETF))\b",
    ]
    for p in patterns:
        m = re.search(p, head)
        if m:
            name = m.group(1).strip()
            if len(name) > 8 and len(name) < 80:
                return name
    return ""


# ═══════════════════════════════════════════════════════════════════════════
# PARAGRAPH DENSITY FALLBACK
# ═══════════════════════════════════════════════════════════════════════════

def extract_crypto_paragraphs(text: str, max_chars: int = 60000) -> str:
    """Extract paragraphs that contain both crypto AND risk language."""
    paragraphs = re.split(r"\n\s*\n", text)
    if len(paragraphs) < 5:
        # Long unsegmented text — chunk by sentence pairs
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
        if len(p) < 60 or BOILERPLATE_RE.search(p) or FEE_SCHEDULE_RE.search(p):
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


# ═══════════════════════════════════════════════════════════════════════════
# FULL-TEXT FETCH (stores raw text for DB caching)
# ═══════════════════════════════════════════════════════════════════════════

def fetch_filing_text(filing) -> Tuple[str, str]:
    """Get (full_text, raw_html) from a Filing object. Prefer HTML for cleaner
    structural parsing."""
    full_text = ""
    raw_html = ""

    try:
        raw_html = filing.html() or ""
    except Exception:
        pass

    if raw_html:
        full_text = clean_html_to_text(raw_html)

    if not full_text:
        try:
            full_text = filing.text() or ""
        except Exception:
            pass

    return full_text, raw_html


# ═══════════════════════════════════════════════════════════════════════════
# HTML FORMATTING FOR DISPLAY
# ═══════════════════════════════════════════════════════════════════════════

def format_risk_for_html(text: str) -> str:
    """Format extracted risk text into HTML for display."""
    if not text:
        return ""

    text = re.sub(r"\n\s*\d{1,3}\s*\n", "\n", text)
    text = re.sub(r"\nTable of Contents\n", "\n", text, flags=re.I)

    paragraphs = re.split(r"\n\s*\n", text)
    parts = []
    for p in paragraphs:
        p = p.strip()
        if not p or len(p) < 5:
            continue

        is_header = (
            len(p) < 120 and not p.endswith(".") and not p.endswith(",")
            and re.search(r"[A-Z]", p)
            and not BOILERPLATE_RE.search(p)
            and not FEE_SCHEDULE_RE.search(p)
        )

        escaped = html_mod.escape(p)
        lines = escaped.split("\n")
        if len(lines) > 1:
            bullet_lines = sum(
                1 for ln in lines
                if re.match(r"^\s*[•●\-\*\(\d]", ln.strip())
            )
            if bullet_lines > len(lines) * 0.3:
                escaped = "<br>".join(ln.strip() for ln in lines if ln.strip())
            else:
                escaped = " ".join(ln.strip() for ln in lines if ln.strip())

        if is_header:
            parts.append(
                f'<div style="font-weight:700;font-size:11.5px;color:#e2e8f0;'
                f'margin:14px 0 4px 0;padding-bottom:2px;'
                f'border-bottom:1px solid #1e293b;">{escaped}</div>'
            )
        else:
            parts.append(
                f'<p style="margin:6px 0;text-indent:0;line-height:1.75;">{escaped}</p>'
            )

    return "".join(parts)


def get_filing_url(cik, accession_no) -> str:
    cik_clean = str(cik).lstrip("0")
    acc_clean = accession_no.replace("-", "")
    return (
        f"https://www.sec.gov/Archives/edgar/data/"
        f"{cik_clean}/{acc_clean}/{accession_no}-index.htm"
    )
