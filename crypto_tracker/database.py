"""
Database layer for Crypto SEC Filing Tracker.

NORMALIZED schema (4 tables) replaces v1.1.26's single filings table:
  - filings           : metadata only (one row per filing)
  - filing_documents  : raw cached text of each attachment (one-time fetch)
  - filing_sections   : extracted section CANDIDATES with confidence scores
  - filing_summaries  : AI/template summaries (regeneratable)

Why normalized? So we can re-extract or re-summarize WITHOUT re-scraping.
v1.1.26 was re-downloading filings every time extraction was tweaked.

View `v_filings_display` gives back the flat shape the Flask app expects.

v1.1.33.1 hotfix:
  v1.1.33 added _backfill_company_details() to init_db(), which made init_db()
  hold a write lock for hundreds of ms on every process start. Flask's debug-mode
  auto-reloader spawns a child process that ALSO runs init_db(); the child's
  conn.executescript("DROP VIEW IF EXISTS ...") collided with the parent's
  still-active backfill write, and executescript() doesn't respect busy_timeout
  → instant crash. v1.1.32 didn't hit this because init_db() finished fast
  enough that processes never overlapped.

  Fix: track schema version with PRAGMA user_version. On the second+ run
  (including the reloader child), init_db() reads the version, sees it's
  current, and returns without taking ANY write lock. DDL and backfill only
  run when SCHEMA_VERSION is bumped.
"""
import os
import sqlite3
import threading
import time
from datetime import datetime

from . import config


_local = threading.local()
_init_done = False
_init_lock = threading.Lock()

# Bump this whenever the schema or the COMPANY_DETAILS lookup changes.
# init_db() compares against PRAGMA user_version and only runs DDL/backfill
# when the on-disk version is behind.
SCHEMA_VERSION = 2


def _ensure_data_dir():
    os.makedirs(config.DATA_DIR, exist_ok=True)
    os.makedirs(config.EXPORT_DIR, exist_ok=True)
    os.makedirs(config.EDGAR_CACHE_DIR, exist_ok=True)
    text_cache = getattr(config, "TEXT_CACHE_DIR", "")
    if text_cache:
        os.makedirs(text_cache, exist_ok=True)


def get_connection():
    """Thread-local SQLite connection with WAL mode + busy_timeout.

    busy_timeout makes other threads wait up to 30s for the writer to release
    its lock, instead of immediately raising "database is locked".
    """
    if not hasattr(_local, "conn") or _local.conn is None:
        _ensure_data_dir()
        _local.conn = sqlite3.connect(
            config.DB_PATH, check_same_thread=False, timeout=30.0,
        )
        _local.conn.row_factory = sqlite3.Row
        _local.conn.execute("PRAGMA journal_mode=WAL")
        _local.conn.execute("PRAGMA foreign_keys=ON")
        _local.conn.execute("PRAGMA busy_timeout=30000")  # 30s wait on locks
        _local.conn.execute("PRAGMA synchronous=NORMAL")  # WAL-safe, faster writes
    return _local.conn


def _migrate_from_old_versions():
    """One-time: if shared DB doesn't exist, copy the latest version-local DB."""
    import shutil
    import glob
    if os.path.exists(config.DB_PATH):
        return
    base_repo = os.path.dirname(os.path.dirname(config.BASE_DIR))
    candidates = sorted(glob.glob(
        os.path.join(base_repo, "batch-1-v*/crypto_tracker/data/crypto_filings.db")
    ))
    if candidates:
        src = candidates[-1]
        _ensure_data_dir()
        shutil.copy2(src, config.DB_PATH)
        count = sqlite3.connect(src).execute("SELECT COUNT(*) FROM filings").fetchone()[0]
        print(f"  Migrated {count} filings from {os.path.basename(os.path.dirname(os.path.dirname(os.path.dirname(src))))} to shared DB")


