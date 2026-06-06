"""
Filing Summary Generator v1.1.31.

v1.1.31: Claude prompt now returns structured JSON with summary + purpose +
holdings in a single API call (zero extra cost). Template fallback extracts
purpose/holdings from text patterns.
"""
import os
import re
import json
from typing import Tuple, List, Dict

from .extractor import CRYPTO_RE, RISK_RE, BOILERPLATE_RE, FEE_SCHEDULE_RE
from . import config


# ═══════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════

def build_summary(text: str, form_type: str = "",
                  company_name: str = "") -> Dict[str, str]:
    """Generate summary + structured details. Returns dict with keys:
    summary, model, purpose, top_holdings.

    Quality gate: if the input text isn't substantively crypto-related,
    return empty dict so the filing is dropped.
    """
    empty = {"summary": "", "model": "", "purpose": "", "top_holdings": ""}

    if not text or len(text) < 200:
        return empty

    sample = text[:30000]
    crypto_hits = len(CRYPTO_RE.findall(sample))
    risk_hits = len(RISK_RE.findall(sample))
    fee_hits = len(FEE_SCHEDULE_RE.findall(sample))

    if crypto_hits < 3:
        return empty
    if risk_hits < 5:
        return empty
    if risk_hits and fee_hits / risk_hits > 0.5:
        return empty

    if config.USE_CLAUDE_SUMMARIES and config.ANTHROPIC_API_KEY:
        try:
            result = _summarize_with_claude(text, form_type, company_name)
            if result.get("summary") and len(result["summary"]) > 40:
                result["model"] = config.SUMMARY_MODEL
                return result
        except Exception as e:
            print(f"    Claude API failed, falling back to template: {str(e)[:100]}")

    template_result = _summarize_with_template(text, form_type, company_name)
    template_result["model"] = f"template-v{config.VERSION}"
    return template_result


# ═══════════════════════════════════════════════════════════════════════════
# CLAUDE API SUMMARIZER
# ═══════════════════════════════════════════════════════════════════════════

_SYSTEM_PROMPT = """You are a legal-compliance analyst summarizing SEC filings for crypto lawyers. Be precise, factual, and specific. Never invent details not in the source text. Always respond with valid JSON."""

_USER_PROMPT_TEMPLATE = """Analyze this SEC filing excerpt and return a JSON object with exactly these fields:

{{
  "summary": "<4-6 sentence risk summary>",
  "purpose": "<one-line description of what this fund/company does, e.g. 'Track the price of Bitcoin' or 'Bitcoin mining and hosting services'>",
  "top_holdings": "<comma-separated crypto tickers held/tracked, e.g. 'BTC' or 'BTC, ETH, SOL' or 'N/A' for companies that don't hold crypto directly>"
}}

Filing metadata:
- Company: {company}
- Form type: {form_type}

Summary rules:
- 4-6 sentences: identify the filing, scope of risk disclosure, then 2-4 specific non-boilerplate crypto risks
- Plain text in the summary value, no markdown
- Cite specific crypto assets by name
- Maximum 150 words for summary

Purpose rules:
- One line, under 80 characters
- For funds: what it tracks or invests in (e.g. "Track the spot price of Bitcoin")
- For companies: what the business does related to crypto
- If unclear from text, write "Crypto-related [form_type] filing"

Holdings rules:
- Use standard tickers: BTC, ETH, SOL, ADA, DOT, DOGE, XRP, USDT, USDC, LTC, BCH, etc.
- For funds tracking an index, list the top 3-5 assets in the index
- For miners, write "BTC" (or whatever they mine)
- If truly unclear, write "N/A"

Filing text:
---
{text}
---

Return ONLY valid JSON, no other text:"""


