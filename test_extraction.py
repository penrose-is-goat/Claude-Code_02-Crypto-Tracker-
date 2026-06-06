#!/usr/bin/env python3
"""
Extraction test suite v1.1.27.

Two parts:
  (A) UNIT TESTS (run offline, no network) — test regex patterns, HTML
      cleaning, boundary-pair extraction, validation logic against synthetic
      fixtures. These MUST pass before any real-filing test runs.

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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from crypto_tracker.extractor import (
    clean_html_to_text, extract_boundary_pair, extract_prospectus_risk,
    validate_section, extract_all_candidates, CRYPTO_RE, RISK_RE,
    FEE_SCHEDULE_RE, _item_regex,
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

    # ── Test 7: 10-K full pipeline ───────────────────────────────────────
    print("\n[7] Full pipeline on mock 10-K")
    candidates = extract_all_candidates(
        MockFiling(), MOCK_10K, html="", form_type="10-K",
    )
    test("Pipeline finds 10-K risk section", len(candidates) >= 1)
    if candidates:
        test("Uses boundary-pair method",
             "boundary_pair" in candidates[0]["method"],
             f"method={candidates[0]['method']}")

    # ── Summary ──────────────────────────────────────────────────────────
    passed = sum(1 for _, ok, _ in results if ok)
    failed = sum(1 for _, ok, _ in results if not ok)
    print(f"\n═══ RESULTS: {passed} passed, {failed} failed ═══\n")
    return failed == 0


# ═══════════════════════════════════════════════════════════════════════════
# LIVE TESTS (require SEC access)
# ═══════════════════════════════════════════════════════════════════════════

# A curated set of real filings known to be challenging
LIVE_TEST_FILINGS = [
    # (accession_no, expected_crypto_content, description)
    ("0001144204-21-036158", True,  "Grayscale Bitcoin Trust 10-K"),
    ("0001140361-24-015123", True,  "BlackRock iShares Bitcoin Trust S-1"),
    ("0001193125-21-063253", True,  "Coinbase S-1"),
    ("0001398344-24-011100", True,  "Listed Funds Trust 485APOS (multi-fund)"),
    ("0001010549-24-000456", True,  "Morgan Stanley Bitcoin Trust"),
]


def run_live_tests():
    print("\n═══ LIVE TESTS ═══")
    print("  (These hit SEC.gov — requires network access)\n")

    from edgar import set_identity, get_by_accession_number
    from crypto_tracker.extractor import fetch_filing_text

    set_identity(config.SEC_IDENTITY)

    passed = 0
    failed = 0

    for acc, expect_crypto, desc in LIVE_TEST_FILINGS:
        print(f"\n  Testing: {desc} ({acc})")
        try:
            filing = get_by_accession_number(acc)
            if filing is None:
                print(f"    [SKIP] Could not retrieve filing")
                failed += 1
                continue

            full_text, raw_html = fetch_filing_text(filing)
            candidates = extract_all_candidates(
                filing, full_text, html=raw_html, form_type=filing.form,
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

            has_crypto = bool(CRYPTO_RE.search(primary["text"][:20000]))
            if expect_crypto and not has_crypto:
                print(f"    [FAIL] Expected crypto content, got none")
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
