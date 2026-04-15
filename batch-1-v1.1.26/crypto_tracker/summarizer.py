"""
Filing Summary Generator v1.1.26 — Actually informative summaries.

v24/v25 FAILURES:
- Just listed sub-heading names: "Filing identifies 12 risk areas: Concentration Risk..."
- Picked up fee schedule items as "risk areas"
- Generic category labels like "Key risk categories (3): Regulatory; Custody; Valuation"
- Copied boilerplate first paragraphs as "specific sentences"

v1.1.26 APPROACH:
- Summaries describe WHAT the filing is (type, entity, crypto assets involved)
- Count actual risk sub-sections and categorize them
- Extract 2-3 specific, substantive sentences with real content
- For non-risk filings (8-K, agreements): describe purpose, parties, key terms
- No API key needed — template-based with intelligent content extraction
"""
import re
from typing import Optional, List

from .extractor import CRYPTO_RE, RISK_RE, BOILERPLATE_RE, FEE_SCHEDULE_RE
from . import config


# ═══════════════════════════════════════════════════════════════════════════
# FILING TYPE DETECTION
# ═══════════════════════════════════════════════════════════════════════════

def _detect_entity_and_type(text, form_type="", company_name=""):
    """Detect the entity name, crypto assets, and filing purpose from the text."""
    info = {
        "entity": company_name or "",
        "crypto_assets": [],
        "filing_purpose": "",
        "fund_name": "",
    }

    sample = text[:10000] if text else ""
    lower = sample.lower()

    # Detect crypto assets mentioned
    asset_patterns = [
        (r"\bbitcoin\b", "Bitcoin"),
        (r"\bethereum\b|\beth\b", "Ethereum"),
        (r"\bxrp\b", "XRP"),
        (r"\bsolana\b|\bsol\b", "Solana"),
        (r"\bstablecoin", "stablecoins"),
        (r"\bdefi\b|decentralized finance", "DeFi"),
        (r"\bnft\b|non.fungible", "NFTs"),
        (r"\bcrypto(?:currency|currencies)\b", "cryptocurrency"),
        (r"\bdigital asset", "digital assets"),
        (r"\bblockchain\b", "blockchain"),
    ]
    for pattern, label in asset_patterns:
        if re.search(pattern, lower):
            info["crypto_assets"].append(label)

    # Detect fund/trust name
    fund_patterns = [
        r"(?:the\s+)?([A-Z][A-Za-z\s]+(?:Trust|Fund|ETF)(?:\s+[IVX]+)?)",
        r"(?:the\s+)?([A-Z][A-Za-z\s]+Bitcoin\s+(?:Trust|Fund|ETF))",
        r"(?:the\s+)?([A-Z][A-Za-z\s]+Crypto\s+(?:Trust|Fund|ETF))",
        r"(?:the\s+)?([A-Z][A-Za-z\s]+Digital\s+(?:Trust|Fund|ETF))",
    ]
    for pattern in fund_patterns:
        match = re.search(pattern, sample)
        if match:
            name = match.group(1).strip()
            if len(name) > 5 and len(name) < 80 and not BOILERPLATE_RE.search(name):
                info["fund_name"] = name
                break

    # Detect filing purpose based on form type
    form_purposes = {
        "S-1": "security registration statement",
        "S-1/A": "amended security registration statement",
        "N-1A": "investment company registration",
        "N-1A/A": "amended investment company registration",
        "485APOS": "post-effective amendment to fund registration",
        "485BPOS": "post-effective amendment to fund registration",
        "10-K": "annual report",
        "10-Q": "quarterly report",
        "8-K": "current report (material event disclosure)",
        "8-K/A": "amended current report",
        "D": "exempt offering notice",
        "D/A": "amended exempt offering notice",
    }
    info["filing_purpose"] = form_purposes.get(form_type, f"{form_type} filing")

    return info


# ═══════════════════════════════════════════════════════════════════════════
# RISK SUB-SECTION ANALYSIS
# ═══════════════════════════════════════════════════════════════════════════

