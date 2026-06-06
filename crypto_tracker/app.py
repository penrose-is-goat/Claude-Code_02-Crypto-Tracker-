"""
Flask web application for Crypto SEC Filing Tracker v1.1.35.

v1.1.35: exact risk text endpoints, provenance display, route/export tests.
v1.1.32: Bypass edgartools.search_filings PoolTimeout; live search progress.
v1.1.31: UI modernization + entity detail fields (purpose, holdings).
"""
import json
import os
import threading
from datetime import datetime
from urllib.parse import urlencode

from flask import (
    Flask, render_template, jsonify, request, send_file, redirect, url_for
)

from . import config
from . import database as db
from .extractor import format_risk_for_html
from .scraper import (
    clear_filings_data,
    clear_runtime_caches,
    run_scraper,
    reprocess_existing,
)

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
    "skipped": 0,
    "mode": config.DEFAULT_SCRAPE_MODE,
    "scope": config.DEFAULT_SCRAPE_SCOPE,
    "metrics": {},
    "message": "Idle",
    "last_run": None,
}
_scraper_lock = threading.Lock()


def _scraper_progress(current, total, saved, failed, message=None):
    with _scraper_lock:
        _scraper_status["progress"] = current
        _scraper_status["total"] = total
        _scraper_status["saved"] = saved
        _scraper_status["failed"] = failed
        if message:
            _scraper_status["message"] = message
        else:
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
    limit = max(1, min(request.args.get("limit", default=50, type=int) or 50, 200))
    offset = max(0, request.args.get("offset", default=0, type=int) or 0)

    page_rows = db.get_filtered_filings(
        company=company or None, form_type=form_type or None,
        month=month or None, category=category or None,
        keyword=keyword or None, search=search or None,
        limit=limit + 1, offset=offset,
    )
    has_next = len(page_rows) > limit
    filings = page_rows[:limit]
    for f in filings:
        risk = f.get("risk_section", "")
        f["risk_preview"] = (risk[:700] + "...") if len(risk) > 700 else risk

    active_filters = {
        k: v for k, v in {
            "company": company, "form_type": form_type, "month": month,
            "category": category, "keyword": keyword, "search": search,
        }.items() if v
    }
    page_args = request.args.to_dict(flat=True)
    page_args["limit"] = str(limit)
    prev_url = None
    if offset > 0:
        prev_args = dict(page_args)
        prev_args["offset"] = str(max(0, offset - limit))
        prev_url = url_for("filings_page") + "?" + urlencode(prev_args)
    next_url = None
    if has_next:
        next_args = dict(page_args)
        next_args["offset"] = str(offset + limit)
        next_url = url_for("filings_page") + "?" + urlencode(next_args)

    return render_template(
        "filings.html",
        filings=filings,
        active_filters=active_filters,
        page_limit=limit,
        page_offset=offset,
        has_next=has_next,
        prev_url=prev_url,
        next_url=next_url,
        form_desc=config.FORM_DESCRIPTIONS,
        now=datetime.now(),
    )


@app.route("/filing/<accession_no>")
def filing_detail(accession_no):
    db.init_db()
    filing = db.get_filing_by_accession(accession_no)
    if not filing:
        return "Filing not found", 404
    filing["risk_char_count"] = len(filing.get("risk_section") or "")
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
        limit=request.args.get("limit", type=int),
        offset=request.args.get("offset", type=int),
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


