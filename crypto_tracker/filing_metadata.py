"""
Per-filing contextual metadata generator (v1.1.35).

Produces human-readable Type, Purpose, and Holdings strings for each filing
based on what the entity IS (ETF, miner, exchange, etc.) and what this
specific form type MEANS (annual report, prospectus amendment, etc.).

Previously (v1.1.33): one static purpose per company CIK regardless of form.
Now: combines entity classification with form semantics.
"""
from . import config

# ─── Entity subcategories ────────────────────────────────────────────────
# Maps the "category" field from CRYPTO_COMPANIES to display labels and
# holdings descriptions.

ENTITY_TYPES = {
    "etf_issuer": "ETF",
    "exchange": "Crypto Exchange",
    "broker": "Crypto Broker/Financial Services",
    "miner": "Bitcoin Miner",
    "treasury": "Corporate Treasury (Crypto Holdings)",
    "payments": "Payments/Fintech",
    "other": "Company",
}

ENTITY_HOLDINGS_TEMPLATES = {
    "etf_issuer": "Holds spot {tokens} in trust for shareholders",
    "exchange": "Operates marketplace for {tokens} and other digital assets",
    "broker": "Provides trading access to {tokens} and other digital assets",
    "miner": "Mines and holds {tokens} as primary business output",
    "treasury": "Holds {tokens} as corporate treasury reserve asset",
    "payments": "Enables buying, selling, and holding {tokens} for users",
    "other": "Exposure to {tokens} via operations or investments",
}

# ─── Form type semantics ─────────────────────────────────────────────────

FORM_DESCRIPTIONS = {
    "10-K": "Annual report",
    "10-K/A": "Amended annual report",
    "10-Q": "Quarterly report",
    "10-Q/A": "Amended quarterly report",
    "8-K": "Current report (material event disclosure)",
    "8-K/A": "Amended current report",
    "S-1": "Registration statement for new securities",
    "S-1/A": "Amended registration statement",
    "S-3": "Shelf registration statement",
    "S-3/A": "Amended shelf registration statement",
    "N-1A": "Mutual fund/ETF registration statement",
    "N-1A/A": "Amended mutual fund/ETF registration",
    "485APOS": "Post-effective prospectus amendment",
    "485BPOS": "Post-effective prospectus amendment (Rule 485(b))",
    "D": "Notice of exempt offering (Regulation D)",
    "D/A": "Amended notice of exempt offering",
    "DEF 14A": "Definitive proxy statement",
    "4": "Insider transaction report (Section 16)",
    "SC 13D": "Beneficial ownership report (activist)",
    "SC 13G": "Beneficial ownership report (passive)",
    "20-F": "Annual report (foreign private issuer)",
    "6-K": "Current report (foreign private issuer)",
}

# ─── Purpose templates by (entity_category, form_root) ───────────────────
# {company} = company name, {form_desc} = form description, {tokens} = assets

