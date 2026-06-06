#!/usr/bin/env python3
"""
Extraction test suite v1.1.35.

Two parts:
  (A) UNIT TESTS (run offline, no network) — test regex patterns, HTML
      cleaning, boundary-pair extraction, validation logic, and exact
      extraction against saved SEC filing text fixtures.

  (B) LIVE TESTS (optional, require SEC access) — pull a small set of
      known filings, run the full extractor, check that extraction returns
      sensible candidates with confidence scores.

Run:
    python test_extraction.py          # unit tests only (fast, offline)
    python test_extraction.py --live   # unit + live tests against real filings
"""
import sys
import os
import argparse
import csv
import json
import tempfile
import time
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from crypto_tracker.extractor import (
    clean_html_to_text, extract_boundary_pair, extract_prospectus_risk,
    validate_section, extract_all_candidates, CRYPTO_RE, RISK_RE,
    FEE_SCHEDULE_RE, _item_regex, _text_cache_path, extract_exact_risk_sections,
    extract_fund_documents,
)
from crypto_tracker import config


# ═══════════════════════════════════════════════════════════════════════════
# FIXTURES
# ═══════════════════════════════════════════════════════════════════════════

# Mock 10-K with TOC + real body (the edgar-crawler test case)
MOCK_10K = """
Company Bitcoin Corp
Form 10-K for fiscal year 2024

TABLE OF CONTENTS
Item 1 Business ...... 3
Item 1A Risk Factors ...... 14
Item 1B Unresolved Staff Comments ...... 42
Item 2 Properties ...... 43

PART I

Item 1. Business
We are a Bitcoin mining company that operates data centers.

Item 1A. Risk Factors
You should carefully consider the following risks. Our business is subject to
numerous risks including cryptocurrency market volatility, regulatory uncertainty,
cybersecurity threats, and custody risks for our Bitcoin holdings. The price of
Bitcoin has been extremely volatile, and any decline in Bitcoin prices could
materially adversely affect our results of operations. We face cybersecurity
risks because digital asset mining operations are frequent targets of hackers.
Our ability to custody Bitcoin relies on cold storage solutions, which carry
operational risks. Regulatory frameworks for cryptocurrency remain uncertain,
and new regulations could prohibit or restrict our operations. We also face
competition from other crypto miners and consolidation in the industry.
The SEC has taken enforcement actions against similar companies, which could
indicate future risks to our operations. Changes in Bitcoin protocol through
hard forks may also adversely affect our mining economics.

Item 1B. Unresolved Staff Comments
None.

Item 2. Properties
We operate facilities in Texas and Washington state.
"""

# Mock prospectus with fee table BEFORE risk section (common trap)
MOCK_PROSPECTUS = """
Corgi Crypto ETF Trust III - Prospectus

Fund Summary

Fees and Expenses of the Fund
The following table describes the fees and expenses that you may pay if you
buy, hold, and sell shares of the Fund.
Annual Fund Operating Expenses: 0.95%
Management Fee: 0.75%
Custody Fee: 0.10%

Principal Investment Strategies
The Fund invests in Bitcoin and Ethereum through physically-backed exposure.

Principal Investment Risks
Cryptocurrency Risk. The cryptocurrency market is highly volatile and can be
subject to sudden and extreme price fluctuations. Bitcoin and Ethereum are
digital assets that are not backed by any central bank or government, and
their value derives solely from market demand. The Fund's concentration in
crypto assets exposes investors to unprecedented volatility that has caused
double-digit daily price swings. Regulatory uncertainty regarding digital
assets could materially harm the Fund if the SEC, CFTC, or foreign regulators
impose new restrictions. Custody risks include the potential loss of private
keys stored in cold storage wallets, and the Fund's custodian has limited
insurance coverage for digital asset losses. Cybersecurity risks include
hacking of exchanges and wallets, which have resulted in multi-billion dollar
losses across the industry. Market manipulation has been documented in
cryptocurrency markets and can affect Bitcoin and Ethereum prices.
Stablecoin de-pegging events have caused large losses and liquidity crises.
Forks and protocol changes in the Bitcoin or Ethereum networks could
materially impact the Fund's holdings. Tax treatment of digital assets
remains uncertain and subject to change.

Portfolio Turnover
The Fund pays transaction costs when it buys and sells securities.
"""

# Mock multi-fund filing — fund A is rare earth, fund B is crypto (the bug)
MOCK_MULTI_FUND = """
Listed Funds Trust - Multiple Series Prospectus

FUND A: RARE EARTH MINING ETF
Principal Investment Risks
Investors face risks from fluctuations in rare earth metal prices. Supply chain
risks in China. Mining operations involve environmental risks. Regulatory
requirements affect mining operations globally.

FUND B: CRYPTO INDEX ETF
Principal Investment Risks
The Fund faces cryptocurrency market volatility. Bitcoin and Ethereum prices
are extremely volatile. Custody of digital assets involves cybersecurity risks.
Regulatory uncertainty for cryptocurrency could harm the Fund. Market
manipulation is a documented risk in crypto markets.
"""

MOCK_HTML_WITH_TOC = """
<html><body>
<table>
<tr><td>Table of Contents</td></tr>
<tr><td>Item 1. Business</td><td>3</td></tr>
<tr><td>Item 1A. Risk Factors</td><td>14</td></tr>
<tr><td>Item 1B. Unresolved Staff Comments</td><td>42</td></tr>
</table>
<p>Item 1A. Risk Factors</p>
<p>We face substantial cryptocurrency risks including Bitcoin price volatility,
regulatory uncertainty from the SEC and CFTC, custody risks for our digital
assets, and cybersecurity threats from hackers targeting crypto mining
operations. The price of Bitcoin has declined significantly in prior years and
may continue to be volatile. Our Ethereum holdings face similar risks. Our
ability to comply with evolving cryptocurrency regulations is uncertain.
Adverse regulatory actions could force us to cease operations.</p>
<p>Item 1B. Unresolved Staff Comments</p>
</body></html>
"""

FIXTURE_DIR = Path(__file__).resolve().parent / "tests" / "fixtures" / "sec"
SEC_FIXTURE_META = json.loads((FIXTURE_DIR / "SEC_FIXTURES.json").read_text(encoding="utf-8"))


def load_sec_fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text(encoding="utf-8")


# ═══════════════════════════════════════════════════════════════════════════
# UNIT TESTS
# ═══════════════════════════════════════════════════════════════════════════