@app.route("/api/filing/<accession_no>/exact-risk.txt")
def api_exact_risk_text(accession_no):
    """Download the primary exact risk section as plain text."""
    db.init_db()
    filing = db.get_filing_by_accession(accession_no)
    if not filing:
        return "Filing not found", 404
    text = filing.get("risk_section") or ""
    if not text:
        return "Exact risk section not available", 404
    filename = f"{accession_no}_exact_risk_section.txt"
    return (
        text,
        200,
        {
            "Content-Type": "text/plain; charset=utf-8",
            "Content-Disposition": f"attachment; filename={filename}",
        },
    )


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
    mode = request.args.get("mode", config.DEFAULT_SCRAPE_MODE)
    scope = request.args.get("scope", config.DEFAULT_SCRAPE_SCOPE)
    benchmark = request.args.get("benchmark", "0") == "1"
    if mode not in config.SCRAPE_MODES:
        return jsonify({"error": f"Invalid mode: {mode}"}), 400
    if scope not in config.SCRAPE_SCOPES:
        return jsonify({"error": f"Invalid scope: {scope}"}), 400
    with _scraper_lock:
        if _scraper_status["running"]:
            return jsonify({"error": "Scraper already running"}), 409
        _scraper_status["running"] = True
        _scraper_status["message"] = f"Starting {mode} update ({scope})..."
        _scraper_status["progress"] = 0
        _scraper_status["total"] = 0
        _scraper_status["saved"] = 0
        _scraper_status["failed"] = 0
        _scraper_status["skipped"] = 0
        _scraper_status["mode"] = mode
        _scraper_status["scope"] = scope
        _scraper_status["metrics"] = {}

    def _run():
        try:
            result = run_scraper(
                progress_callback=_scraper_progress,
                mode=mode,
                scope=scope,
                benchmark=benchmark,
            )
            with _scraper_lock:
                metrics = result.get("metrics", {})
                counters = metrics.get("counters", {})
                _scraper_status["metrics"] = metrics
                reprocessed = result.get("reprocessed", 0)
                if reprocessed > 0:
                    _scraper_status["message"] = (
                        f"Done: reprocessed {reprocessed} cached filings, "
                        f"{result['total_in_db']} exact sections stored "
                        f"({result['duration_seconds']/60:.1f}min)"
                    )
                elif result["new_found"] == 0 and result["total_in_db"] > 0:
                    fixed = "fixed point, " if counters.get("fixed_point_complete") else ""
                    _scraper_status["message"] = (
                        f"Up to date: {fixed}{result['total_in_db']} exact sections "
                        f"already stored ({result['duration_seconds']/60:.1f}min)"
                    )
                elif result["new_found"] == 0 and result["total_in_db"] == 0:
                    _scraper_status["message"] = (
                        "No filings found. Check your internet connection and try again."
                    )
                else:
                    _scraper_status["skipped"] = result.get("skipped", 0)
                    deferred = counters.get("analysis_deferred", 0)
                    _scraper_status["message"] = (
                        f"Done: {result['saved']} exact risk sections saved, "
                        f"{deferred} analyses deferred, {result['failed']} failed, "
                        f"{result.get('skipped', 0)} skipped, "
                        f"{result['total_in_db']} total exact sections "
                        f"({result['duration_seconds']/60:.1f}min)"
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
    clear_filings_data()
    if request.args.get("clear_caches", "0") == "1":
        clear_runtime_caches()
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
    mode = request.args.get("mode", "all_cached")
    if mode not in {"analysis_only", "low_confidence_only", "all_cached"}:
        return jsonify({"error": f"Invalid reprocess mode: {mode}"}), 400
    limit = request.args.get("limit", type=int)

    def _run():
        try:
            if mode == "analysis_only":
                from .scraper import analyze_pending_sections
                n = analyze_pending_sections(limit=limit)
            else:
                n = reprocess_existing(
                    limit=limit,
                    only_low_confidence=only_low or mode == "low_confidence_only",
                    all_cached=mode == "all_cached",
                    mode="exact_only",
                )
            with _scraper_lock:
                _scraper_status["message"] = f"Reprocessed {n} cached filings"
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
    """Export filings to CSV or XLSX. v1.1.33: respects the same filter args
    as /filings so the download matches what the user is currently viewing.
    """
    db.init_db()
    filters = dict(
        company=request.args.get("company") or None,
        form_type=request.args.get("form_type") or None,
        month=request.args.get("month") or None,
        category=request.args.get("category") or None,
        keyword=request.args.get("keyword") or None,
        search=request.args.get("search") or None,
    )
    if fmt == "csv":
        filepath = db.export_to_csv(filters=filters)
        if not filepath:
            return jsonify({"error": "No data to export"}), 404
        return send_file(filepath, as_attachment=True)
    elif fmt == "xlsx":
        try:
            filepath = db.export_to_excel(filters=filters)
            if not filepath:
                return jsonify({"error": "No data to export"}), 404
            return send_file(filepath, as_attachment=True)
        except ImportError:
            return jsonify({"error": "openpyxl not installed"}), 500
    return jsonify({"error": "Format must be 'csv' or 'xlsx'"}), 400


def create_app():
    db.init_db()
    return app
