#!/usr/bin/env python3
"""
Extraction Validation Test Script v1.1.26

Run this on your local machine (not in the sandbox) to validate that the
extractor correctly handles different filing types.

Usage:
    python test_extraction.py

This will:
1. Fetch 30+ real filings from SEC EDGAR using edgartools
2. Extract risk sections from each
3. Validate the extraction against known criteria
4. Print a detailed report

EXPECTED: At least 30 out of 40 filings should pass validation.
"""
import sys
import os
import time
import re

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from edgar import set_identity, search_filings, get_by_accession_number
from crypto_tracker.extractor import (
    extract_risk_from_filing, CRYPTO_RE, RISK_RE, FEE_SCHEDULE_RE,
    _has_sufficient_crypto_content,
)
from crypto_tracker.summarizer import build_summary

set_identity("PenroseHayek crypto.hedger23@gmail.com")

# ═══════════════════════════════════════════════════════════════════════════
# TEST CASES — Mix of filing types
# ═══════════════════════════════════════════════════════════════════════════

def find_test_filings():
    """Search EDGAR for a diverse set of crypto filings to test against."""
    print("Searching for test filings...")
    test_filings = []
    seen = set()

    # Search for different types
    searches = [
        ("bitcoin", ["S-1", "S-1/A"], 5),
        ("cryptocurrency", ["10-K"], 5),
        ("digital asset", ["485APOS"], 5),
        ("crypto ETF", ["N-1A"], 5),
        ("blockchain", ["8-K"], 5),
        ("bitcoin", ["485BPOS"], 5),
        ("ethereum", ["S-1"], 3),
        ("stablecoin", ["10-K", "10-Q"], 3),
        ("crypto", ["D"], 4),
    ]

    for kw, forms, count in searches:
        for form in forms:
            try:
                results = search_filings(kw, forms=[form],
                                          start_date="2024-01-01",
                                          end_date="2026-12-31")
                added = 0
                for r in results:
                    if added >= count:
                        break
                    acc = r.accession_number
                    if acc in seen:
                        continue
                    seen.add(acc)
                    test_filings.append({
                        "accession": acc,
                        "company": r.company or "Unknown",
                        "form": r.form or form,
                        "keyword": kw,
                    })
                    added += 1
            except Exception as e:
                print(f"  Warning: search for {kw}/{form} failed: {e}")
            time.sleep(0.15)

    print(f"  Found {len(test_filings)} test filings")
    return test_filings


# ═══════════════════════════════════════════════════════════════════════════
# VALIDATION CRITERIA
# ═══════════════════════════════════════════════════════════════════════════

def validate_extraction(filing_info, risk_text, full_text, summary):
    """Validate that the extraction meets quality criteria.

    Returns (passed: bool, issues: list of strings)
    """
    issues = []
    form = filing_info["form"]

    # 1. Must have some content
    if not full_text or len(full_text) < 200:
        issues.append("FAIL: No document text retrieved")
        return False, issues

    # 2. Must have a summary
    if not summary or len(summary) < 50:
        issues.append("FAIL: No summary generated")
        return False, issues

    # 3. Summary should NOT be just a list of generic categories
    if re.match(r"^Key risk categories \(\d+\):", summary):
        if len(summary) < 150:
            issues.append("WARN: Summary is just generic categories, too short")

    # 4. Summary should describe the filing type
    has_filing_context = any(term in summary.lower() for term in [
        "registration", "annual report", "quarterly", "current report",
        "amendment", "offering", "etf", "fund", "trust", "filing",
        "report", "bitcoin", "crypto", "digital", "blockchain",
    ])
    if not has_filing_context:
        issues.append("WARN: Summary lacks filing context")

    # 5. Risk text should exist for prospectus/registration filings
    risk_forms = {"S-1", "S-1/A", "N-1A", "N-1A/A", "485APOS", "485BPOS", "10-K"}
    root_form = re.sub(r"/A$", "", form)
    if root_form in risk_forms:
        if not risk_text or len(risk_text) < 300:
            issues.append("FAIL: No risk section found for filing type that should have one")
            return False, issues

        # 6. Risk text should discuss crypto (not rare earth mining, etc.)
        if not _has_sufficient_crypto_content(risk_text, threshold=2):
            issues.append("WARN: Risk section may not be crypto-related")

        # 7. Risk text should NOT be a fee schedule
        fee_hits = len(FEE_SCHEDULE_RE.findall(risk_text[:3000]))
        risk_hits = len(RISK_RE.findall(risk_text[:3000]))
        if fee_hits > risk_hits:
            issues.append("FAIL: Risk section is actually a fee schedule")
            return False, issues

        # 8. Risk text should have reasonable length (not just a fragment)
        if len(risk_text) < 1000:
            issues.append("WARN: Risk section seems too short (<1000 chars)")

    # 9. Summary should not copy fee schedule items
    fee_in_summary = FEE_SCHEDULE_RE.search(summary)
    if fee_in_summary:
        issues.append("FAIL: Summary contains fee schedule items")
        return False, issues

    # 10. Summary should not just be the first paragraph copied
    if risk_text and summary in risk_text[:500]:
        issues.append("WARN: Summary appears to be copied from first paragraph")

    passed = not any(issue.startswith("FAIL") for issue in issues)
    return passed, issues