def _count_risk_subsections(text) -> List[str]:
    """Find actual risk sub-headings in the text.
    Returns list of sub-heading names that are genuinely risk-related."""
    lines = text.split("\n")
    sub_headings = []

    # Common sentence starters that indicate body text, NOT headings
    sentence_starters = frozenset({
        "the", "a", "an", "in", "it", "if", "as", "we", "to", "for", "on",
        "at", "by", "or", "and", "but", "this", "that", "these", "those",
        "there", "they", "you", "your", "its", "such", "each", "any",
        "no", "not", "than", "with", "from", "however", "because",
        "although", "while", "since", "when", "where", "which",
    })

    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue

        words = line.split()
        num_words = len(words)

        # Shape filters for sub-headings
        if num_words < 2 or num_words > 15:
            continue
        if len(line) < 8 or len(line) > 140:
            continue
        if line.endswith(".") or line.endswith(","):
            continue
        if not line[0].isupper():
            continue

        # Reject sentence starters
        if words[0].lower() in sentence_starters:
            continue

        # Reject fee schedule items
        if FEE_SCHEDULE_RE.search(line):
            continue
        if BOILERPLATE_RE.search(line):
            continue

        # Must contain risk-related OR crypto-related words
        has_risk = bool(re.search(
            r"\b(Risk|Risks|Volatility|Volatile|Uncertainty|Loss|"
            r"Fraud|Hack|Theft|Custody|Regulatory|Compliance|Manipulation|"
            r"Cybersecurity|Liquidity|Concentration|Litigation|Fork|Tax|"
            r"Speculative|Leverage|Counterparty|Credit|Default|Impairment|"
            r"Adverse|Uncertain)\b", line
        ))
        has_crypto = bool(CRYPTO_RE.search(line))
        if not has_risk and not has_crypto:
            continue

        # Next non-empty line should be a paragraph body (longer)
        next_len = 0
        for j in range(i + 1, min(i + 5, len(lines))):
            nl = lines[j].strip()
            if nl and len(nl) > 30:
                next_len = len(nl)
                break
        if next_len < len(line) * 1.2 and next_len < 80:
            continue

        # Clean up
        clean = re.sub(r"^[\u2022\u25CF\-\*]+\s*", "", line).strip()
        clean = re.sub(r"[:.]$", "", clean).strip()
        if clean and len(clean) > 8:
            is_dup = any(
                existing.lower()[:20] == clean.lower()[:20]
                for existing in sub_headings
            )
            if not is_dup:
                sub_headings.append(clean)

        if len(sub_headings) >= 20:
            break

    return sub_headings


# ═══════════════════════════════════════════════════════════════════════════
# RISK CATEGORY DETECTION
# ═══════════════════════════════════════════════════════════════════════════

_RISK_CATEGORIES = [
    ("bitcoin/crypto price volatility",
     r"(price volatility|volatile|price.{0,15}fluctuat|extreme.{0,10}fluctuat|speculative)"),
    ("regulatory and legal uncertainty",
     r"(regulatory.{0,15}(?:uncertain|risk|change|evolv)|SEC.{0,15}(?:regulat|enforce)|CFTC|securities law|regulatory framework)"),
    ("custody and storage of digital assets",
     r"(custody|custod|safekeeping|private key|wallet|cold storage|hot wallet)"),
    ("cybersecurity and hacking threats",
     r"(hack|cyber.?security|cyber.?attack|security breach|unauthorized access|phishing)"),
    ("tax treatment and reporting",
     r"(tax.{0,15}(?:treatment|uncertain|consequence|implication)|IRS|capital gain|tax reporting)"),
    ("liquidity and trading volume",
     r"(liquidity.{0,10}risk|illiquid|trading volume.{0,10}(?:low|limit)|market depth)"),
    ("market manipulation",
     r"(manipulation|manipulat|wash trading|spoofing|front.running)"),
    ("blockchain fork and protocol risk",
     r"(fork|hard fork|protocol.{0,10}(?:change|upgrade|risk)|consensus mechanism)"),
    ("concentration and single-asset exposure",
     r"(concentration|concentrated|single.{0,10}(?:asset|invest|exposure)|undiversified)"),
    ("operational and technology failures",
     r"(operational.{0,10}risk|service.{0,10}(?:disrupt|interrupt)|system.{0,10}fail|technology risk)"),
    ("counterparty and exchange risk",
     r"(counterparty|counter.party|exchange.{0,10}(?:fail|insol|bankrupt)|exchange risk)"),
    ("valuation uncertainty",
     r"(valuation.{0,10}(?:risk|difficult|uncertain)|fair value|net asset value|NAV)"),
    ("AML/KYC compliance",
     r"(anti.money|money laundering|AML|KYC|know your customer|sanctions)"),
    ("competition from other digital assets",
     r"(competition|competitive.{0,10}(?:pressure|landscape|risk)|competing.{0,10}(?:digital|crypto))"),
    ("insurance limitations",
     r"(insurance.{0,10}(?:limit|not cover|inadequate)|SIPC|FDIC.{0,10}not|uninsured)"),
]


