"""
Flask web application for Crypto SEC Filing Tracker.

Two pages:
  1. Dashboard (/) — Interactive charts, clickable metrics, mini filing table
  2. Filings (/filings) — Full filing list with filters, summaries, risk dropdowns

Plus API endpoints for data, export, and scraper control.
"""
import json
import os
import threading
from datetime import datetime

from flask import (
    Flask, render_template, jsonify, request, send_file, redirect, url_for
)

from . import config
from . import database as db
from .extractor import format_risk_for_html
from .scraper import run_scraper

app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(__file__), "templates"),
    static_folder=os.path.join(os.path.dirname(__file__), "static"),
)
app.secret_key = config.SECRET_KEY

# Track scraper status
_scraper_status = {
    "running": False,
    "progress": 0,
    "total": 0,
    "saved": 0,
    "failed": 0,
    "message": "Idle",
    "last_run": None,
}
_scraper_lock = threading.Lock()


def _scraper_progress(current, total, saved, failed):
    with _scraper_lock:
        _scraper_status["progress"] = current
        _scraper_status["total"] = total
        _scraper_status["saved"] = saved
        _scraper_status["failed"] = failed
        _scraper_status["message"] = f"Processing {current}/{total} (saved: {saved})"


# ═══════════════════════════════════════════════════════════════════════════
# PAGE ROUTES
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/")
def dashboard():
    """Dashboard page with charts and clickable metrics."""
    db.init_db()
    stats = db.get_dashboard_stats()
    form_desc = config.FORM_DESCRIPTIONS
    return render_template(
        "dashboard.html",
        stats=stats,
        form_desc=form_desc,
        scraper_status=_scraper_status,
        now=datetime.now(),
    )


@app.route("/filings")
def filings_page():
    """All filings page with filters, summaries, and risk section dropdowns."""
    db.init_db()

    # Get filter params from query string
    company = request.args.get("company", "").strip()
    form_type = request.args.get("form_type", "").strip()
    month = request.args.get("month", "").strip()
    category = request.args.get("category", "").strip()
    keyword = request.args.get("keyword", "").strip()
    search = request.args.get("search", "").strip()

    filings = db.get_filtered_filings(
        company=company or None,
        form_type=form_type or None,
        month=month or None,
        category=category or None,
        keyword=keyword or None,
        search=search or None,
    )

    # Pre-format risk sections for HTML display
    for f in filings:
        f["risk_html"] = format_risk_for_html(f.get("risk_section", ""))

    active_filters = {
        k: v for k, v in {
            "company": company, "form_type": form_type, "month": month,
            "category": category, "keyword": keyword, "search": search,
        }.items() if v
    }

    form_desc = config.FORM_DESCRIPTIONS
    return render_template(
        "filings.html",
        filings=filings,
        active_filters=active_filters,
        form_desc=form_desc,
        now=datetime.now(),
    )


@app.route("/filing/<accession_no>")
def filing_detail(accession_no):
    """Single filing detail view."""
    db.init_db()
    filing = db.get_filing_by_accession(accession_no)
    if not filing:
        return "Filing not found", 404
    filing["risk_html"] = format_risk_for_html(filing.get("risk_section", ""))
    return render_template("filing_detail.html", filing=filing, form_desc=config.FORM_DESCRIPTIONS)


# ═══════════════════════════════════════════════════════════════════════════
# API ROUTES
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/stats")
def api_stats():
    """Return dashboard statistics as JSON."""
    db.init_db()
    stats = db.get_dashboard_stats()
    return jsonify(stats)


@app.route("/api/filings")
def api_filings():
    """Return filings as JSON, with optional filters."""
    db.init_db()
    filings = db.get_filtered_filings(
        company=request.args.get("company"),
        form_type=request.args.get("form_type"),
        month=request.args.get("month"),
        category=request.args.get("category"),
        keyword=request.args.get("keyword"),
        search=request.args.get("search"),
    )
    # Don't send full risk_section in list endpoint (too large)
    for f in filings:
        f.pop("risk_section", None)
    return jsonify(filings)


@app.route("/api/filing/<accession_no>")
def api_filing_detail(accession_no):
    """Return single filing as JSON (including risk section)."""
    db.init_db()
    filing = db.get_filing_by_accession(accession_no)
    if not filing:
        return jsonify({"error": "Not found"}), 404
    return jsonify(filing)


@app.route("/api/scrape", methods=["POST"])
def api_start_scraper():
    """Start the scraper in a background thread."""
    with _scraper_lock:
        if _scraper_status["running"]:
            return jsonify({"error": "Scraper already running"}), 409
        _scraper_status["running"] = True
        _scraper_status["message"] = "Starting..."
        _scraper_status["progress"] = 0

    def _run():
        try:
            result = run_scraper(progress_callback=_scraper_progress)
            with _scraper_lock:
                _scraper_status["message"] = (
                    f"Done: {result['saved']} new, "
                    f"{result['failed']} failed, "
                    f"{result['total_in_db']} total"
                )
                _scraper_status["last_run"] = datetime.now().isoformat()
        except Exception as e:
            with _scraper_lock:
                _scraper_status["message"] = f"Error: {str(e)[:200]}"
        finally:
            with _scraper_lock:
                _scraper_status["running"] = False

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return jsonify({"status": "started"})


@app.route("/api/scraper-status")
def api_scraper_status():
    """Return current scraper status."""
    with _scraper_lock:
        return jsonify(dict(_scraper_status))


@app.route("/api/export/<fmt>")
def api_export(fmt):
    """Export filings to CSV or Excel."""
    db.init_db()
    if fmt == "csv":
        filepath = db.export_to_csv()
        if not filepath:
            return jsonify({"error": "No data to export"}), 404
        return send_file(filepath, as_attachment=True)
    elif fmt == "xlsx":
        try:
            filepath = db.export_to_excel()
            if not filepath:
                return jsonify({"error": "No data to export"}), 404
            return send_file(filepath, as_attachment=True)
        except ImportError:
            return jsonify({"error": "openpyxl not installed. Run: pip install openpyxl"}), 500
    else:
        return jsonify({"error": "Format must be 'csv' or 'xlsx'"}), 400


def create_app():
    """Application factory."""
    db.init_db()
    return app