def _run_ddl(conn):
    """Run all CREATE/ALTER/DROP statements as individual execute() calls.
    Unlike conn.executescript(), individual execute() calls respect
    PRAGMA busy_timeout, so they wait for any concurrent writer instead
    of crashing instantly with 'database is locked'."""
    conn.execute("""
        CREATE TABLE IF NOT EXISTS filings (
            accession_no      TEXT PRIMARY KEY,
            cik               TEXT NOT NULL,
            company_name      TEXT NOT NULL,
            ticker            TEXT DEFAULT '',
            form_type         TEXT NOT NULL,
            root_form         TEXT NOT NULL,
            filing_date       TEXT NOT NULL,
            filing_category   TEXT NOT NULL,
            tier              INTEGER DEFAULT 1,
            sec_url           TEXT DEFAULT '',
            filing_pdf_url    TEXT DEFAULT '',
            crypto_connection TEXT DEFAULT '',
            search_keyword    TEXT DEFAULT '',
            purpose           TEXT DEFAULT '',
            top_holdings      TEXT DEFAULT '',
            fetched_at        TEXT DEFAULT '',
            processed_at      TEXT DEFAULT ''
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_filings_date ON filings(filing_date)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_filings_company ON filings(company_name)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_filings_form ON filings(form_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_filings_category ON filings(filing_category)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS filing_documents (
            accession_no  TEXT NOT NULL,
            doc_seq       INTEGER NOT NULL,
            doc_name      TEXT DEFAULT '',
            doc_type      TEXT DEFAULT '',
            raw_text      TEXT DEFAULT '',
            char_count    INTEGER DEFAULT 0,
            crypto_score  INTEGER DEFAULT 0,
            fund_name     TEXT DEFAULT '',
            PRIMARY KEY (accession_no, doc_seq),
            FOREIGN KEY (accession_no) REFERENCES filings(accession_no) ON DELETE CASCADE
        )
    """)

    conn.execute("""
        CREATE TABLE IF NOT EXISTS filing_sections (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            accession_no  TEXT NOT NULL,
            section_type  TEXT NOT NULL,
            method        TEXT NOT NULL,
            title         TEXT DEFAULT '',
            text          TEXT NOT NULL,
            char_count    INTEGER DEFAULT 0,
            confidence    REAL DEFAULT 0,
            is_primary    INTEGER DEFAULT 0,
            extracted_at  TEXT DEFAULT '',
            FOREIGN KEY (accession_no) REFERENCES filings(accession_no) ON DELETE CASCADE
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sections_acc ON filing_sections(accession_no)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sections_primary ON filing_sections(accession_no, is_primary)")

    conn.execute("""
        CREATE TABLE IF NOT EXISTS filing_summaries (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            accession_no  TEXT NOT NULL,
            section_id    INTEGER,
            model         TEXT NOT NULL,
            summary       TEXT NOT NULL,
            is_current    INTEGER DEFAULT 1,
            created_at    TEXT DEFAULT '',
            FOREIGN KEY (accession_no) REFERENCES filings(accession_no) ON DELETE CASCADE,
            FOREIGN KEY (section_id) REFERENCES filing_sections(id) ON DELETE SET NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_summaries_acc ON filing_summaries(accession_no)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_summaries_current ON filing_summaries(accession_no, is_current)")

    for col in ("purpose", "top_holdings"):
        try:
            conn.execute(f"ALTER TABLE filings ADD COLUMN {col} TEXT DEFAULT ''")
        except Exception:
            pass

    conn.execute("DROP VIEW IF EXISTS v_filings_display")
    conn.execute("""
        CREATE VIEW v_filings_display AS
        SELECT
            f.accession_no, f.cik, f.company_name, f.ticker, f.form_type, f.root_form,
            f.filing_date, f.filing_category, f.tier, f.sec_url, f.filing_pdf_url,
            f.crypto_connection, f.search_keyword, f.processed_at,
            f.purpose, f.top_holdings,
            COALESCE(s.text, '')          AS risk_section,
            COALESCE(s.char_count, 0)     AS text_length,
            COALESCE(s.confidence, 0)     AS extraction_confidence,
            COALESCE(s.method, '')        AS extraction_method,
            COALESCE(s.section_type, '')  AS section_type,
            COALESCE(sm.summary, '')      AS risk_summary,
            COALESCE(sm.model, '')        AS summary_model
        FROM filings f
        LEFT JOIN filing_sections s
            ON s.accession_no = f.accession_no AND s.is_primary = 1
        LEFT JOIN filing_summaries sm
            ON sm.accession_no = f.accession_no AND sm.is_current = 1
    """)

    conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    conn.commit()


def init_db():
    """Initialize schema and (if needed) backfill purpose/holdings.

    Fast path: if PRAGMA user_version >= SCHEMA_VERSION on the DB file, return
    immediately. NO write locks taken. This is the path every subsequent process
    takes (including Flask debug-mode reloader children, and every Flask route
    handler that re-calls init_db).

    Slow path: only runs the first time a given DB sees this version. DDL +
    backfill happen here, with retry-on-lock if another writer is mid-flight.
    """
    global _init_done
    if _init_done:
        return get_connection()

    with _init_lock:
        if _init_done:
            return get_connection()

        _migrate_from_old_versions()
        conn = get_connection()

        current_version = conn.execute("PRAGMA user_version").fetchone()[0]
        if current_version >= SCHEMA_VERSION:
            _init_done = True
            return conn

        # Schema upgrade needed. Retry on transient locks (e.g. Flask reloader
        # parent still finishing its DDL when the child starts).
        last_err = None
        for attempt in range(5):
            try:
                _run_ddl(conn)
                _backfill_company_details(conn)
                last_err = None
                break
            except sqlite3.OperationalError as e:
                last_err = e
                if "locked" not in str(e).lower():
                    raise
                wait = 1.0 * (attempt + 1)
                print(f"  DDL attempt {attempt + 1} hit lock, waiting {wait}s...")
                time.sleep(wait)
        if last_err is not None:
            raise last_err

        _init_done = True
        return conn


def _backfill_company_details(conn):
    """For filings whose CIK is in COMPANY_DETAILS but whose purpose is blank,
    fill in purpose + top_holdings from the lookup table. Runs once at startup
    after schema migration."""
    details = getattr(config, "COMPANY_DETAILS", {})
    if not details:
        return
    try:
        updated = 0
        for cik, (purpose, holdings) in details.items():
            cur = conn.execute(
                "UPDATE filings SET purpose=?, top_holdings=? "
                "WHERE cik=? AND (purpose IS NULL OR purpose='')",
                (purpose, holdings, cik),
            )
            updated += cur.rowcount
        if updated:
            conn.commit()
            print(f"  Backfilled purpose/holdings on {updated} existing filings")
    except Exception as e:
        # Schema may not have these columns yet on very old DBs — non-fatal
        print(f"  Backfill skipped: {type(e).__name__}: {e}")


# ═══════════════════════════════════════════════════════════════════════════
# WRITE OPERATIONS
# ═══════════════════════════════════════════════════════════════════════════

def upsert_filing(data: dict):
    """Insert or update filing metadata."""
    conn = get_connection()
    data.setdefault("purpose", "")
    data.setdefault("top_holdings", "")
    conn.execute("""
        INSERT INTO filings (
            accession_no, cik, company_name, ticker, form_type, root_form,
            filing_date, filing_category, tier, sec_url, filing_pdf_url,
            crypto_connection, search_keyword, purpose, top_holdings,
            fetched_at, processed_at
        ) VALUES (
            :accession_no, :cik, :company_name, :ticker, :form_type, :root_form,
            :filing_date, :filing_category, :tier, :sec_url, :filing_pdf_url,
            :crypto_connection, :search_keyword, :purpose, :top_holdings,
            :fetched_at, :processed_at
        )
        ON CONFLICT(accession_no) DO UPDATE SET
            company_name=excluded.company_name, ticker=excluded.ticker,
            sec_url=excluded.sec_url, filing_pdf_url=excluded.filing_pdf_url,
            crypto_connection=excluded.crypto_connection,
            purpose=excluded.purpose, top_holdings=excluded.top_holdings,
            processed_at=excluded.processed_at
    """, data)


def replace_documents(accession_no: str, documents: list):
    """Replace all document rows for a filing (one-time cache)."""
    conn = get_connection()
    conn.execute("DELETE FROM filing_documents WHERE accession_no = ?", (accession_no,))
    for seq, d in enumerate(documents):
        conn.execute("""
            INSERT INTO filing_documents
                (accession_no, doc_seq, doc_name, doc_type, raw_text,
                 char_count, crypto_score, fund_name)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            accession_no, seq, d.get("name", ""), d.get("type", ""),
            d.get("text", ""), len(d.get("text", "")),
            d.get("crypto_score", 0), d.get("fund_name", ""),
        ))


def replace_sections(accession_no: str, candidates: list):
    """Replace all section candidates for a filing. First candidate = primary."""
    conn = get_connection()
    conn.execute("DELETE FROM filing_sections WHERE accession_no = ?", (accession_no,))
    now = datetime.now().isoformat()
    for i, c in enumerate(candidates):
        conn.execute("""
            INSERT INTO filing_sections
                (accession_no, section_type, method, title, text, char_count,
                 confidence, is_primary, extracted_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            accession_no, c.get("section_type", "risk_factors"),
            c.get("method", "unknown"), c.get("title", ""),
            c.get("text", ""), len(c.get("text", "")),
            float(c.get("confidence", 0)),
            1 if i == 0 else 0, now,
        ))


def get_primary_section_id(accession_no: str):
    conn = get_connection()
    row = conn.execute(
        "SELECT id FROM filing_sections WHERE accession_no = ? AND is_primary = 1",
        (accession_no,),
    ).fetchone()
    return row["id"] if row else None


def upsert_summary(accession_no: str, summary: str, model: str, section_id=None):
    """Replace the current summary. Old summaries stay but lose is_current."""
    conn = get_connection()
    conn.execute(
        "UPDATE filing_summaries SET is_current = 0 WHERE accession_no = ?",
        (accession_no,),
    )
    conn.execute("""
        INSERT INTO filing_summaries
            (accession_no, section_id, model, summary, is_current, created_at)
        VALUES (?, ?, ?, ?, 1, ?)
    """, (accession_no, section_id, model, summary, datetime.now().isoformat()))


def commit():
    get_connection().commit()


# ═══════════════════════════════════════════════════════════════════════════
# READ OPERATIONS
# ═══════════════════════════════════════════════════════════════════════════

def get_existing_accessions():
    conn = get_connection()
    rows = conn.execute("SELECT accession_no FROM filings").fetchall()
    return {r["accession_no"] for r in rows}


def get_filing_count():
    return get_connection().execute(
        "SELECT COUNT(*) AS c FROM v_filings_display WHERE risk_summary != ''"
    ).fetchone()["c"]


def get_total_filing_count():
    """Count ALL filings in DB, including those without summaries."""
    return get_connection().execute(
        "SELECT COUNT(*) AS c FROM filings"
    ).fetchone()["c"]


def get_all_filings(order_by="filing_date DESC"):
    conn = get_connection()
    rows = conn.execute(
        f"SELECT * FROM v_filings_display WHERE risk_summary != '' ORDER BY {order_by}"
    ).fetchall()
    return [dict(r) for r in rows]


def get_filing_by_accession(accession_no):
    conn = get_connection()
    row = conn.execute(
        "SELECT * FROM v_filings_display WHERE accession_no = ?", (accession_no,)
    ).fetchone()
    if not row:
        return None
    data = dict(row)
    # Attach all section candidates for the detail page
    data["sections"] = [
        dict(r) for r in conn.execute(
            "SELECT id, section_type, method, title, char_count, confidence, "
            "is_primary FROM filing_sections WHERE accession_no = ? "
            "ORDER BY is_primary DESC, confidence DESC",
            (accession_no,),
        ).fetchall()
    ]
    return data


def get_section_text(section_id):
    conn = get_connection()
    row = conn.execute(
        "SELECT text FROM filing_sections WHERE id = ?", (section_id,)
    ).fetchone()
    return row["text"] if row else ""


def set_primary_section(accession_no, section_id):
    """Promote one candidate to primary. Used for manual overrides."""
    conn = get_connection()
    conn.execute(
        "UPDATE filing_sections SET is_primary = 0 WHERE accession_no = ?",
        (accession_no,),
    )
    conn.execute(
        "UPDATE filing_sections SET is_primary = 1 WHERE id = ? AND accession_no = ?",
        (section_id, accession_no),
    )


def get_dashboard_stats():
    conn = get_connection()

    total = conn.execute(
        "SELECT COUNT(*) AS c FROM v_filings_display WHERE risk_summary != ''"
    ).fetchone()["c"]
    companies = conn.execute(
        "SELECT COUNT(DISTINCT company_name) AS c FROM v_filings_display "
        "WHERE risk_summary != ''"
    ).fetchone()["c"]
    etf_count = conn.execute(
        "SELECT COUNT(*) AS c FROM v_filings_display "
        "WHERE filing_category='ETF/Fund' AND risk_summary != ''"
    ).fetchone()["c"]
    oper_count = conn.execute(
        "SELECT COUNT(*) AS c FROM v_filings_display "
        "WHERE filing_category='Operating Co.' AND risk_summary != ''"
    ).fetchone()["c"]

    monthly = conn.execute("""
        SELECT substr(filing_date,1,7) AS month, COUNT(*) AS count
        FROM v_filings_display WHERE risk_summary != '' AND filing_date != ''
        GROUP BY month ORDER BY month
    """).fetchall()

    top_companies = conn.execute("""
        SELECT company_name, COUNT(*) AS count
        FROM v_filings_display WHERE risk_summary != ''
        GROUP BY company_name ORDER BY count DESC LIMIT 15
    """).fetchall()

    form_types = conn.execute("""
        SELECT filing_category, form_type, COUNT(*) AS count
        FROM v_filings_display WHERE risk_summary != ''
        GROUP BY filing_category, form_type ORDER BY count DESC
    """).fetchall()

    keywords = conn.execute("""
        SELECT search_keyword, COUNT(*) AS count
        FROM v_filings_display
        WHERE risk_summary != '' AND search_keyword != ''
        GROUP BY search_keyword ORDER BY count DESC LIMIT 10
    """).fetchall()

    categories = conn.execute("""
        SELECT filing_category, COUNT(*) AS count
        FROM v_filings_display WHERE risk_summary != ''
        GROUP BY filing_category ORDER BY count DESC
    """).fetchall()

    daily = conn.execute("""
        SELECT filing_date, COUNT(*) AS count
        FROM v_filings_display WHERE risk_summary != '' AND filing_date != ''
        GROUP BY filing_date ORDER BY filing_date DESC LIMIT 30
    """).fetchall()

    return {
        "total": total, "companies": companies,
        "etf_count": etf_count, "oper_count": oper_count,
        "monthly": [dict(r) for r in monthly],
        "top_companies": [dict(r) for r in top_companies],
        "form_types": [dict(r) for r in form_types],
        "keywords": [dict(r) for r in keywords],
        "categories": [dict(r) for r in categories],
        "daily": [dict(r) for r in daily],
    }


def get_filtered_filings(company=None, form_type=None, month=None,
                         category=None, keyword=None, search=None):
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
        conditions.append("substr(filing_date,1,7) = ?")
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
        f"SELECT * FROM v_filings_display WHERE {where} ORDER BY filing_date DESC",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


# ═══════════════════════════════════════════════════════════════════════════
# EXPORTS
# ═══════════════════════════════════════════════════════════════════════════

_ILLEGAL_XML_RE = None


def _sanitize_for_excel(value):
    """Strip control characters that openpyxl rejects (\\x00-\\x08, \\x0B, \\x0C,
    \\x0E-\\x1F). Also cap cell length at Excel's 32,767 char limit."""
    global _ILLEGAL_XML_RE
    if _ILLEGAL_XML_RE is None:
        import re as _re
        _ILLEGAL_XML_RE = _re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F]")
    if not isinstance(value, str):
        return value
    cleaned = _ILLEGAL_XML_RE.sub(" ", value)
    if len(cleaned) > 32767:
        cleaned = cleaned[:32764] + "..."
    return cleaned


def _filtered_or_all(filters):
    """v1.1.33: if any filter is set, export the filtered view; else export all."""
    if filters and any(filters.values()):
        return get_filtered_filings(**filters)
    return get_all_filings()


def export_to_csv(filepath=None, filters=None):
    import csv
    if filepath is None:
        _ensure_data_dir()
        filepath = os.path.join(
            config.EXPORT_DIR,
            f"crypto_filings_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
        )
    filings = _filtered_or_all(filters)
    if not filings:
        return None
    keys = filings[0].keys()
    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(filings)
    return filepath


def export_to_excel(filepath=None, filters=None):
    import pandas as pd
    if filepath is None:
        _ensure_data_dir()
        filepath = os.path.join(
            config.EXPORT_DIR,
            f"crypto_filings_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
        )
    filings = _filtered_or_all(filters)
    if not filings:
        return None
    sanitized = [
        {k: _sanitize_for_excel(v) for k, v in row.items()}
        for row in filings
    ]
    df = pd.DataFrame(sanitized)
    df.to_excel(filepath, index=False, engine="openpyxl")
    return filepath