def _detect_risk_categories(text) -> List[str]:
    """Detect which risk categories are present in the text."""
    lower = text[:60000].lower()
    return [name for name, pattern in _RISK_CATEGORIES if re.search(pattern, lower)]


# ═══════════════════════════════════════════════════════════════════════════
# SPECIFIC SENTENCE EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════

_GENERIC_RE = re.compile(
    r"("
    r"you could lose|no assurance|past performance|"
    r"an investment in the|please read|as with all investments|"
    r"you should consider|before investing|there is no guarantee|"
    r"may not be suitable|carefully consider|the following|"
    r"this summary is not complete|see the prospectus|"
    r"table of contents|page \d+|for more information"
    r")", re.I
)


def _get_specific_sentences(text, count=3) -> List[str]:
    """Find concrete, specific sentences about actual risks.
    Rejects boilerplate, generic warnings, and fee descriptions."""
    sentences = re.split(r"(?<=[.!?])\s+", text[:50000])
    scored = []

    for s in sentences:
        s = s.strip()
        if len(s) < 80 or len(s) > 450:
            continue
        if _GENERIC_RE.search(s):
            continue
        if BOILERPLATE_RE.search(s):
            continue
        if FEE_SCHEDULE_RE.search(s):
            continue

        crypto_hits = len(CRYPTO_RE.findall(s))
        risk_hits = len(RISK_RE.findall(s))
        if crypto_hits == 0 or risk_hits == 0:
            continue

        # Bonus for specificity (named entities, numbers, specific terms)
        specificity = len(re.findall(
            r"\b(SEC|CFTC|IRS|FINRA|FinCEN|OCC|DOJ|"
            r"bitcoin|ethereum|XRP|Coinbase|CBOE|NYSE|Nasdaq|CME|"
            r"Bitfinex|Binance|Kraken|Gemini|Grayscale|BlackRock|Fidelity|"
            r"\d+%|\$[\d,.]+(?:\s*(?:billion|million))?|billion|million)\b",
            s, re.I,
        ))

        # Penalty for vague hedge words
        hedge_penalty = len(re.findall(
            r"\b(may|could|might|would|should|possible|potential|certain)\b",
            s, re.I,
        )) * 0.5

        score = crypto_hits * 3 + risk_hits * 2 + specificity * 5 - hedge_penalty
        scored.append((score, s))

    scored.sort(key=lambda x: -x[0])

    # Deduplicate
    selected = []
    for sc, s in scored:
        s_words = set(s.lower().split())
        is_dup = any(
            len(s_words & set(prev.lower().split())) / max(len(s_words), 1) > 0.4
            for _, prev in selected
        )
        if not is_dup:
            selected.append((sc, s))
        if len(selected) >= count:
            break

    return [s for _, s in selected]


# ═══════════════════════════════════════════════════════════════════════════
# NON-RISK FILING SUMMARIZATION (8-K, D forms, agreements, etc.)
# ═══════════════════════════════════════════════════════════════════════════