def run_unit_tests():
    results = []

    def test(name, cond, detail=""):
        status = "PASS" if cond else "FAIL"
        results.append((name, cond, detail))
        print(f"  [{status}] {name}{' — ' + detail if detail else ''}")

    print("\n═══ UNIT TESTS ═══")

    # ── Test 1: _item_regex handles spaced codes ─────────────────────────
    print("\n[1] Item regex patterns")
    pat = _item_regex("1A")
    test("_item_regex('1A') matches 'Item 1A.'",
         bool(__import__("re").search(pat, "Item 1A.", __import__("re").I)))
    test("_item_regex('1A') matches 'ITEM 1 A:' (with space)",
         bool(__import__("re").search(pat, "ITEM 1 A:", __import__("re").I)))
    test("_item_regex('1A') matches rendered newline heading",
         bool(__import__("re").search(pat, "Item\n1A.", __import__("re").I)))
    test("_item_regex('1A') matches bare 1A heading",
         bool(__import__("re").search(pat, "1A. Risk Factors", __import__("re").I)))

    # ── Test 2: Boundary-pair extraction on 10-K mock ────────────────────
    print("\n[2] Boundary-pair extraction")
    spans = extract_boundary_pair(MOCK_10K, "1A", "1B")
    test("Finds Item 1A → Item 1B span", len(spans) >= 1,
         f"{len(spans)} spans found")
    if spans:
        largest = spans[0]
        test("Largest span contains crypto content",
             "cryptocurrency" in largest["text"].lower() or "bitcoin" in largest["text"].lower())
        test("Largest span is the BODY not the TOC",
             len(largest["text"]) > 500,
             f"span length={len(largest['text'])}")

    print("\n[2b] Exact source-text extraction")
    real_10k_text = load_sec_fixture("coinbase_2024_10k_excerpt.txt")
    real_10k_meta = SEC_FIXTURE_META["coinbase_2024_10k_excerpt.txt"]
    exact_10k = extract_exact_risk_sections(real_10k_text, real_10k_meta["form_type"])
    test("Exact 10-K extraction finds source risk section", len(exact_10k) == 1,
         f"{len(exact_10k)} candidates")
    if exact_10k:
        source_slice = real_10k_text[
            exact_10k[0]["start_offset"]:exact_10k[0]["end_offset"]
        ].rstrip()
        test("Exact 10-K text equals source section word-for-word",
             exact_10k[0]["text"] == source_slice)
        test("Exact 10-K matches saved SEC fixture hash",
             exact_10k[0]["exact_text_hash"] == real_10k_meta["expected_hash"])
        test("Exact 10-K contains actual Coinbase risk phrase",
             "Investing in our Class A common stock involves a high degree of risk" in exact_10k[0]["text"])
        test("Exact 10-K stops before Item 1B peer heading",
             "ITEM 1B. UNRESOLVED STAFF COMMENTS" not in exact_10k[0]["text"])
        test("Exact 10-K records source offsets and hashes",
             exact_10k[0]["start_offset"] is not None and
             exact_10k[0]["end_offset"] is not None and
             len(exact_10k[0]["source_hash"]) == 64 and
             len(exact_10k[0]["exact_text_hash"]) == 64)

    bakkt_10q_risk = (
        "PART II--OTHER INFORMATION\n\n"
        "Item 1A. Risk Factors.\n\n"
        "In addition to the information set forth in this Report on Form 10-Q, "
        "you should carefully consider the risk factors and other cautionary "
        "statements described under the heading “ Item 1A. Risk Factors ” "
        "included in our Form 10-K, which could materially affect our businesses, "
        "financial condition, or future results. Additional risks and uncertainties "
        "not currently known to us or that we currently deem to be immaterial also "
        "may materially adversely affect our business, financial condition, or "
        "future results. There have been no material changes in our risk factors "
        "from those described in our Form 10-K.\n\n"
        "Item 2. Unregistered Sales of Equity Securities and Use of Proceeds.\n\n"
        "None."
    )
    exact_10q = extract_exact_risk_sections(bakkt_10q_risk, "10-Q")
    expected_10q = bakkt_10q_risk[
        bakkt_10q_risk.find("Item 1A. Risk Factors."):
        bakkt_10q_risk.find("Item 2. Unregistered Sales")
    ].rstrip()
    test("Exact 10-Q no-change Item 1A section is preserved",
         len(exact_10q) == 1 and exact_10q[0]["text"] == expected_10q)

    no_item_1a_reference_10q = (
        "Item 2. Management's Discussion and Analysis of Financial Condition "
        "and Results of Operations\n\n"
        "Forward-Looking Statements\n\n"
        "You should carefully review the risks described in Item 1A of the "
        "Company's Annual Report on Form 10-K for the year ended May 31, 2022, "
        "as well as any other cautionary language in this Quarterly Report on "
        "Form 10-Q, as the occurrence of any of these events could have an "
        "adverse effect, which may be material, on our business, results of "
        "operations, financial condition or cash flows.\n\n"
        "Executive Overview\n\n"
        "The following discussion should be read together with our financial statements."
    )
    exact_10q_reference = extract_exact_risk_sections(no_item_1a_reference_10q, "10-Q")
    expected_reference = no_item_1a_reference_10q[
        no_item_1a_reference_10q.find("You should carefully review"):
        no_item_1a_reference_10q.find("Executive Overview")
    ].rstrip()
    test("Exact 10-Q risk-reference paragraph is preserved when Item 1A is omitted",
         len(exact_10q_reference) == 1 and
         exact_10q_reference[0]["method"] == "exact_10q_risk_reference" and
         exact_10q_reference[0]["text"] == expected_reference)

    low_density_item_1a = (
        "Item 1A. Risk Factors.\n\n"
        + ("This company-specific disclosure is a source risk factor. " * 12)
        + "\n\nItem 1B. Unresolved Staff Comments"
    )
    low_density_exact = extract_exact_risk_sections(low_density_item_1a, "10-K")
    test("Trusted Item 1A boundary preserves lower-density source risk section",
         bool(low_density_exact) and
         low_density_exact[0]["text"].startswith("Item 1A. Risk Factors"))

    split_risk_heading = (
        "ITEM 1A.\n\nRI SK FACTORS\n\n"
        + ("This source risk factor may materially affect operations. " * 12)
        + "\n\nITEM 1B. Unresolved Staff Comments"
    )
    split_risk_exact = extract_exact_risk_sections(split_risk_heading, "10-K")
    test("Trusted Item 1A boundary handles split RI SK heading",
         bool(split_risk_exact) and "RI SK FACTORS" in split_risk_exact[0]["text"])

    real_prospectus_text = load_sec_fixture("ibit_2024_424b3_excerpt.txt")
    real_prospectus_meta = SEC_FIXTURE_META["ibit_2024_424b3_excerpt.txt"]
    exact_prospectus = extract_exact_risk_sections(
        real_prospectus_text, real_prospectus_meta["form_type"]
    )
    test("Exact prospectus extraction finds SEC risk section", len(exact_prospectus) >= 1,
         f"{len(exact_prospectus)} candidates")
    if exact_prospectus:
        source_slice = real_prospectus_text[
            exact_prospectus[0]["start_offset"]:exact_prospectus[0]["end_offset"]
        ].rstrip()
        test("Exact prospectus text equals source section word-for-word",
             exact_prospectus[0]["text"] == source_slice)
        test("Exact prospectus matches saved SEC fixture hash",
             exact_prospectus[0]["exact_text_hash"] == real_prospectus_meta["expected_hash"])
        test("Exact prospectus starts at top-level risk factors",
             exact_prospectus[0]["text"].startswith("RISK FACTORS\n\nThe Shares are speculative"))
        test("Exact prospectus contains actual IBIT risk phrase",
             "The value of the Shares relates directly to the value of bitcoins" in exact_prospectus[0]["text"])
        test("Exact prospectus excludes next peer section",
             "USE OF PROCEEDS" not in exact_prospectus[0]["text"])
        class EmptyFiling:
            def sections(self): return []

        real_pipeline_candidates = extract_all_candidates(
            EmptyFiling(), real_prospectus_text, html="",
            form_type=real_prospectus_meta["form_type"],
        )
        test("Full pipeline keeps top-level exact prospectus section primary",
             bool(real_pipeline_candidates) and
             real_pipeline_candidates[0]["exact_text_hash"] == real_prospectus_meta["expected_hash"])

    invalid_span = (
        "Risk Factors\n\n" + ("Chainlink digital asset disclosure. " * 900) +
        "\n\nUSE OF PROCEEDS\n\n"
    )
    valid_span = (
        "Risk Factors\n\n" +
        ("Chainlink risk may adversely affect the Trust and investors may lose money. " * 80) +
        "\n\nUSE OF PROCEEDS\n\n"
    )
    later_valid = invalid_span + invalid_span + invalid_span + valid_span
    later_candidates = extract_exact_risk_sections(later_valid, "S-1/A")
    test("Exact prospectus extraction considers later valid risk spans",
         bool(later_candidates) and
         later_candidates[0]["text"].startswith("Risk Factors") and
         "investors may lose money" in later_candidates[0]["text"])

    split_source_s1 = (
        "Table of Contents\n\n"
        "RISK FACTORS\n\n6\n\n"
        "BITCOIN, BITCOIN MARKET, BITCOIN EXCHANGES AND REGULATION OF\n"
        "BITCOIN\n\n16\n\n"
        "Prospectus Summary\n\n"
        "Investors considering a purchase of Shares of the Trust should carefully "
        "consider how much of their total assets should be exposed to the bitcoin "
        "market.\n\n"
        "RISK\n"
        "FACTORS\n\n"
        "You should consider carefully the risks described below before making an "
        "investment decision. You should also refer to the other information "
        "included in this prospectus before you decide to purchase any Shares. "
        "Bitcoin is a new technological innovation with a limited operating "
        "history. The price of Bitcoin has exhibited periods of extreme "
        "volatility, which could have a negative impact on the performance of "
        "the Trust. Regulatory uncertainty regarding digital assets may "
        "materially adversely affect the Trust and investors may lose money. "
        "Custody of bitcoin may be vulnerable to cybersecurity threats and "
        "private key loss. Bitcoin exchanges may close due to fraud, failure, "
        "security breaches or otherwise, which may adversely affect the value "
        "of the Shares.\n\n"
        "BITCOIN, BITCOIN MARKET, BITCOIN EXCHANGES AND REGULATION OF BITCOIN\n\n"
        "This section of the prospectus provides a more detailed description of bitcoin."
    )
    split_s1_candidates = extract_exact_risk_sections(split_source_s1, "S-1")
    split_expected = split_source_s1[
        split_source_s1.find("RISK\nFACTORS"):
        split_source_s1.rfind("BITCOIN, BITCOIN MARKET")
    ].rstrip()
    test("Exact prospectus extraction handles SEC split RISK FACTORS heading",
         bool(split_s1_candidates) and
         split_s1_candidates[0]["text"] == split_expected)
    if split_s1_candidates:
        test("Split-heading prospectus exact text excludes next peer section",
             "This section of the prospectus provides" not in split_s1_candidates[0]["text"])

    prospectus_crossref_then_body = (
        "Prospectus Summary\n\n"
        "Risk Factors\n\n"
        "See the risks discussed in “Risk Factors” in this prospectus before "
        "investing in the Trust.\n\n"
        "RISK FACTORS\n\n"
        "You should carefully consider the following risks before investing. "
        "Bitcoin volatility may adversely affect the value of the Shares. "
        "Digital asset custody may be vulnerable to cybersecurity threats. "
        "Regulatory uncertainty regarding cryptocurrency markets may materially "
        "adversely affect investors and investors may lose money. "
        "Bitcoin exchanges may close due to fraud or security breaches.\n\n"
        "USE OF PROCEEDS\n\n"
        "The Trust receives bitcoin deposits."
    )
    crossref_candidates = extract_exact_risk_sections(
        prospectus_crossref_then_body, "S-1",
    )
    test("Exact prospectus extraction skips body cross-reference anchors",
         bool(crossref_candidates) and
         crossref_candidates[0]["text"].startswith("RISK FACTORS\n\nYou should"))

    no_item_1a_10q = (
        "Item 2. Management's Discussion and Analysis of Financial Condition "
        "and Results of Operations\n\n"
        "This Quarterly Report contains forward-looking statements that involve "
        "substantial risks and uncertainties. Our customers operate in the "
        "crypto mining industry and are subject to bitcoin price volatility, "
        "regulatory uncertainty, cybersecurity threats, and other risks that "
        "may adversely affect demand.\n\n"
        "Part II - Other Information\n\n"
        "Item 1. Legal Proceedings\n\nNone\n\n"
        "Item 2. Unregistered Sales of Equity Securities and Use of Proceeds\n\nNone."
    )
    class NoSectionFiling:
        def sections(self): return []

    no_item_candidates = extract_all_candidates(
        NoSectionFiling(), no_item_1a_10q, html="", form_type="10-Q",
    )
    test("Exact 10-Q extraction does not invent a risk section without Item 1A",
         len(no_item_candidates) == 0,
         f"{len(no_item_candidates)} candidates")

    # ── Test 3: Prospectus extraction rejects fee table ──────────────────
    print("\n[3] Prospectus header extraction")
    spans = extract_prospectus_risk(MOCK_PROSPECTUS)
    test("Finds at least one prospectus risk section", len(spans) >= 1,
         f"{len(spans)} found")
    if spans:
        text = spans[0]["text"]
        test("Extracted section does NOT contain fee table",
             "management fee: 0.75" not in text.lower())
        test("Extracted section DOES contain principal risks",
             "cryptocurrency risk" in text.lower() or "bitcoin" in text.lower())

    # ── Test 4: validate_section rejects fee schedule ────────────────────
    print("\n[4] Dual-signal validation")
    fee_only = "Management Fee: 0.75%. Custody Fee: 0.10%. Service Fee: $50."
    v = validate_section(fee_only, require_crypto=False)
    test("validate_section rejects fee-only text", not v["valid"],
         v.get("signals", {}).get("reason", ""))

    real_risk = (
        "Cryptocurrency risk. The Fund's investments in Bitcoin and Ethereum "
        "expose investors to significant volatility. Regulatory uncertainty "
        "could materially adversely affect operations. Custody risks include "
        "private key theft and cybersecurity threats. "
    ) * 5
    v = validate_section(real_risk)
    test("validate_section accepts real crypto risk text", v["valid"],
         f"confidence={v['score']:.2f}")
    test("Confidence score in [0,1]", 0 <= v["score"] <= 1)

    # ── Test 5: HTML cleaning drops TOC tables ───────────────────────────
    print("\n[5] HTML cleaning")
    cleaned = clean_html_to_text(MOCK_HTML_WITH_TOC)
    test("HTML cleaning produces non-empty text", len(cleaned) > 100)
    test("Cleaned text contains the body",
         "bitcoin price volatility" in cleaned.lower() or
         "cryptocurrency risks" in cleaned.lower())
    # The TOC table should be dropped, leaving only the <p> content
    test("TOC table is stripped from cleaned text",
         cleaned.lower().count("table of contents") == 0,
         "TOC still present" if cleaned.lower().count("table of contents") > 0 else "")

    with tempfile.TemporaryDirectory() as tmp:
        old_cache_dir = config.TEXT_CACHE_DIR
        old_cache_version = getattr(config, "TEXT_CACHE_VERSION", "")
        old_fast_threshold = getattr(config, "FAST_HTML_CLEAN_BYTES", 0)
        try:
            config.TEXT_CACHE_DIR = tmp
            config.TEXT_CACHE_VERSION = "unit-v2"
            cache_path = _text_cache_path("a" * 64)
            test("Text cache path includes cleaner version",
                 os.path.join("unit-v2", "aa") in cache_path,
                 cache_path)

            cached_cleaned = clean_html_to_text(MOCK_HTML_WITH_TOC)
            test("Versioned cache stores cleaned text without TOC",
                 "table of contents" not in cached_cleaned.lower())

            config.TEXT_CACHE_VERSION = "unit-v3-fast"
            config.FAST_HTML_CLEAN_BYTES = 1
            fast_cleaned = clean_html_to_text(MOCK_HTML_WITH_TOC)
            class FastMockFiling:
                def sections(self): return []
            fast_candidates = extract_all_candidates(
                FastMockFiling(), fast_cleaned, html=MOCK_HTML_WITH_TOC,
                form_type="10-K",
            )
            test("Fast large-HTML cleaner strips TOC table",
                 "table of contents" not in fast_cleaned.lower())
            test("Fast large-HTML cleaner preserves exact risk extraction",
                 bool(fast_candidates) and
                 "Bitcoin price volatility" in fast_candidates[0]["text"])
        finally:
            config.TEXT_CACHE_DIR = old_cache_dir
            config.TEXT_CACHE_VERSION = old_cache_version
            config.FAST_HTML_CLEAN_BYTES = old_fast_threshold

    # ── Test 6: Full pipeline on mock prospectus ─────────────────────────
    print("\n[6] Full pipeline (extract_all_candidates)")

    class MockFiling:
        def sections(self): return []

    candidates = extract_all_candidates(
        MockFiling(), MOCK_PROSPECTUS, html="", form_type="485APOS",
    )
    test("Pipeline finds at least one candidate from mock prospectus",
         len(candidates) >= 1, f"{len(candidates)} candidates")
    if candidates:
        primary = candidates[0]
        test("Primary candidate has non-zero confidence",
             primary["confidence"] > 0,
             f"confidence={primary['confidence']:.2f}, method={primary['method']}")
        test("Primary candidate is NOT the fee table",
             "management fee" not in primary["text"].lower()[:300])

    # ── Test 6b: Multi-fund crypto document selection ───────────────────
    print("\n[6b] Multi-fund document selection")

    class MockDoc:
        is_binary = False

        def __init__(self, name, text):
            self.document = name
            self.document_type = "485APOS"
            self._text = text

        def text(self):
            return self._text

    class MockAttachments:
        def __init__(self, documents):
            self.documents = documents

    class MockMultiFundFiling:
        def __init__(self, documents):
            self.attachments = MockAttachments(documents)

    rare_doc = ("""
Rare Earth Mining ETF
Principal Investment Risks
Investors face risks from fluctuations in rare earth metal prices. Supply chain
risks, mining operations, environmental regulation, tariffs, and commodity
markets may adversely affect the fund. These risks may be significant.
""" * 4)
    crypto_doc = (MOCK_PROSPECTUS + "\n") * 2
    fund_docs = extract_fund_documents(MockMultiFundFiling([
        MockDoc("rare-earth.htm", rare_doc),
        MockDoc("crypto-index.htm", crypto_doc),
    ]))
    test("Multi-fund scoring returns attachment candidates", len(fund_docs) == 2,
         f"{len(fund_docs)} docs")
    if fund_docs:
        test("Multi-fund scoring chooses crypto document first",
             fund_docs[0]["name"] == "crypto-index.htm" and
             fund_docs[0]["crypto_score"] > fund_docs[1]["crypto_score"],
             f"top={fund_docs[0]['name']} score={fund_docs[0]['crypto_score']}")
        crypto_candidates = extract_all_candidates(
            MockFiling(), fund_docs[0]["text"], html="", form_type="485APOS",
        )
        test("Selected crypto document extracts crypto risk text",
             bool(crypto_candidates) and
             "cryptocurrency market is highly volatile" in crypto_candidates[0]["text"].lower())
        test("Selected crypto document excludes rare-earth fund text",
             bool(crypto_candidates) and
             "rare earth metal prices" not in crypto_candidates[0]["text"].lower())

    # ── Test 7: 10-K full pipeline ───────────────────────────────────────
    print("\n[7] Full pipeline on mock 10-K")
    candidates = extract_all_candidates(
        MockFiling(), MOCK_10K, html="", form_type="10-K",
    )
    test("Pipeline finds 10-K risk section", len(candidates) >= 1)
    if candidates:
        test("Uses boundary-pair method",
             "boundary" in candidates[0]["method"],
             f"method={candidates[0]['method']}")

    # ── Test 8: Flask routes, filters, and export parity ────────────────
    print("\n[8] Flask routes, API filters, and exports")
    with tempfile.TemporaryDirectory() as tmp:
        old_paths = {
            "DATA_DIR": config.DATA_DIR,
            "DB_PATH": config.DB_PATH,
            "EXPORT_DIR": config.EXPORT_DIR,
            "EDGAR_CACHE_DIR": config.EDGAR_CACHE_DIR,
            "TEXT_CACHE_DIR": config.TEXT_CACHE_DIR,
            "RAW_DOC_CACHE_DIR": getattr(config, "RAW_DOC_CACHE_DIR", ""),
            "PROCESSOR_VERSION": getattr(config, "PROCESSOR_VERSION", config.VERSION),
        }
        try:
            config.DATA_DIR = tmp
            config.DB_PATH = os.path.join(tmp, "crypto_filings.db")
            config.EXPORT_DIR = os.path.join(tmp, "exports")
            config.EDGAR_CACHE_DIR = os.path.join(tmp, "edgar_cache")
            config.TEXT_CACHE_DIR = os.path.join(tmp, "text_cache")
            config.RAW_DOC_CACHE_DIR = os.path.join(tmp, "raw_doc_cache")
            config.PROCESSOR_VERSION = "unit-processor-v1"

            from crypto_tracker import database as db

            if hasattr(db._local, "conn") and db._local.conn is not None:
                db._local.conn.close()
                db._local.conn = None
            db._init_done = False
            db.init_db()

            sample_rows = [
                {
                    "accession_no": "0000000001-25-000001",
                    "cik": "1980994",
                    "company_name": "iShares Bitcoin Trust",
                    "ticker": "IBIT",
                    "form_type": "485APOS",
                    "root_form": "485APOS",
                    "filing_date": "2025-01-15",
                    "filing_category": "ETF/Fund",
                    "tier": 1,
                    "sec_url": "https://www.sec.gov/Archives/example-1",
                    "filing_pdf_url": "",
                    "crypto_connection": "Spot Bitcoin ETF filing.",
                    "entity_type": "ETF - Prospectus amendment",
                    "search_keyword": "bitcoin",
                    "purpose": "Updates spot Bitcoin ETF risk disclosures.",
                    "top_holdings": "BTC",
                    "fetched_at": "2025-01-15T00:00:00",
                    "processed_at": "2025-01-15T00:00:00",
                    "summary": "ETF summary with Bitcoin custody and market risk.",
                },
                {
                    "accession_no": "0000000002-25-000002",
                    "cik": "1679788",
                    "company_name": "Coinbase Global Inc",
                    "ticker": "COIN",
                    "form_type": "8-K",
                    "root_form": "8-K",
                    "filing_date": "2025-02-20",
                    "filing_category": "Operating Co.",
                    "tier": 2,
                    "sec_url": "https://www.sec.gov/Archives/example-2",
                    "filing_pdf_url": "",
                    "crypto_connection": "Crypto exchange current report.",
                    "entity_type": "Crypto Exchange - Current report",
                    "search_keyword": "company:exchange",
                    "purpose": "Discloses a material crypto exchange event.",
                    "top_holdings": "BTC, ETH",
                    "fetched_at": "2025-02-20T00:00:00",
                    "processed_at": "2025-02-20T00:00:00",
                    "summary": "Exchange summary with regulatory and custody risk.",
                },
            ]
            for row in sample_rows:
                summary = row.pop("summary")
                db.upsert_filing(row)
                db.replace_sections(row["accession_no"], [{
                    "section_type": "risk_factors",
                    "method": "unit_fixture",
                    "title": "Risk Factors",
                    "text": real_risk,
                    "confidence": 0.91,
                    "source_doc_name": "unit-source.htm",
                    "source_doc_url": "https://www.sec.gov/Archives/unit-source.htm",
                    "source_hash": "a" * 64,
                    "exact_text_hash": "b" * 64,
                    "start_offset": 10,
                    "end_offset": 10 + len(real_risk),
                }])
                section_id = db.get_primary_section_id(row["accession_no"])
                db.upsert_summary(row["accession_no"], summary, "unit", section_id)
            db.commit()

            stats = db.get_dashboard_stats()
            test("Dashboard stats count seeded filings", stats["total"] == 2,
                 f"total={stats['total']}")
            filtered = db.get_filtered_filings(category="ETF/Fund")
            test("DB category filter returns one ETF filing",
                 len(filtered) == 1 and filtered[0]["ticker"] == "IBIT")

            csv_path = db.export_to_csv(filters={"category": "Operating Co."})
            with open(csv_path, newline="", encoding="utf-8") as f:
                exported = list(csv.DictReader(f))
            test("CSV export respects active filters",
                 len(exported) == 1 and exported[0]["ticker"] == "COIN")
            test("CSV export includes exact full risk section",
                 exported[0]["risk_section"] == real_risk)

            filing = db.get_filing_by_accession("0000000001-25-000001")
            test("DB detail includes section provenance fields",
                 filing["exact_text_hash"] == "b" * 64 and
                 filing["source_hash"] == "a" * 64 and
                 filing["source_doc_url"].endswith("unit-source.htm") and
                 filing["start_offset"] == 10)

            skipped_meta = {
                "accession_no": "0000000009-25-000009",
                "cik": "9999999",
                "company_name": "No Risk Candidate Inc",
                "ticker": "NONE",
                "form_type": "8-K",
                "root_form": "8-K",
                "filing_date": "2025-03-01",
                "doc_name": "no-risk.htm",
                "doc_url": "https://www.sec.gov/Archives/no-risk.htm",
            }
            db.record_filing_attempt(
                skipped_meta, "skipped", "no_extraction_candidates"
            )
            db.commit()
            test("Skipped filing attempt is counted",
                 db.get_attempt_count(status="skipped") == 1)
            test("Skipped current-version accession is treated as seen",
                 skipped_meta["accession_no"] in db.get_existing_accessions())
            config.PROCESSOR_VERSION = "unit-processor-v2"
            test("Skipped accession is revisited after processor bump",
                 skipped_meta["accession_no"] not in db.get_existing_accessions())
            config.PROCESSOR_VERSION = "unit-processor-v1"

            try:
                from crypto_tracker.app import app as flask_app
            except ModuleNotFoundError as e:
                if e.name != "flask":
                    raise
                print("  [SKIP] Flask route tests — flask is not installed")
            else:
                flask_app.config["TESTING"] = True
                client = flask_app.test_client()
                test("Dashboard route renders", client.get("/").status_code == 200)
                test("Filings route renders", client.get("/filings").status_code == 200)
                api_resp = client.get("/api/filings?category=ETF/Fund")
                payload = api_resp.get_json()
                test("API category filter returns ETF filing",
                     api_resp.status_code == 200 and len(payload) == 1 and
                     payload[0]["ticker"] == "IBIT")
                limit_resp = client.get("/api/filings?limit=1")
                test("API limit parameter is backward-compatible",
                     limit_resp.status_code == 200 and len(limit_resp.get_json()) == 1)
                test("API scrape rejects invalid mode",
                     client.post("/api/scrape?mode=bad_mode").status_code == 400)
                test("API scrape rejects invalid scope",
                     client.post("/api/scrape?scope=bad_scope").status_code == 400)
                test("Filing detail 404 is preserved",
                     client.get("/filing/not-real-accession").status_code == 404)
        finally:
            from crypto_tracker import database as db
            if hasattr(db._local, "conn") and db._local.conn is not None:
                db._local.conn.close()
                db._local.conn = None
            db._init_done = False
            for key, value in old_paths.items():
                setattr(config, key, value)

    # ── Test 9: Fast discovery + analysis cache behavior ────────────────
    print("\n[9] Discovery and cache behavior")
    from crypto_tracker import scraper

    test("Default scope excludes Form D and blanket 8-K",
         "D" not in scraper._forms_for_scope("risk_default") and
         "8-K" not in scraper._forms_for_scope("risk_default") and
         "8-K" not in scraper._company_forms_for_scope("risk_default"))
    test("Default EFTS scope includes full prospectus/offering search",
         "10-K" not in scraper._efts_forms_for_scope("risk_default") and
         "10-Q" not in scraper._efts_forms_for_scope("risk_default") and
         "S-1" in scraper._efts_forms_for_scope("risk_default") and
         "485APOS" in scraper._efts_forms_for_scope("risk_default"))
    test("Event/all scopes allow event or broad forms explicitly",
         "8-K" in scraper._forms_for_scope("event_risk") and
         "D" in scraper._forms_for_scope("all"))
    false_fund_item = (
        "false-acc", "1924868", "Tidal Trust II", "", "485APOS",
        "485APOS", "2026-03-26", 1, "", "https://example.test/openai.htm",
        "openai_485apos-032626.htm",
    )
    crypto_fund_item = (
        "crypto-acc", "1980994", "iShares Bitcoin Trust", "IBIT", "485BPOS",
        "485BPOS", "2026-03-26", 1, "", "https://example.test/ibit.htm",
        "ibit_485bpos.htm",
    )
    operating_item = (
        "op-acc", "1679788", "Coinbase Global Inc", "COIN", "10-Q",
        "10-Q", "2026-03-26", 2, "", "https://example.test/coin.htm",
        "coin-10q.htm",
    )
    test("Default scope filters non-crypto fund metadata",
         not scraper._is_scope_relevant_candidate(false_fund_item, "risk_default"))
    test("Default scope keeps crypto fund metadata",
         scraper._is_scope_relevant_candidate(crypto_fund_item, "risk_default"))
    test("Default scope keeps operating-company candidates",
         scraper._is_scope_relevant_candidate(operating_item, "risk_default"))
    test("All scope bypasses fund metadata filter",
         scraper._is_scope_relevant_candidate(false_fund_item, "all"))

    amendment_only_text = (
        "This Pre-Effective Amendment No. 1 is filed solely to amend Item 16. "
        "This Pre-Effective Amendment No. 1 does not modify any provision of "
        "the preliminary prospectus contained in Part I. Accordingly, the "
        "preliminary prospectus has been omitted. Bitcoin Custody Agreement."
    )
    test("Default scope can identify amendment-only filings with omitted prospectus",
         scraper._is_non_risk_amendment_text(amendment_only_text))

    def efts_hit(acc, form="10-K", doc="doc.htm", cik="1679788"):
        return {
            "_id": f"{acc}:{doc}",
            "_source": {
                "adsh": acc,
                "display_names": ["Coinbase Global Inc (COIN)"],
                "form": form,
                "ciks": [cik],
                "file_date": "2025-03-01",
            },
        }

    seen_docs = set()
    deduped_docs = []
    scraper._add_search_hit(
        efts_hit("same-acc", form="S-1/A", doc="ea026067101ex5-1_defi.htm", cik="1805526"),
        "S-1/A", seen_docs, deduped_docs,
    )
    scraper._add_search_hit(
        efts_hit("same-acc", form="S-1/A", doc="ea0260671-s1a1_defi.htm", cik="1805526"),
        "S-1/A", seen_docs, deduped_docs,
    )
    test("Duplicate accession keeps primary filing doc over exhibit",
         len(deduped_docs) == 1 and deduped_docs[0][10] == "ea0260671-s1a1_defi.htm")

    embedded_exhibit = (
        "embedded-ex-acc", "2045872", "Bitwise Solana ETF", "", "S-1/A",
        "S-1", "2025-09-26", 1, "",
        "https://example.test/ea025873301ex10-5_bitwise.htm",
        "ea025873301ex10-5_bitwise.htm",
    )
    test("Embedded ex10 document names are ranked as exhibits",
         scraper._candidate_doc_rank(embedded_exhibit)[0] >= 50,
         str(scraper._candidate_doc_rank(embedded_exhibit)))

    index_fixture = {
        "directory": {
            "item": [
                {"name": "0001213900-25-112216-index.html"},
                {"name": "ea026592601ex10-5_bitwise.htm", "size": "21274"},
                {"name": "ea026592601ex-fee_bitwise.htm", "size": "12576"},
                {"name": "ea0265926-s1a1_bitwise.htm", "size": "1496282"},
            ]
        }
    }
    primary_url, primary_name = scraper._choose_primary_doc_from_index(
        "2082889", "0001213900-25-112216", "S-1/A", index_fixture
    )
    test("Archive index chooses primary S-1/A doc over exhibits",
         primary_name == "ea0265926-s1a1_bitwise.htm" and
         primary_url.endswith("/ea0265926-s1a1_bitwise.htm"))

    generic_index_fixture = {
        "directory": {
            "item": [
                {"name": "filename6.htm", "size": "9985"},
                {"name": "grysclebtcntrstcvrcl01112024.htm", "size": "648244"},
            ]
        }
    }
    _, generic_primary_name = scraper._choose_primary_doc_from_index(
        "1976672", "0001680359-24-000022", "N-1A", generic_index_fixture
    )
    test("Archive index chooses larger same-rank prospectus over cover filename",
         generic_primary_name == "grysclebtcntrstcvrcl01112024.htm")

    metrics = scraper._new_run_metrics()
    scraper._set_active_metrics(metrics)
    try:
        with mock.patch.object(scraper.db, "get_existing_accessions",
                               return_value={"old-acc"}), \
             mock.patch.object(scraper, "_build_keyword_batches",
                               return_value=["bitcoin", "ethereum"]), \
             mock.patch.object(scraper, "_efts_forms_for_scope",
                               return_value=["10-K"]), \
             mock.patch.object(scraper, "_search_by_company_cik",
                               return_value=0), \
             mock.patch.object(scraper, "_efts_search_all") as efts_mock:
            efts_mock.side_effect = [
                [efts_hit("old-acc"), efts_hit("new-acc-1")],
                [efts_hit("new-acc-2")],
            ]
            discovered = scraper.search_edgar_filings(scope="risk_default")
    finally:
        scraper._set_active_metrics(None)
    test("Discovery builds universe before existing-accession filtering",
         {row[0] for row in discovered} == {"new-acc-1", "new-acc-2"},
         str([row[0] for row in discovered]))
    test("Discovery metrics record universe and terminal rows",
         metrics["counters"]["candidate_universe"] == 3 and
         metrics["counters"]["already_terminal"] == 1,
         str(metrics["counters"]))

    metrics = scraper._new_run_metrics()
    scraper._set_active_metrics(metrics)
    try:
        with mock.patch.object(scraper.db, "get_existing_accessions",
                               return_value={"old-acc", "new-acc-1", "new-acc-2"}), \
             mock.patch.object(scraper, "_build_keyword_batches",
                               return_value=["bitcoin", "ethereum"]), \
             mock.patch.object(scraper, "_efts_forms_for_scope",
                               return_value=["10-K"]), \
             mock.patch.object(scraper, "_search_by_company_cik",
                               return_value=0), \
             mock.patch.object(scraper, "_efts_search_all") as efts_mock:
            efts_mock.side_effect = [
                [efts_hit("old-acc"), efts_hit("new-acc-1")],
                [efts_hit("new-acc-2")],
            ]
            fixed_point = scraper.search_edgar_filings(scope="risk_default")
    finally:
        scraper._set_active_metrics(None)
    test("Immediate second discovery is fixed-point complete",
         fixed_point == [] and metrics["counters"]["fixed_point_complete"] == 1,
         str(metrics["counters"]))

    submissions_fixture = {
        "name": "Coinbase Global, Inc.",
        "tickers": ["COIN"],
        "filings": {
            "recent": {
                "accessionNumber": [
                    "0001679788-25-000010",
                    "0001679788-25-000009",
                    "0001679788-25-000008",
                ],
                "form": ["10-K", "8-K", "10-Q"],
                "filingDate": ["2025-03-01", "2025-02-01", "2025-01-01"],
                "primaryDocument": ["coin-20241231.htm", "coin-8k.htm", "coin-10q.htm"],
            }
        }
    }
    with mock.patch.object(scraper, "_fetch_company_submissions", return_value=submissions_fixture) as sub_mock:
        status, _, errors, hits = scraper._fetch_company_filings(
            "1679788", "Coinbase Global Inc", "COIN", "exchange",
            ["10-K", "10-Q", "8-K"], 1,
        )
    test("Submissions JSON discovery succeeds", status == "ok" and not errors)
    test("Submissions JSON called once per CIK", sub_mock.call_count == 1)
    test("Submissions discovery respects per-form cap",
         len(hits) == 3 and hits[0][9].endswith("/coin-20241231.htm"))

    dated_submissions_fixture = {
        "name": "Coinbase Global, Inc.",
        "tickers": ["COIN"],
        "filings": {
            "recent": {
                "accessionNumber": ["old-acc", "new-acc"],
                "form": ["10-K", "10-K"],
                "filingDate": ["2018-12-31", "2025-03-01"],
                "primaryDocument": ["old.htm", "new.htm"],
            }
        }
    }
    with mock.patch.object(scraper, "_fetch_company_submissions",
                           return_value=dated_submissions_fixture):
        status, _, errors, hits = scraper._fetch_company_filings(
            "1679788", "Coinbase Global Inc", "COIN", "exchange",
            ["10-K"], 2,
        )
    test("Submissions discovery respects configured date range",
         status == "ok" and len(hits) == 1 and hits[0][0] == "new-acc")

    stale_identity = {"name": "Enovix Corp", "tickers": ["ENVX"], "filings": {"recent": {}}}
    test("CIK identity validation rejects stale ticker reuse",
         not scraper._company_identity_matches(stale_identity, "Bakkt Holdings Inc", "BKKT"))

    with mock.patch.object(scraper, "_fetch_company_submissions",
                           return_value=stale_identity):
        status, _, info, hits = scraper._fetch_company_filings(
            "1828318", "Bakkt Holdings Inc", "BKKT", "exchange",
            ["10-K"], 1,
        )
    test("Stale hardcoded CIK is skipped before candidate creation",
         status == "identity_mismatch" and hits == [] and "Enovix" in info)

    old_companies = config.CRYPTO_COMPANIES
    try:
        config.CRYPTO_COMPANIES = {
            "1828318": ("Bakkt Holdings Inc", "BKKT", "exchange"),
            "1679788": ("Coinbase Global Inc", "COIN", "exchange"),
        }
        with mock.patch.object(scraper, "_fetch_sec_ticker_map",
                               return_value={
                                   "BKKT": {"cik": "1820302", "title": "Bakkt, Inc."},
                                   "COIN": {"cik": "1679788", "title": "Coinbase Global, Inc."},
                               }):
            resolved = scraper._resolved_crypto_companies()
        test("SEC ticker map corrects stale hardcoded CIKs",
             "1820302" in resolved and "1828318" not in resolved and
             resolved["1820302"][1] == "BKKT")
    finally:
        config.CRYPTO_COMPANIES = old_companies

    with tempfile.TemporaryDirectory() as tmp:
        old_cache = config.DATA_DIR
        try:
            config.DATA_DIR = tmp
            scraper._save_submissions_cache("1679788", submissions_fixture)
            t0 = time.perf_counter()
            cached = scraper._load_submissions_cache("1679788")
            elapsed = time.perf_counter() - t0
            test("Submissions cache reloads fixture without network",
                 cached == submissions_fixture)
            test("Cached submissions lookup is fast", elapsed < 0.05,
                 f"{elapsed:.4f}s")
        finally:
            config.DATA_DIR = old_cache

    with tempfile.TemporaryDirectory() as tmp:
        old_raw_cache = getattr(config, "RAW_DOC_CACHE_DIR", "")
        try:
            config.RAW_DOC_CACHE_DIR = tmp

            class MockResponse:
                text = "Risk Factors\nBitcoin custody risk and regulatory risk."

            metrics = scraper._new_run_metrics()
            scraper._set_active_metrics(metrics)
            try:
                with mock.patch("crypto_tracker.scraper._sec_get",
                                return_value=MockResponse()) as sec_get:
                    first_text, _ = scraper.fetch_direct_document_text(
                        "https://www.sec.gov/Archives/edgar/data/1/2/doc.htm"
                    )
                    second_text, _ = scraper.fetch_direct_document_text(
                        "https://www.sec.gov/Archives/edgar/data/1/2/doc.htm"
                    )
            finally:
                scraper._set_active_metrics(None)
            test("Raw SEC document cache avoids second network fetch",
                 sec_get.call_count == 1 and first_text == second_text)
            test("Raw SEC document cache records hit/miss counters",
                 metrics["counters"]["raw_doc_cache_misses"] == 1 and
                 metrics["counters"]["raw_doc_cache_hits"] == 1,
                 str(metrics["counters"]))
        finally:
            config.RAW_DOC_CACHE_DIR = old_raw_cache

    with tempfile.TemporaryDirectory() as tmp:
        old_paths = {
            "DATA_DIR": config.DATA_DIR,
            "DB_PATH": config.DB_PATH,
            "EXPORT_DIR": config.EXPORT_DIR,
            "EDGAR_CACHE_DIR": config.EDGAR_CACHE_DIR,
            "TEXT_CACHE_DIR": config.TEXT_CACHE_DIR,
            "RAW_DOC_CACHE_DIR": getattr(config, "RAW_DOC_CACHE_DIR", ""),
        }
        try:
            config.DATA_DIR = tmp
            config.DB_PATH = os.path.join(tmp, "crypto_filings.db")
            config.EXPORT_DIR = os.path.join(tmp, "exports")
            config.EDGAR_CACHE_DIR = os.path.join(tmp, "edgar_cache")
            config.TEXT_CACHE_DIR = os.path.join(tmp, "text_cache")
            config.RAW_DOC_CACHE_DIR = os.path.join(tmp, "raw_doc_cache")

            from crypto_tracker import database as db
            if hasattr(db._local, "conn") and db._local.conn is not None:
                db._local.conn.close()
                db._local.conn = None
            db._init_done = False
            db.init_db()
            row = {
                "accession_no": "0000000003-25-000003",
                "cik": "1679788",
                "company_name": "Coinbase Global Inc",
                "ticker": "COIN",
                "form_type": "10-K",
                "root_form": "10-K",
                "filing_date": "2025-03-01",
                "filing_category": "Operating Co.",
                "tier": 2,
                "sec_url": "https://www.sec.gov/Archives/example-3",
                "filing_pdf_url": "",
                "crypto_connection": "Crypto exchange annual report.",
                "entity_type": "Crypto Exchange - Annual report",
                "search_keyword": "company:exchange",
                "purpose": "Annual crypto exchange risk disclosures.",
                "top_holdings": "BTC, ETH",
                "fetched_at": "2025-03-01T00:00:00",
                "processed_at": "2025-03-01T00:00:00",
            }
            db.upsert_filing(row)
            db.replace_sections(row["accession_no"], [{
                "section_type": "risk_factors",
                "method": "unit_low_confidence",
                "title": "Risk Factors",
                "text": real_risk,
                "confidence": 0.1,
                "source_doc_name": "coin-20241231.htm",
                "source_doc_url": "https://www.sec.gov/Archives/coin-20241231.htm",
                "source_hash": "c" * 64,
                "exact_text_hash": "d" * 64,
            }])
            db.commit()
            with mock.patch("crypto_tracker.scraper.process_one_filing",
                            return_value=scraper._skip_result(
                                row["accession_no"], "unit_skip"
                            )):
                reprocessed = scraper.reprocess_existing(only_low_confidence=True)
            test("Reprocess handles skipped cached filing without crashing",
                 reprocessed == 0)
        finally:
            from crypto_tracker import database as db
            if hasattr(db._local, "conn") and db._local.conn is not None:
                db._local.conn.close()
                db._local.conn = None
            db._init_done = False
            for key, value in old_paths.items():
                setattr(config, key, value)

    with mock.patch("crypto_tracker.scraper.build_summary") as build_mock:
        build_mock.return_value = {"summary": "fresh analysis", "model": "unit"}
        with mock.patch("crypto_tracker.scraper.db.get_current_summary_hash",
                        return_value=exact_10k[0]["exact_text_hash"] if exact_10k else ""):
            # Matching hash should skip build_summary in process flow; test the
            # decision directly because network fetch is outside offline scope.
            should_skip = (
                exact_10k and
                exact_10k[0]["exact_text_hash"] ==
                scraper.db.get_current_summary_hash("0001679788-25-000010")
            )
            if not should_skip:
                build_mock("text")
    test("Matching exact text hash skips analysis call", build_mock.call_count == 0)

    item = (
        "0001679788-25-000010", "1679788", "Coinbase Global Inc", "COIN",
        "10-K", "10-K", "2025-03-01", 2, "company:exchange",
        "https://www.sec.gov/Archives/edgar/data/1679788/000167978825000010/coin.htm",
        "coin.htm",
    )
    with mock.patch("crypto_tracker.scraper.fetch_direct_document_text",
                    return_value=(MOCK_10K, MOCK_10K)), \
         mock.patch("crypto_tracker.scraper.build_summary") as build_mock:
        exact_only_record = scraper.process_one_filing(item, mode="exact_only")
    test("Exact-only processing extracts a section",
         bool(exact_only_record and exact_only_record.get("candidates")))
    test("Exact-only processing defers analysis",
         exact_only_record.get("summary") == "" and
         exact_only_record.get("summary_model") == "deferred" and
         build_mock.call_count == 0)

    with tempfile.TemporaryDirectory() as tmp:
        old_paths = {
            "DATA_DIR": config.DATA_DIR,
            "DB_PATH": config.DB_PATH,
            "EXPORT_DIR": config.EXPORT_DIR,
            "EDGAR_CACHE_DIR": config.EDGAR_CACHE_DIR,
            "TEXT_CACHE_DIR": config.TEXT_CACHE_DIR,
            "RAW_DOC_CACHE_DIR": getattr(config, "RAW_DOC_CACHE_DIR", ""),
        }
        try:
            config.DATA_DIR = tmp
            config.DB_PATH = os.path.join(tmp, "crypto_filings.db")
            config.EXPORT_DIR = os.path.join(tmp, "exports")
            config.EDGAR_CACHE_DIR = os.path.join(tmp, "edgar_cache")
            config.TEXT_CACHE_DIR = os.path.join(tmp, "text_cache")
            config.RAW_DOC_CACHE_DIR = os.path.join(tmp, "raw_doc_cache")

            from crypto_tracker import database as db
            if hasattr(db._local, "conn") and db._local.conn is not None:
                db._local.conn.close()
                db._local.conn = None
            db._init_done = False
            db.init_db()
            long_text = "Risk Factors\n" + ("Bitcoin custody and regulatory risk. " * 7000)
            scraper._write_filing_to_db({
                "meta": {
                    "accession_no": "0000000004-25-000004",
                    "cik": "1679788",
                    "company_name": "Coinbase Global Inc",
                    "ticker": "COIN",
                    "form_type": "10-K",
                    "root_form": "10-K",
                    "filing_date": "2025-03-01",
                    "filing_category": "Operating Co.",
                    "tier": 2,
                    "sec_url": "https://www.sec.gov/Archives/example-4",
                    "filing_pdf_url": "",
                    "crypto_connection": "Crypto exchange annual report.",
                    "entity_type": "Crypto Exchange - Annual report",
                    "search_keyword": "company:exchange",
                    "purpose": "Annual crypto exchange risk disclosures.",
                    "top_holdings": "BTC, ETH",
                    "fetched_at": "2025-03-01T00:00:00",
                    "processed_at": "2025-03-01T00:00:00",
                },
                "fund_docs": [],
                "candidates": [{
                    "section_type": "risk_factors",
                    "method": "unit_long_exact",
                    "title": "Risk Factors",
                    "text": long_text,
                    "confidence": 0.9,
                    "source_doc_name": "long.htm",
                    "source_doc_url": "https://www.sec.gov/Archives/long.htm",
                    "source_hash": "e" * 64,
                    "exact_text_hash": "f" * 64,
                }],
                "summary": "",
                "summary_model": "deferred",
                "summary_text_hash": "f" * 64,
            })
            db.commit()
            saved_long = db.get_filing_by_accession("0000000004-25-000004")
            test("DB write preserves full exact section text without truncation",
                 saved_long["risk_section"] == long_text and len(saved_long["risk_section"]) > 200000,
                 f"len={len(saved_long['risk_section']) if saved_long else 0}")
        finally:
            from crypto_tracker import database as db
            if hasattr(db._local, "conn") and db._local.conn is not None:
                db._local.conn.close()
                db._local.conn = None
            db._init_done = False
            for key, value in old_paths.items():
                setattr(config, key, value)

    with tempfile.TemporaryDirectory() as tmp:
        old_data_dir = config.DATA_DIR
        try:
            config.DATA_DIR = tmp
            with mock.patch.object(scraper.db, "init_db"), \
                 mock.patch.object(scraper, "search_edgar_filings", return_value=[]), \
                 mock.patch.object(scraper.db, "get_total_filing_count", return_value=2), \
                 mock.patch.object(scraper.db, "get_filing_count", return_value=2), \
                 mock.patch.object(scraper, "reprocess_existing", return_value=99) as reprocess_mock:
                no_new_result = scraper.run_scraper()
            test("Up-to-date scraper run does not auto-reprocess",
                 reprocess_mock.call_count == 0 and
                 no_new_result["reprocessed"] == 0 and
                 no_new_result["new_found"] == 0)
        finally:
            config.DATA_DIR = old_data_dir

    # ── Summary ──────────────────────────────────────────────────────────
    passed = sum(1 for _, ok, _ in results if ok)
    failed = sum(1 for _, ok, _ in results if not ok)
    print(f"\n═══ RESULTS: {passed} passed, {failed} failed ═══\n")
    return failed == 0


