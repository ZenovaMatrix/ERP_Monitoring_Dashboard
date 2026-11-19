"""
Production start script (use this instead of `python app_local.py` when deploying).

    python run_server.py

* serves the dashboard with waitress (a real WSGI server, not Flask's development server)
* listens on HOST:PORT from .env (default 0.0.0.0:8080 = reachable from other computers on the network)
* starts the background checker, so e-mail alerts go out even with nobody looking at the dashboard
"""
import os
import sys

from db_wrapper import DatabaseWrapper      # also loads .env
import app_local


def main():
    db = DatabaseWrapper()
    try:
        db.test_connection()
        print(f"Database type: {db.db_type} - connection successful")
    finally:
        db.close()

    app_local.start_background_scanner()

    try:
        from waitress import serve
    except ImportError:
        sys.exit("waitress is not installed. Run:  pip install -r requirements.txt")

    host = os.getenv("HOST", "0.0.0.0")
    port = int(os.getenv("PORT", "8080"))
    print(f"ERP Monitoring Dashboard running on http://{host}:{port}  (Ctrl+C to stop)")
    if not os.getenv("DASH_USER") or not os.getenv("DASH_PASS"):
        print("WARNING: DASH_USER / DASH_PASS are not set - anyone who can reach this port can open the dashboard.")
    serve(app_local.app, host=host, port=port, threads=int(os.getenv("THREADS", "8")))


if __name__ == "__main__":
    main()