def _summarize_non_risk_filing(text, form_type="", company_name="") -> str:
    """Summarize filings that don't have a traditional risk section.
    Describes: what the filing is, parties involved, purpose, key terms."""
    info = _detect_entity_and_type(text, form_type, company_name)
    parts = []

    # Opening: what is this filing?
    entity = info["entity"] or "Unknown entity"
    purpose = info["filing_purpose"]
    assets = info["crypto_assets"]

    opening = f"{entity} filed a {purpose}"
    if assets:
        opening += f" related to {', '.join(assets[:3])}"
    opening += "."
    parts.append(opening)

    # Try to extract what happened (for 8-K: material event)
    if "8-K" in form_type:
        # Look for item numbers that indicate what the 8-K is about
        item_patterns = [
            (r"Item\s+1\.01", "entry into a material definitive agreement"),
            (r"Item\s+1\.02", "termination of a material definitive agreement"),
            (r"Item\s+2\.01", "completion of an acquisition or disposition"),
            (r"Item\s+2\.02", "results of operations and financial condition"),
            (r"Item\s+5\.02", "departure/election of directors or officers"),
            (r"Item\s+7\.01", "Regulation FD disclosure"),
            (r"Item\s+8\.01", "other events"),
            (r"Item\s+9\.01", "financial statements and exhibits"),
        ]
        for pattern, desc in item_patterns:
            if re.search(pattern, text[:5000]):
                parts.append(f"Reports: {desc}.")
                break

    # For D forms: try to find offering amount
    if form_type in ("D", "D/A"):
        amount_match = re.search(
            r"(?:total|aggregate).{0,30}(?:offering|amount).{0,20}(\$[\d,.]+(?:\s*(?:million|billion))?)",
            text[:10000], re.I,
        )
        if amount_match:
            parts.append(f"Offering amount: {amount_match.group(1)}.")

    # Get 2 specific sentences about crypto content
    specific = _get_specific_sentences(text, count=2)
    for s in specific:
        parts.append(s)

    if len(parts) < 2:
        # Fallback: grab the most crypto-relevant sentence
        sentences = re.split(r"(?<=[.!?])\s+", text[:20000])
        for s in sentences:
            s = s.strip()
            if len(s) > 80 and CRYPTO_RE.search(s) and not _GENERIC_RE.search(s):
                parts.append(s)
                break

    return " ".join(parts)


# ═══════════════════════════════════════════════════════════════════════════
# MAIN SUMMARY BUILDER
# ═══════════════════════════════════════════════════════════════════════════

def build_summary(text, form_type="", company_name="") -> str:
    """Generate an informative summary of the filing's risk section.

    Structure:
    1. What the filing IS (type, entity, crypto assets involved)
    2. How many risk areas / what categories
    3. 2-3 specific sentences with actual substance

    Args:
        text: The risk section text (or full document for non-risk filings)
        form_type: SEC form type (S-1, 10-K, 485APOS, etc.)
        company_name: Company name for context

    Returns:
        str: 4-8 line informative summary
    """
    if not text or len(text) < 100:
        return ""

    # Check if this text has a real risk section or if we need non-risk handling
    has_risk_content = bool(re.search(
        r"risk factor|principal.{0,5}risk|investment risk",
        text[:5000], re.I,
    ))

    if not has_risk_content and form_type in ("8-K", "8-K/A", "D", "D/A"):
        return _summarize_non_risk_filing(text, form_type, company_name)

    # Detect entity and filing info
    info = _detect_entity_and_type(text, form_type, company_name)
    parts = []

    # PART 1: What is this filing?
    entity = info["entity"] or company_name or "This entity"
    purpose = info["filing_purpose"]
    fund_name = info["fund_name"]
    assets = info["crypto_assets"]

    opening = f"{entity}"
    if fund_name and fund_name.lower() != entity.lower():
        opening += f" ({fund_name})"
    opening += f" — {purpose}"
    if assets:
        opening += f" involving {', '.join(assets[:3])}"
    opening += "."
    parts.append(opening)

    # PART 2: Risk section scope
    sub_headings = _count_risk_subsections(text)
    categories = _detect_risk_categories(text)

    if sub_headings:
        n = len(sub_headings)
        # Show the top risk sub-headings (most relevant ones)
        crypto_headings = [h for h in sub_headings if CRYPTO_RE.search(h)]
        display = crypto_headings[:4] if crypto_headings else sub_headings[:4]
        parts.append(
            f"Risk section covers {n} areas including: "
            + "; ".join(display)
            + (f" (and {n - len(display)} more)" if n > len(display) else "")
            + "."
        )
    elif categories:
        parts.append(
            f"Key risk themes ({len(categories)}): "
            + "; ".join(categories[:5])
            + (f" (and {len(categories) - 5} more)" if len(categories) > 5 else "")
            + "."
        )

    # PART 3: Specific substantive sentences
    specific = _get_specific_sentences(text, count=2)
    for s in specific:
        parts.append(s)

    # Ensure we have something meaningful
    if len(parts) < 2:
        return _summarize_non_risk_filing(text, form_type, company_name)

    return " ".join(parts)