PURPOSE_TEMPLATES = {
    # ETF filings
    ("etf_issuer", "S-1"): "{form_desc} for {company}, a spot {tokens} ETF. Registers the fund's shares for public sale.",
    ("etf_issuer", "N-1A"): "{form_desc} for {company}, a spot {tokens} ETF. Defines fund objectives, fees, and risk factors.",
    ("etf_issuer", "485APOS"): "{form_desc} for {company}, a spot {tokens} ETF. Updates prospectus disclosures including risk factors and fund performance.",
    ("etf_issuer", "485BPOS"): "{form_desc} for {company}, a spot {tokens} ETF. Updates prospectus disclosures including risk factors and fund performance.",
    ("etf_issuer", "10-K"): "{form_desc} for {company}, a spot {tokens} ETF. Annual financial statements and risk disclosures for the fund.",
    ("etf_issuer", "10-Q"): "{form_desc} for {company}, a spot {tokens} ETF. Quarterly financial performance and NAV updates.",
    ("etf_issuer", "8-K"): "{form_desc} for {company}, a spot {tokens} ETF. Discloses material event such as NAV change, fee adjustment, or regulatory update.",
    ("etf_issuer", "D"): "Notice of exempt offering for {company}, a spot {tokens} fund. Filed under Regulation D.",

    # Exchanges
    ("exchange", "10-K"): "{form_desc} for {company}, a cryptocurrency exchange. Contains annual financials, trading volume data, and crypto-related risk factors.",
    ("exchange", "10-Q"): "{form_desc} for {company}, a cryptocurrency exchange. Quarterly revenue, user growth, and regulatory risk updates.",
    ("exchange", "8-K"): "{form_desc} for {company}, a cryptocurrency exchange. Discloses material event such as regulatory action, product launch, or executive change.",
    ("exchange", "S-1"): "{form_desc} for {company}, a cryptocurrency exchange. Registers securities for public offering.",
    ("exchange", "DEF 14A"): "Proxy statement for {company}, a cryptocurrency exchange. Annual meeting voting matters.",

    # Brokers
    ("broker", "10-K"): "{form_desc} for {company}, a crypto-integrated financial services firm. Annual financials and risk disclosures related to digital asset services.",
    ("broker", "10-Q"): "{form_desc} for {company}, a crypto-integrated financial services firm. Quarterly performance and crypto segment updates.",
    ("broker", "8-K"): "{form_desc} for {company}, a crypto-integrated financial services firm. Material event disclosure.",
    ("broker", "S-1"): "{form_desc} for {company}, a crypto-integrated financial services firm. Registers securities for public offering.",

    # Miners
    ("miner", "10-K"): "{form_desc} for {company}, a {tokens} mining company. Annual financials including hash rate, BTC production, energy costs, and crypto risk factors.",
    ("miner", "10-Q"): "{form_desc} for {company}, a {tokens} mining company. Quarterly mining output, revenue, and operational metrics.",
    ("miner", "8-K"): "{form_desc} for {company}, a {tokens} mining company. Discloses material event such as equipment purchase, facility expansion, or hash rate change.",
    ("miner", "S-1"): "{form_desc} for {company}, a {tokens} mining company. Registers securities for public offering or capital raise.",
    ("miner", "S-3"): "Shelf registration for {company}, a {tokens} mining company. Enables future capital raises.",

    # Treasury companies
    ("treasury", "10-K"): "{form_desc} for {company}, which holds {tokens} as a corporate treasury reserve. Annual financials including digital asset impairment and fair value disclosures.",
    ("treasury", "10-Q"): "{form_desc} for {company}, which holds {tokens} as a corporate treasury reserve. Quarterly crypto treasury valuation and impairment updates.",
    ("treasury", "8-K"): "{form_desc} for {company}, which holds {tokens} as a corporate treasury reserve. Material event such as additional crypto purchase, sale, or strategy change.",
    ("treasury", "S-1"): "{form_desc} for {company}, which holds {tokens} as a corporate treasury reserve. Registers securities (often to fund further crypto acquisitions).",
    ("treasury", "S-3"): "Shelf registration for {company}. Enables future capital raises, often used to fund {tokens} treasury purchases.",

    # Payments/Fintech
    ("payments", "10-K"): "{form_desc} for {company}, a payments company with crypto services. Annual financials and risk disclosures related to digital asset operations.",
    ("payments", "10-Q"): "{form_desc} for {company}, a payments company with crypto services. Quarterly crypto revenue and regulatory updates.",
    ("payments", "8-K"): "{form_desc} for {company}, a payments company with crypto services. Material event disclosure.",
    ("payments", "S-1"): "{form_desc} for {company}, a payments company with crypto services. Registers securities for offering.",

    # Other/fallback
    ("other", "10-K"): "{form_desc} for {company}. Annual report with risk disclosures related to {tokens} or digital assets.",
    ("other", "10-Q"): "{form_desc} for {company}. Quarterly report with crypto-related risk disclosures.",
    ("other", "8-K"): "{form_desc} for {company}. Material event related to {tokens} or digital assets.",
    ("other", "S-1"): "{form_desc} for {company}. Registration statement for securities offering.",
}


def _get_entity_category(cik: str) -> str:
    """Look up the entity subcategory from CRYPTO_COMPANIES."""
    companies = getattr(config, "CRYPTO_COMPANIES", {})
    if cik in companies:
        return companies[cik][2]  # (name, ticker, category)
    return ""


def _get_tokens(cik: str, filing_category: str) -> str:
    """Get the token list for a company. Falls back to category-based guess."""
    details = getattr(config, "COMPANY_DETAILS", {})
    if cik in details:
        return details[cik][1]  # (purpose, holdings)
    companies = getattr(config, "CRYPTO_COMPANIES", {})
    if cik in companies:
        cat = companies[cik][2]
        if "btc" in cat.lower() or cat == "miner":
            return "BTC"
        if "eth" in cat.lower():
            return "ETH"
    if filing_category == "ETF/Fund":
        return "digital assets"
    return "cryptocurrency"


def generate_filing_metadata(company_name: str, ticker: str, form_type: str,
                             filing_category: str, crypto_connection: str,
                             cik: str) -> tuple:
    """Generate contextual (entity_type, purpose, holdings) for one filing.

    Returns:
        (entity_type: str, purpose: str, holdings: str)
    """
    category = _get_entity_category(cik)
    root_form = form_type.replace("/A", "")
    tokens = _get_tokens(cik, filing_category)

    # Entity type
    if category:
        entity_type = ENTITY_TYPES.get(category, "Company")
    elif filing_category == "ETF/Fund":
        entity_type = "ETF/Fund"
    else:
        entity_type = "Company"

    # Add form type context to entity_type
    form_desc = FORM_DESCRIPTIONS.get(form_type, "") or FORM_DESCRIPTIONS.get(root_form, f"{form_type} filing")
    display_type = f"{entity_type} — {form_desc}"

    # Purpose: look up template by (category, root_form), fall back to generic
    template_key = (category or "other", root_form)
    template = PURPOSE_TEMPLATES.get(template_key)

    if not template:
        # Fallback: use category-generic template
        fallback_key = (category or "other", "10-K")
        template = PURPOSE_TEMPLATES.get(fallback_key, "{form_desc} for {company}. Contains disclosures related to {tokens}.")

    purpose = template.format(
        company=company_name,
        form_desc=form_desc,
        tokens=tokens,
    )

    # Holdings
    if category:
        holdings_template = ENTITY_HOLDINGS_TEMPLATES.get(category, "Exposure to {tokens}")
        holdings = holdings_template.format(tokens=tokens)
    elif filing_category == "ETF/Fund":
        holdings = f"Fund with exposure to {tokens}"
    else:
        # Unknown company — use crypto_connection if available
        if crypto_connection:
            holdings = f"Exposure to {crypto_connection}"
        else:
            holdings = f"Exposure to {tokens}"

    return (display_type, purpose, holdings)
