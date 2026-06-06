"""
Flask web application for Crypto SEC Filing Tracker v1.1.31.

v1.1.31: UI modernization + entity detail fields (purpose, holdings).
v1.1.30: Performance rewrite — shared DB, BS4 text cache, lxml parser.
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
from .scraper import run_scraper, reprocess_existing

app = Flask(
    __name__,
    template_folder=os.path.join(os.path.dirname(__file__), "templates"),
    static_folder=os.path.join(os.path.dirname(__file__), "static"),
)
app.secret_key = config.SECRET_KEY


@app.context_processor
def _inject_globals():
    """Make config and version available to every template."""
    return {"config": config}

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
    db.init_db()
    stats = db.get_dashboard_stats()
    return render_template(
        "dashboard.html",
        stats=stats,
        form_desc=config.FORM_DESCRIPTIONS,
        scraper_status=_scraper_status,
        now=datetime.now(),
    )


@app.route("/filings")
def filings_page():
    db.init_db()
    company = request.args.get("company", "").strip()
    form_type = request.args.get("form_type", "").strip()
    month = request.args.get("month", "").strip()
    category = request.args.get("category", "").strip()
    keyword = request.args.get("keyword", "").strip()
    search = request.args.get("search", "").strip()

    filings = db.get_filtered_filings(
        company=company or None, form_type=form_type or None,
        month=month or None, category=category or None,
        keyword=keyword or None, search=search or None,
    )
    for f in filings:
        f["risk_html"] = format_risk_for_html(f.get("risk_section", ""))

    active_filters = {
        k: v for k, v in {
            "company": company, "form_type": form_type, "month": month,
            "category": category, "keyword": keyword, "search": search,
        }.items() if v
    }

    return render_template(
        "filings.html",
        filings=filings,
        active_filters=active_filters,
        form_desc=config.FORM_DESCRIPTIONS,
        now=datetime.now(),
    )


@app.route("/filing/<accession_no>")
def filing_detail(accession_no):
    db.init_db()
    filing = db.get_filing_by_accession(accession_no)
    if not filing:
        return "Filing not found", 404
    filing["risk_html"] = format_risk_for_html(filing.get("risk_section", ""))
    return render_template(
        "filing_detail.html",
        filing=filing,
        form_desc=config.FORM_DESCRIPTIONS,
        now=datetime.now(),
    )


@app.route("/settings")
def settings_page():
    db.init_db()
    return render_template(
        "settings.html",
        config=config,
        scraper_status=_scraper_status,
        db_count=db.get_filing_count(),
        now=datetime.now(),
    )


# ═══════════════════════════════════════════════════════════════════════════
# API ROUTES
# ═══════════════════════════════════════════════════════════════════════════

@app.route("/api/stats")
def api_stats():
    db.init_db()
    return jsonify(db.get_dashboard_stats())


@app.route("/api/filings")
def api_filings():
    db.init_db()
    filings = db.get_filtered_filings(
        company=request.args.get("company"),
        form_type=request.args.get("form_type"),
        month=request.args.get("month"),
        category=request.args.get("category"),
        keyword=request.args.get("keyword"),
        search=request.args.get("search"),
    )
    for f in filings:
        f.pop("risk_section", None)
    return jsonify(filings)


@app.route("/api/filing/<accession_no>")
def api_filing_detail(accession_no):
    db.init_db()
    filing = db.get_filing_by_accession(accession_no)
    if not filing:
        return jsonify({"error": "Not found"}), 404
    return jsonify(filing)


@app.route("/api/filing/<accession_no>/section/<int:section_id>")
def api_section_text(accession_no, section_id):
    """Return the full text of a specific extraction candidate."""
    db.init_db()
    text = db.get_section_text(section_id)
    return jsonify({"text": text, "html": format_risk_for_html(text)})


@app.route("/api/filing/<accession_no>/set-primary/<int:section_id>", methods=["POST"])
def api_set_primary(accession_no, section_id):
    """Manually promote a candidate to primary. Useful when the auto-picked
    one is wrong and a human wants to override."""
    db.init_db()
    db.set_primary_section(accession_no, section_id)
    db.commit()
    return jsonify({"status": "ok"})


@app.route("/api/scrape", methods=["POST"])
def api_start_scraper():
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
                reprocessed = result.get("reprocessed", 0)
                if reprocessed > 0:
                    _scraper_status["message"] = (
                        f"Done: reprocessed {reprocessed} cached filings, "
                        f"{result['total_in_db']} with summaries "
                        f"({result['duration_seconds']/60:.1f}min)"
                    )
                elif result["new_found"] == 0 and result["total_in_db"] == 0:
                    _scraper_status["message"] = (
                        "No filings found. Check your internet connection and try again."
                    )
                else:
                    _scraper_status["message"] = (
                        f"Done: {result['saved']} new, {result['failed']} failed, "
                        f"{result['total_in_db']} total ({result['duration_seconds']/60:.1f}min)"
                    )
                _scraper_status["last_run"] = datetime.now().isoformat()
        except Exception as e:
            with _scraper_lock:
                _scraper_status["message"] = f"Error: {str(e)[:200]}"
        finally:
            with _scraper_lock:
                _scraper_status["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/reset", methods=["POST"])
def api_reset_db():
    """Wipe all filing data. Useful when extraction logic changes and you
    want a clean re-scrape from scratch."""
    db.init_db()
    conn = db.get_connection()
    conn.executescript("""
        DELETE FROM filing_summaries;
        DELETE FROM filing_sections;
        DELETE FROM filing_documents;
        DELETE FROM filings;
    """)
    conn.commit()
    return jsonify({"status": "ok", "message": "Database wiped. Press Update Filings to re-scrape."})


@app.route("/api/reprocess", methods=["POST"])
def api_reprocess():
    """Re-extract and re-summarize all cached filings. No re-scraping.
    Use this when extraction logic is improved in a later version.
    """
    with _scraper_lock:
        if _scraper_status["running"]:
            return jsonify({"error": "Scraper already running"}), 409
        _scraper_status["running"] = True
        _scraper_status["message"] = "Reprocessing cached filings..."

    only_low = request.args.get("only_low_confidence", "0") == "1"
    limit = request.args.get("limit", type=int)

    def _run():
        try:
            n = reprocess_existing(limit=limit, only_low_confidence=only_low)
            with _scraper_lock:
                _scraper_status["message"] = f"Reprocessed {n} filings"
                _scraper_status["last_run"] = datetime.now().isoformat()
        except Exception as e:
            with _scraper_lock:
                _scraper_status["message"] = f"Error: {str(e)[:200]}"
        finally:
            with _scraper_lock:
                _scraper_status["running"] = False

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "started"})


@app.route("/api/scraper-status")
def api_scraper_status():
    with _scraper_lock:
        return jsonify(dict(_scraper_status))


@app.route("/api/export/<fmt>")
def api_export(fmt):
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
            return jsonify({"error": "openpyxl not installed"}), 500
    return jsonify({"error": "Format must be 'csv' or 'xlsx'"}), 400


def create_app():
    db.init_db()
    return app