# ═══════════════════════════════════════════════════════════════════════════
# MAIN TEST RUNNER
# ═══════════════════════════════════════════════════════════════════════════

def run_tests():
    print("=" * 70)
    print("  CRYPTO SEC FILING TRACKER — Extraction Validation Tests")
    print("  v1.1.26")
    print("=" * 70)

    test_filings = find_test_filings()
    if len(test_filings) < 10:
        print("ERROR: Could not find enough test filings. Check network connection.")
        return

    results = {"passed": 0, "failed": 0, "errors": 0, "warnings": 0}
    detailed = []

    for i, tf in enumerate(test_filings, 1):
        acc = tf["accession"]
        company = tf["company"][:40]
        form = tf["form"]

        print(f"\n  [{i}/{len(test_filings)}] {company} ({form})")
        print(f"    Accession: {acc}")

        try:
            filing = get_by_accession_number(acc)
            if filing is None:
                print(f"    ERROR: Could not retrieve filing")
                results["errors"] += 1
                continue

            risk_text, full_text = extract_risk_from_filing(filing)
            summary = build_summary(
                risk_text if risk_text else full_text,
                form_type=form,
                company_name=company,
            )

            passed, issues = validate_extraction(tf, risk_text, full_text, summary)

            status = "PASS" if passed else "FAIL"
            if passed and issues:
                status = "PASS (with warnings)"
                results["warnings"] += len(issues)

            if passed:
                results["passed"] += 1
            else:
                results["failed"] += 1

            print(f"    Status: {status}")
            print(f"    Doc length: {len(full_text):,} chars")
            print(f"    Risk length: {len(risk_text):,} chars")
            print(f"    Summary: {summary[:120]}...")
            for issue in issues:
                print(f"    {issue}")

            detailed.append({
                "accession": acc,
                "company": company,
                "form": form,
                "status": status,
                "risk_len": len(risk_text),
                "doc_len": len(full_text),
                "summary_preview": summary[:100],
                "issues": issues,
            })

        except Exception as e:
            print(f"    ERROR: {str(e)[:100]}")
            results["errors"] += 1

        time.sleep(0.3)  # Be polite to SEC servers

    # ═══════════════ REPORT ═══════════════
    print("\n" + "=" * 70)
    print("  TEST RESULTS")
    print("=" * 70)
    total_tested = results["passed"] + results["failed"]
    print(f"  Passed:   {results['passed']}/{total_tested}")
    print(f"  Failed:   {results['failed']}/{total_tested}")
    print(f"  Errors:   {results['errors']} (could not retrieve filing)")
    print(f"  Warnings: {results['warnings']}")

    if total_tested > 0:
        pass_rate = results["passed"] / total_tested * 100
        print(f"  Pass rate: {pass_rate:.0f}%")
        if pass_rate >= 75:
            print(f"\n  RESULT: ACCEPTABLE ({pass_rate:.0f}% >= 75% threshold)")
        else:
            print(f"\n  RESULT: NEEDS IMPROVEMENT ({pass_rate:.0f}% < 75% threshold)")

    # Print failures for investigation
    failures = [d for d in detailed if "FAIL" in d["status"]]
    if failures:
        print(f"\n  FAILURES ({len(failures)}):")
        for f in failures:
            print(f"    - {f['company']} ({f['form']}) [{f['accession']}]")
            for issue in f["issues"]:
                print(f"      {issue}")

    print("=" * 70)


if __name__ == "__main__":
    run_tests()
