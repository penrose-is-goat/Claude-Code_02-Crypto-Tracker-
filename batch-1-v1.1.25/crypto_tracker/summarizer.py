"""
Filing Summary Generator.

Two modes:
1. AI Summarization (Claude API) — If ANTHROPIC_API_KEY is set in config,
   uses Claude to generate high-quality 6-10 line summaries.
2. Extractive Summarization (default) — No API needed. Extracts sub-headings
   and key specific sentences from the risk section text.
"""
import re

from . import config
from .extractor import CRYPTO_RE, RISK_RE, BOILERPLATE_RE


# ═══════════════════════════════════════════════════════════════════════════
# AI SUMMARIZATION (Claude API)
# ═══════════════════════════════════════════════════════════════════════════

def _summarize_with_ai(text):
    """Use Claude API to generate a summary of the risk section.
    Returns summary string or None if API unavailable/fails."""
    if not config.ANTHROPIC_API_KEY:
        return None

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)

        # Truncate to avoid excessive token usage
        truncated = text[:25000]

        response = client.messages.create(
            model=config.SUMMARY_MODEL,
            max_tokens=config.SUMMARY_MAX_TOKENS,
            messages=[{
                "role": "user",
                "content": (
                    "You are analyzing an SEC filing's risk section for a crypto/digital asset "
                    "related company or fund. Provide a concise 6-10 line summary that:\n"
                    "1. Lists the key risk areas identified (e.g., price volatility, regulatory "
                    "uncertainty, custody risk, cybersecurity)\n"
                    "2. Highlights any specific, notable risks unique to this filing\n"
                    "3. Mentions any specific regulatory bodies, exchanges, or entities referenced\n"
                    "Do NOT use generic boilerplate. Be specific to THIS filing.\n\n"
                    f"RISK SECTION TEXT:\n{truncated}"
                ),
            }],
        )
        return response.content[0].text.strip()

    except Exception:
        return None


# ═══════════════════════════════════════════════════════════════════════════
# EXTRACTIVE SUMMARIZATION (no API needed)
# ═══════════════════════════════════════════════════════════════════════════

# Common sentence starters that indicate body text, not headings
_SENTENCE_STARTERS = frozenset({
    "the", "a", "an", "in", "it", "if", "as", "we", "to", "for", "on",
    "at", "by", "or", "and", "but", "this", "that", "these", "those",
    "there", "they", "you", "your", "its", "such", "each", "any",
    "no", "not", "than", "with", "from", "however", "because",
    "although", "while", "since", "when", "where", "which",
})

# Generic boilerplate phrases to filter out of summaries
_GENERIC_RE = re.compile(
    r"("
    r"you could lose|no assurance|past performance|"
    r"an investment in the|please read|as with all investments|"
    r"you should consider|before investing|there is no guarantee|"
    r"may not be suitable|carefully consider|the following"
    r")", re.I
)

# Thematic risk categories with detection patterns
_THEMES = [
    ("Price volatility risk",
     r"(price volatility|volatile|price.{0,15}fluctuat|extreme.{0,10}fluctuat|speculative)"),
    ("Regulatory/compliance uncertainty",
     r"(regulatory.{0,15}(?:uncertain|risk|change|evolv)|SEC.{0,15}(?:regulat|enforce)|CFTC|securities law)"),
    ("Custody & storage risk",
     r"(custody|custod|safekeeping|private key|wallet|cold storage)"),
    ("Cybersecurity & hacking risk",
     r"(hack|cyber.?security|cyber.?attack|security breach|unauthorized access)"),
    ("Tax treatment uncertainty",
     r"(tax.{0,15}(?:treatment|uncertain|consequence|implication)|IRS|capital gain)"),
    ("Liquidity risk",
     r"(liquidity.{0,10}risk|illiquid|trading volume.{0,10}(?:low|limit))"),
    ("Market manipulation risk",
     r"(manipulation|manipulat|wash trading|spoofing)"),
    ("Fork & protocol risk",
     r"(fork|hard fork|protocol.{0,10}(?:change|upgrade|risk))"),
    ("Concentration risk",
     r"(concentration|concentrated|single.{0,10}(?:asset|invest|exposure)|undiversified)"),
    ("Operational/technology risk",
     r"(operational.{0,10}risk|service.{0,10}(?:disrupt|interrupt)|system.{0,10}fail)"),
    ("Counterparty risk",
     r"(counterparty|counter.party|exchange.{0,10}(?:fail|insol|bankrupt))"),
    ("Valuation risk",
     r"(valuation.{0,10}(?:risk|difficult|uncertain)|fair value|mark.to.market)"),
    ("AML/KYC risk",
     r"(anti.money|money laundering|AML|KYC|know your customer)"),
    ("Insurance limitations",
     r"(insurance.{0,10}(?:limit|not cover|inadequate)|SIPC|FDIC.{0,10}not)"),
    ("Competition risk",
     r"(competition|competitive.{0,10}(?:pressure|landscape|risk))"),
]