def _summarize_with_claude(text: str, form_type: str, company_name: str) -> Dict[str, str]:
    """Call Claude Haiku. Returns dict with summary, purpose, top_holdings."""
    try:
        import anthropic
    except ImportError:
        raise RuntimeError("anthropic package not installed — pip install anthropic")

    client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    text_for_prompt = text[:30000]

    user_prompt = _USER_PROMPT_TEMPLATE.format(
        company=company_name or "Unknown",
        form_type=form_type or "Unknown",
        text=text_for_prompt,
    )

    response = client.messages.create(
        model=config.SUMMARY_MODEL,
        max_tokens=config.SUMMARY_MAX_TOKENS,
        timeout=config.SUMMARY_TIMEOUT,
        system=[{
            "type": "text",
            "text": _SYSTEM_PROMPT,
            "cache_control": {"type": "ephemeral"},
        }],
        messages=[{"role": "user", "content": user_prompt}],
    )

    raw = ""
    if response.content and hasattr(response.content[0], "text"):
        raw = response.content[0].text.strip()

    if not raw:
        return {"summary": "", "purpose": "", "top_holdings": ""}

    try:
        data = json.loads(raw)
        return {
            "summary": data.get("summary", "").strip(),
            "purpose": data.get("purpose", "").strip()[:200],
            "top_holdings": data.get("top_holdings", "").strip()[:100],
        }
    except json.JSONDecodeError:
        return {"summary": raw, "purpose": "", "top_holdings": ""}


# ═══════════════════════════════════════════════════════════════════════════
# TEMPLATE SUMMARIZER (fallback)
# ═══════════════════════════════════════════════════════════════════════════

def _summarize_with_template(text: str, form_type: str = "",
                             company_name: str = "") -> Dict[str, str]:
    """Template-based summary when Claude API is unavailable.
    Returns dict with summary, purpose, top_holdings.
    """
    if not text or len(text) < 100:
        return {"summary": "", "purpose": "", "top_holdings": ""}

    info = _detect_entity_and_type(text, form_type, company_name)
    parts = []

    entity = info["entity"] or company_name or "This entity"
    purpose_str = info["filing_purpose"]
    assets = info["crypto_assets"]
    opening = f"{entity} — {purpose_str}"
    if assets:
        opening += f" involving {', '.join(assets[:3])}"
    opening += "."
    parts.append(opening)

    categories = _detect_risk_categories(text)
    if categories:
        parts.append(
            f"Risk disclosure covers {len(categories)} crypto-relevant areas: "
            + "; ".join(categories[:5])
            + (f" (and {len(categories) - 5} more)." if len(categories) > 5 else ".")
        )

    specific = _get_specific_sentences(text, count=3)
    for s in specific:
        parts.append(s)

    summary = " ".join(parts) if len(parts) >= 2 else (parts[0] if parts else "")

    # Derive purpose from filing type + entity
    asset_tickers = _assets_to_tickers(assets)

    return {
        "summary": summary,
        "purpose": f"{purpose_str} — {entity}"[:200] if purpose_str else "",
        "top_holdings": ", ".join(asset_tickers) if asset_tickers else "N/A",
    }


def _assets_to_tickers(assets: List[str]) -> List[str]:
    """Convert detected asset names to standard tickers."""
    mapping = {
        "Bitcoin": "BTC", "Ethereum": "ETH", "XRP": "XRP",
        "Solana": "SOL", "stablecoins": "USDT/USDC", "DeFi": "DeFi",
        "NFTs": "NFT", "cryptocurrency": "BTC", "digital assets": "BTC",
        "blockchain": "BTC",
    }
    return [mapping.get(a, a) for a in assets[:5] if mapping.get(a)]


# ═══════════════════════════════════════════════════════════════════════════
# HELPERS (same logic as v1.1.26, kept for template fallback)
# ═══════════════════════════════════════════════════════════════════════════

def _detect_entity_and_type(text: str, form_type="", company_name=""):
    info = {
        "entity": company_name or "",
        "crypto_assets": [],
        "filing_purpose": "",
    }
    sample = text[:10000] if text else ""
    lower = sample.lower()

    for pattern, label in [
        (r"\bbitcoin\b", "Bitcoin"),
        (r"\bethereum\b|\beth\b", "Ethereum"),
        (r"\bxrp\b", "XRP"),
        (r"\bsolana\b", "Solana"),
        (r"\bstablecoin", "stablecoins"),
        (r"\bdefi\b|decentralized finance", "DeFi"),
        (r"\bnft\b|non.fungible", "NFTs"),
        (r"\bcrypto(?:currency|currencies)\b", "cryptocurrency"),
        (r"\bdigital asset", "digital assets"),
        (r"\bblockchain\b", "blockchain"),
    ]:
        if re.search(pattern, lower):
            info["crypto_assets"].append(label)

    info["filing_purpose"] = {
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
    }.get(form_type, f"{form_type} filing")

    return info