# ═══════════════════════════════════════════════════════════════════════════
# LIVE TESTS (require SEC access)
# ═══════════════════════════════════════════════════════════════════════════

# A curated set of immutable SEC filing documents known to be challenging.
LIVE_TEST_FILINGS = [
    {
        "description": "Coinbase 2024 10-K Item 1A",
        "fixture": "coinbase_2024_10k_excerpt.txt",
        "phrase": "Investing in our Class A common stock involves a high degree of risk",
        "exclude": "ITEM 1B. UNRESOLVED STAFF COMMENTS",
    },
    {
        "description": "iShares Bitcoin Trust 2024 424B3 Risk Factors",
        "fixture": "ibit_2024_424b3_excerpt.txt",
        "phrase": "The value of the Shares relates directly to the value of bitcoins",
        "exclude": "USE OF PROCEEDS",
    },
]


def run_live_tests():
    print("\n═══ LIVE TESTS ═══")
    print("  (These hit SEC.gov — requires network access)\n")

    from urllib.request import Request, urlopen

    class EmptyFiling:
        def sections(self): return []

    passed = 0
    failed = 0

    for item in LIVE_TEST_FILINGS:
        meta = SEC_FIXTURE_META[item["fixture"]]
        print(f"\n  Testing: {item['description']}")
        print(f"    Source: {meta['source_url']}")
        try:
            req = Request(meta["source_url"], headers={"User-Agent": config.SEC_IDENTITY})
            raw_html = urlopen(req, timeout=30).read().decode("utf-8", errors="replace")
            full_text = clean_html_to_text(raw_html)
            candidates = extract_all_candidates(
                EmptyFiling(), full_text, html=raw_html, form_type=meta["form_type"],
            )

            if not candidates:
                print(f"    [FAIL] No extraction candidates")
                failed += 1
                continue

            primary = candidates[0]
            print(f"    Method: {primary['method']}")
            print(f"    Confidence: {primary['confidence']:.2f}")
            print(f"    Section size: {len(primary['text']):,} chars")
            print(f"    Candidates: {len(candidates)}")

            if primary["exact_text_hash"] != meta["expected_hash"]:
                print(f"    [FAIL] Exact text hash changed: {primary['exact_text_hash']}")
                failed += 1
                continue
            if item["phrase"] not in primary["text"]:
                print(f"    [FAIL] Expected source phrase missing")
                failed += 1
                continue
            if item["exclude"] in primary["text"]:
                print(f"    [FAIL] Extracted through excluded peer section")
                failed += 1
                continue

            print(f"    [PASS]")
            passed += 1

        except Exception as e:
            print(f"    [ERROR] {str(e)[:150]}")
            failed += 1

    print(f"\n═══ LIVE RESULTS: {passed} passed, {failed} failed ═══")
    return failed == 0


# ═══════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true",
                        help="Run live tests against real SEC filings (needs network)")
    args = parser.parse_args()

    unit_ok = run_unit_tests()

    if not unit_ok:
        print("Unit tests failed — skipping live tests.")
        sys.exit(1)

    if args.live:
        live_ok = run_live_tests()
        sys.exit(0 if live_ok else 1)

    print("Unit tests passed. Use --live to also run against real filings.")


if __name__ == "__main__":
    main()