def _extract_subheadings(text):
    """Extract actual risk sub-headings from the filing.

    SEC risk sub-headers are noun phrases like "Bitcoin Volatility Risk",
    "Custody Risk", "Regulatory Uncertainty" — short, no trailing period,
    followed by a longer paragraph body.
    """
    lines = text.split("\n")
    sub_headings = []

    for i, line in enumerate(lines):
        line = line.strip()
        if not line:
            continue

        words = line.split()
        num_words = len(words)

        # Hard shape filters
        if num_words < 2 or num_words > 15:
            continue
        if len(line) < 10 or len(line) > 140:
            continue
        if line.endswith(".") or line.endswith(","):
            continue
        if BOILERPLATE_RE.search(line):
            continue
        if not line[0].isupper():
            continue

        # Reject common sentence starters (body text, not headings)
        first_word = words[0].lower()
        if first_word in _SENTENCE_STARTERS:
            continue

        # Must contain a risk-related OR crypto-related word
        has_risk = bool(re.search(
            r"\b(Risk|Risks|Volatility|Volatile|Uncertainty|Uncertain|Loss|"
            r"Fraud|Hack|Theft|Custody|Regulatory|Compliance|Manipulation|"
            r"Cybersecurity|Liquidity|Concentration|Litigation|Fork|Tax|"
            r"Speculative|Leverage|Counterparty|Credit|Default|Impairment)\b",
            line,
        ))
        has_crypto = bool(CRYPTO_RE.search(line))
        if not has_risk and not has_crypto:
            continue

        # Next non-empty line should be substantially longer (paragraph body)
        next_para_len = 0
        for j in range(i + 1, min(i + 5, len(lines))):
            nl = lines[j].strip()
            if nl and len(nl) > 30:
                next_para_len = len(nl)
                break
        if next_para_len < len(line) * 1.3 and next_para_len < 80:
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

        if len(sub_headings) >= 12:
            break

    return sub_headings


def _detect_themes(text):
    """Detect which risk themes are present in the text."""
    lower = text[:60000].lower()
    return [name for name, pattern in _THEMES if re.search(pattern, lower)]


def _get_specific_sentences(text, count=3):
    """Find concrete, specific sentences (not boilerplate) from risk text."""
    sentences = re.split(r"(?<=[.!?])\s+", text[:50000])
    scored = []

    for s in sentences:
        s = s.strip()
        if len(s) < 70 or len(s) > 400:
            continue
        if _GENERIC_RE.search(s):
            continue
        if BOILERPLATE_RE.search(s):
            continue

        crypto_hits = len(CRYPTO_RE.findall(s))
        risk_hits = len(RISK_RE.findall(s))
        if crypto_hits == 0 or risk_hits == 0:
            continue

        # Bonus for specificity (named entities, numbers)
        specificity = len(re.findall(
            r"\b(SEC|CFTC|IRS|FINRA|FinCEN|OCC|"
            r"bitcoin|ethereum|XRP|Coinbase|CBOE|NYSE|Nasdaq|CME|"
            r"Bitfinex|Binance|Kraken|Gemini|"
            r"\d+%|\$[\d,.]+|billion|million)\b",
            s, re.I,
        ))

        # Penalty for vague hedge words
        hedge_penalty = len(re.findall(
            r"\b(may|could|might|would|should|possible|potential|certain)\b",
            s, re.I,
        )) * 0.5

        score = crypto_hits * 2 + risk_hits + specificity * 4 - hedge_penalty
        scored.append((score, s))

    scored.sort(key=lambda x: -x[0])

    # Deduplicate (reject sentences too similar to already selected ones)
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


def _fallback_summary(text):
    """Last resort: extract top sentences by keyword density."""
    sentences = re.split(r"(?<=[.!?])\s+", text[:40000])
    scored = []
    for s in sentences:
        s = s.strip()
        if len(s) < 50 or len(s) > 500:
            continue
        c = len(CRYPTO_RE.findall(s))
        r = len(RISK_RE.findall(s))
        if c == 0 or r == 0:
            continue
        if BOILERPLATE_RE.search(s):
            continue
        scored.append((c * 3 + r * 2, s))

    scored.sort(key=lambda x: -x[0])
    selected = []
    for sc, s in scored:
        s_words = set(s.lower().split())
        is_dup = any(
            len(s_words & set(prev.lower().split())) / max(len(s_words), 1) > 0.5
            for _, prev in selected
        )
        if not is_dup:
            selected.append((sc, s))
        if len(selected) >= 3:
            break

    return " ".join(s for _, s in selected) if selected else ""


# ═══════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ═══════════════════════════════════════════════════════════════════════════

def build_summary(text):
    """Generate a summary of the risk section text.

    Tries AI summarization first (if API key configured), then falls back
    to extractive summarization.

    Returns:
        str: 6-10 line summary, or empty string if text is too short.
    """
    if not text or len(text) < 200:
        return ""

    # Try AI summarization first
    ai_summary = _summarize_with_ai(text)
    if ai_summary:
        return ai_summary

    # Extractive summarization
    parts = []

    # Step 1: Extract actual sub-headings from the filing
    sub_headings = _extract_subheadings(text)

    # Step 2: Detect thematic categories as supplemental info
    themes = _detect_themes(text)

    # Step 3: Get specific, non-boilerplate sentences
    specific_sentences = _get_specific_sentences(text, count=3)

    # Step 4: Assemble summary
    if sub_headings:
        n = len(sub_headings)
        display = sub_headings[:8]
        parts.append(
            f"Filing identifies {n} risk area{'s' if n > 1 else ''}: "
            + "; ".join(display)
            + (f" (and {n - 8} more)" if n > 8 else "")
            + "."
        )
    elif themes:
        parts.append(
            f"Key risk categories ({len(themes)}): "
            + "; ".join(themes[:6])
            + (f" (and {len(themes) - 6} more)" if len(themes) > 6 else "")
            + "."
        )

    for s in specific_sentences:
        parts.append(s)

    if not parts:
        return _fallback_summary(text)

    return " ".join(parts)
