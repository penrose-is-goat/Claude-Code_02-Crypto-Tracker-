"""
Database layer for Crypto SEC Filing Tracker.
SQLite database with full schema for filings, risk sections, and metadata.
Supports export to Excel/CSV.
"""
import os
import sqlite3
import threading
from datetime import datetime

from . import config


_local = threading.local()


def _ensure_data_dir():
    os.makedirs(config.DATA_DIR, exist_ok=True)
    os.makedirs(config.EXPORT_DIR, exist_ok=True)


def get_connection():
    """Get a thread-local database connection."""
    if not hasattr(_local, "conn") or _local.conn is None:
        _ensure_data_dir()
        _local.conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA foreign_keys=ON")
    return _local.conn


def init_db():
    """Create tables if they don't exist."""
    conn = get_connection()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS filings (
            accession_no    TEXT PRIMARY KEY,
            cik             TEXT NOT NULL,
            company_name    TEXT NOT NULL,
            ticker          TEXT DEFAULT '',
            form_type       TEXT NOT NULL,
            root_form       TEXT NOT NULL,
            filing_date     TEXT NOT NULL,
            filing_category TEXT NOT NULL,
            tier            INTEGER DEFAULT 1,
            text_length     INTEGER DEFAULT 0,
            risk_summary    TEXT DEFAULT '',
            risk_section    TEXT DEFAULT '',
            crypto_connection TEXT DEFAULT '',
            sec_url         TEXT DEFAULT '',
            filing_pdf_url  TEXT DEFAULT '',
            search_keyword  TEXT DEFAULT '',
            n_risk_paras    INTEGER DEFAULT 0,
            processed_at    TEXT DEFAULT ''
        );

        CREATE INDEX IF NOT EXISTS idx_filings_date ON filings(filing_date);
        CREATE INDEX IF NOT EXISTS idx_filings_company ON filings(company_name);
        CREATE INDEX IF NOT EXISTS idx_filings_form ON filings(form_type);
        CREATE INDEX IF NOT EXISTS idx_filings_category ON filings(filing_category);
    """)
    conn.commit()
    return conn


def get_existing_accessions():
    """Return set of accession numbers already in the database."""
    conn = get_connection()
    rows = conn.execute("SELECT accession_no FROM filings").fetchall()
    return {r["accession_no"] for r in rows}


def insert_filing(data):
    """Insert or replace a single filing record."""
    conn = get_connection()
    conn.execute("""
        INSERT OR REPLACE INTO filings (
            accession_no, cik, company_name, ticker, form_type, root_form,
            filing_date, filing_category, tier, text_length, risk_summary,
            risk_section, crypto_connection, sec_url, filing_pdf_url,
            search_keyword, n_risk_paras, processed_at
        ) VALUES (
            :accession_no, :cik, :company_name, :ticker, :form_type, :root_form,
            :filing_date, :filing_category, :tier, :text_length, :risk_summary,
            :risk_section, :crypto_connection, :sec_url, :filing_pdf_url,
            :search_keyword, :n_risk_paras, :processed_at
        )
    """, data)


def commit():
    conn = get_connection()
    conn.commit()


def get_all_filings(order_by="filing_date DESC"):
    """Return all filings with non-empty summaries, ordered by date."""
    conn = get_connection()
    rows = conn.execute(f"""
        SELECT * FROM filings
        WHERE risk_summary != ''
        ORDER BY {order_by}
    """).fetchall()
    return [dict(r) for r in rows]


def get_filing_by_accession(accession_no):
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM filings WHERE accession_no = ?", (accession_no,)
    ).fetchone()
    return dict(row) if row else None


def get_dashboard_stats():
    """Compute dashboard statistics."""
    conn = get_connection()

    total = conn.execute(
        "SELECT COUNT(*) as c FROM filings WHERE risk_summary != ''"
    ).fetchone()["c"]

    companies = conn.execute(
        "SELECT COUNT(DISTINCT company_name) as c FROM filings WHERE risk_summary != ''"
    ).fetchone()["c"]

    etf_count = conn.execute(
        "SELECT COUNT(*) as c FROM filings WHERE filing_category = 'ETF/Fund' AND risk_summary != ''"
    ).fetchone()["c"]

    oper_count = conn.execute(
        "SELECT COUNT(*) as c FROM filings WHERE filing_category = 'Operating Co.' AND risk_summary != ''"
    ).fetchone()["c"]

    # Monthly filing counts
    monthly = conn.execute("""
        SELECT substr(filing_date, 1, 7) as month, COUNT(*) as count
        FROM filings WHERE risk_summary != '' AND filing_date != ''
        GROUP BY month ORDER BY month
    """).fetchall()

    # Top companies
    top_companies = conn.execute("""
        SELECT company_name, COUNT(*) as count
        FROM filings WHERE risk_summary != ''
        GROUP BY company_name ORDER BY count DESC LIMIT 15
    """).fetchall()

    # Form type breakdown
    form_types = conn.execute("""
        SELECT filing_category, form_type, COUNT(*) as count
        FROM filings WHERE risk_summary != ''
        GROUP BY filing_category, form_type ORDER BY count DESC
    """).fetchall()

    # Keyword breakdown
    keywords = conn.execute("""
        SELECT search_keyword, COUNT(*) as count
        FROM filings WHERE risk_summary != '' AND search_keyword != ''
        GROUP BY search_keyword ORDER BY count DESC LIMIT 10
    """).fetchall()

    # Category breakdown (for doughnut chart)
    categories = conn.execute("""
        SELECT filing_category, COUNT(*) as count
        FROM filings WHERE risk_summary != ''
        GROUP BY filing_category ORDER BY count DESC
    """).fetchall()

    # Filings per day (recent 30 days that have filings)
    daily = conn.execute("""
        SELECT filing_date, COUNT(*) as count
        FROM filings WHERE risk_summary != '' AND filing_date != ''
        GROUP BY filing_date ORDER BY filing_date DESC LIMIT 30
    """).fetchall()

    return {
        "total": total,
        "companies": companies,
        "etf_count": etf_count,
        "oper_count": oper_count,
        "monthly": [dict(r) for r in monthly],
        "top_companies": [dict(r) for r in top_companies],
        "form_types": [dict(r) for r in form_types],
        "keywords": [dict(r) for r in keywords],
        "categories": [dict(r) for r in categories],
        "daily": [dict(r) for r in daily],
    }


def get_filtered_filings(company=None, form_type=None, month=None,
                         category=None, keyword=None, search=None):
    """Return filings matching filter criteria."""
    conn = get_connection()
    conditions = ["risk_summary != ''"]
    params = []

    if company:
        conditions.append("company_name LIKE ?")
        params.append(f"%{company}%")
    if form_type:
        conditions.append("form_type = ?")
        params.append(form_type)
    if month:
        conditions.append("substr(filing_date, 1, 7) = ?")
        params.append(month)
    if category:
        conditions.append("filing_category = ?")
        params.append(category)
    if keyword:
        conditions.append("search_keyword = ?")
        params.append(keyword)
    if search:
        conditions.append(
            "(company_name LIKE ? OR risk_summary LIKE ? OR form_type LIKE ?)"
        )
        params.extend([f"%{search}%"] * 3)

    where = " AND ".join(conditions)
    rows = conn.execute(
        f"SELECT * FROM filings WHERE {where} ORDER BY filing_date DESC", params
    ).fetchall()
    return [dict(r) for r in rows]


def export_to_csv(filepath=None):
    """Export all filings to CSV."""
    import csv

    if filepath is None:
        _ensure_data_dir()
        filepath = os.path.join(
            config.EXPORT_DIR,
            f"crypto_filings_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        )

    filings = get_all_filings()
    if not filings:
        return None

    keys = filings[0].keys()
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(filings)

    return filepath


def export_to_excel(filepath=None):
    """Export all filings to Excel (.xlsx)."""
    import pandas as pd

    if filepath is None:
        _ensure_data_dir()
        filepath = os.path.join(
            config.EXPORT_DIR,
            f"crypto_filings_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
        )

    filings = get_all_filings()
    if not filings:
        return None

    df = pd.DataFrame(filings)
    df.to_excel(filepath, index=False, engine="openpyxl")
    return filepath


def get_filing_count():
    conn = get_connection()
    return conn.execute("SELECT COUNT(*) as c FROM filings").fetchone()["c"]
