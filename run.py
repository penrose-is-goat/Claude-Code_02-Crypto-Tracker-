#!/usr/bin/env python3
"""
Entry point for Crypto SEC Filing Tracker.

Usage:
    python run.py              # Start the web server
    python run.py --scrape     # Run the scraper only (no web server)
    python run.py --port 8000  # Start on a custom port
"""
import argparse
import sys
import os

# Ensure the project root is on the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def main():
    parser = argparse.ArgumentParser(description="Crypto SEC Filing Tracker")
    parser.add_argument(
        "--scrape", action="store_true",
        help="Run the EDGAR scraper only (no web server)"
    )
    parser.add_argument(
        "--host", default=None,
        help="Host to bind the web server to (default: 127.0.0.1)"
    )
    parser.add_argument(
        "--port", type=int, default=None,
        help="Port for the web server (default: 5000)"
    )
    parser.add_argument(
        "--no-debug", action="store_true",
        help="Disable Flask debug mode"
    )
    args = parser.parse_args()

    if args.scrape:
        # Run scraper only
        print("=" * 70)
        print("  CRYPTO SEC FILING TRACKER — Scraper Mode")
        print("=" * 70)

        from crypto_tracker.scraper import run_scraper
        from crypto_tracker.database import init_db
        init_db()

        result = run_scraper()
        print(f"\n  RESULTS:")
        print(f"    New found:    {result['new_found']}")
        print(f"    Saved:        {result['saved']}")
        print(f"    Failed:       {result['failed']}")
        print(f"    Total in DB:  {result['total_in_db']}")
        print("=" * 70)
    else:
        # Start web server
        from crypto_tracker.app import create_app
        from crypto_tracker import config

        app = create_app()

        host = args.host or config.FLASK_HOST
        port = args.port or config.FLASK_PORT
        debug = not args.no_debug and config.FLASK_DEBUG

        print("=" * 70)
        print("  CRYPTO SEC FILING TRACKER — Web Server")
        print(f"  Running at: http://{host}:{port}")
        print(f"  Dashboard:  http://{host}:{port}/")
        print(f"  Filings:    http://{host}:{port}/filings")
        print(f"  Debug mode: {'ON' if debug else 'OFF'}")
        print()
        print("  Press Ctrl+C to stop")
        print("=" * 70)

        app.run(host=host, port=port, debug=debug)


if __name__ == "__main__":
    main()