_RISK_CATEGORIES = [
    ("price volatility",
     r"(price volatility|volatile|price.{0,15}fluctuat|extreme.{0,10}fluctuat|speculative)"),
    ("regulatory uncertainty",
     r"(regulatory.{0,15}(?:uncertain|risk|change|evolv)|SEC.{0,15}(?:regulat|enforce)|CFTC|securities law|regulatory framework)"),
    ("custody/storage",
     r"(custody|custod|safekeeping|private key|wallet|cold storage|hot wallet)"),
    ("cybersecurity",
     r"(hack|cyber.?security|cyber.?attack|security breach|unauthorized access|phishing)"),
    ("tax treatment",
     r"(tax.{0,15}(?:treatment|uncertain|consequence|implication)|IRS|capital gain|tax reporting)"),
    ("liquidity",
     r"(liquidity.{0,10}risk|illiquid|trading volume.{0,10}(?:low|limit)|market depth)"),
    ("market manipulation",
     r"(manipulation|manipulat|wash trading|spoofing|front.running)"),
    ("blockchain fork / protocol",
     r"(fork|hard fork|protocol.{0,10}(?:change|upgrade|risk)|consensus mechanism)"),
    ("concentration",
     r"(concentration|concentrated|single.{0,10}(?:asset|invest|exposure)|undiversified)"),
    ("counterparty/exchange",
     r"(counterparty|counter.party|exchange.{0,10}(?:fail|insol|bankrupt)|exchange risk)"),
    ("valuation",
     r"(valuation.{0,10}(?:risk|difficult|uncertain)|fair value|net asset value|NAV)"),
    ("AML/KYC",
     r"(anti.money|money laundering|AML|KYC|know your customer|sanctions)"),
    ("insurance limitations",
     r"(insurance.{0,10}(?:limit|not cover|inadequate)|SIPC|FDIC.{0,10}not|uninsured)"),
]


def _detect_risk_categories(text: str) -> List[str]:
    lower = text[:60000].lower()
    return [name for name, pat in _RISK_CATEGORIES if re.search(pat, lower)]


_GENERIC_RE = re.compile(
    r"(you could lose|no assurance|past performance|"
    r"an investment in the|please read|as with all investments|"
    r"you should consider|before investing|there is no guarantee|"
    r"may not be suitable|carefully consider|the following|"
    r"this summary is not complete|see the prospectus|"
    r"table of contents|page \d+|for more information)", re.I
)


def _get_specific_sentences(text: str, count: int = 3) -> List[str]:
    sentences = re.split(r"(?<=[.!?])\s+", text[:50000])
    scored = []
    for s in sentences:
        s = s.strip()
        if len(s) < 80 or len(s) > 450:
            continue
        if _GENERIC_RE.search(s) or BOILERPLATE_RE.search(s) or FEE_SCHEDULE_RE.search(s):
            continue
        c = len(CRYPTO_RE.findall(s))
        r = len(RISK_RE.findall(s))
        if c == 0 or r == 0:
            continue
        specificity = len(re.findall(
            r"\b(SEC|CFTC|IRS|FINRA|FinCEN|OCC|DOJ|"
            r"bitcoin|ethereum|XRP|Coinbase|CBOE|NYSE|Nasdaq|CME|"
            r"Bitfinex|Binance|Kraken|Gemini|Grayscale|BlackRock|Fidelity|"
            r"\d+%|\$[\d,.]+(?:\s*(?:billion|million))?|billion|million)\b",
            s, re.I,
        ))
        hedge_penalty = len(re.findall(
            r"\b(may|could|might|would|should|possible|potential|certain)\b", s, re.I
        )) * 0.5
        scored.append((c * 3 + r * 2 + specificity * 5 - hedge_penalty, s))

    scored.sort(key=lambda x: -x[0])
    selected = []
    for sc, s in scored:
        s_words = set(s.lower().split())
        if any(
            len(s_words & set(prev.lower().split())) / max(len(s_words), 1) > 0.4
            for _, prev in selected
        ):
            continue
        selected.append((sc, s))
        if len(selected) >= count:
            break
    return [s for _, s in selected]
