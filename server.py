"""
server.py — GIVA Framework v6  Dashboard API Server

Serves:
  GET /             → dashboard HTML
  GET /api/metrics  → JSON payload for dashboard rendering

Run locally:   python server.py
Render deploy: set Start Command = python server.py
"""

import sqlite3
import os
import json
from datetime import datetime
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse

DB_PATH   = os.environ.get("DB_PATH", "giva.db")
PORT      = int(os.environ.get("PORT", 8080))
DASHBOARD = os.path.join(os.path.dirname(__file__), "dashboard.html")


# ── DB helpers ────────────────────────────────────────────────────────────────

def get_metrics():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row

    # Latest week for each SKU
    rows = conn.execute("""
        SELECT
            cm.sku_id,
            sm.sku_name,
            cm.week_start,
            cm.adjusted_velocity,
            cm.promo_stripped_velocity,
            cm.return_rate,
            cm.realized_cm1,
            cm.spot_gmroi,
            cm.rolling_4wk_gmroi,
            cm.rolling_8wk_gmroi,
            cm.zombie_tier,
            cm.fds_score,
            cm.decision_blocked,
            cm.alert_severity,
            cm.alert_reason,
            cm.cac_confidence,
            cm.cm1_confidence,
            cm.freshness_status,
            cm.computed_at,
            -- CAC: approximate from spend/orders
            (
                SELECT ROUND(SUM(spend) / NULLIF(COUNT(DISTINCT o.order_id), 0), 0)
                FROM raw_meta_ads ma
                LEFT JOIN raw_shopify_orders o
                    ON o.sku_id = cm.sku_id
                    AND date(o.created_at) >= cm.week_start
                WHERE ma.sku_id = cm.sku_id
                AND ma.ad_date >= cm.week_start
            ) AS cac
        FROM computed_metrics cm
        JOIN sku_master sm ON cm.sku_id = sm.sku_id
        WHERE cm.week_start = (
            SELECT MAX(week_start) FROM computed_metrics WHERE sku_id = cm.sku_id
        )
        ORDER BY cm.decision_blocked DESC, cm.sku_id
    """).fetchall()

    freshness = conn.execute("""
        SELECT source, pull_date, rows_received, status
        FROM freshness_log
        WHERE (source, pull_date) IN (
            SELECT source, MAX(pull_date) FROM freshness_log GROUP BY source
        )
        ORDER BY source
    """).fetchall()

    audit = conn.execute("""
        SELECT sku_id, decision_type, actor, decision_ts,
               confidence_score, assumptions_applied, override_reason, layer
        FROM audit_log
        ORDER BY decision_ts DESC
        LIMIT 10
    """).fetchall()

    # Run info
    latest = conn.execute("""
        SELECT week_start, computed_at FROM computed_metrics
        ORDER BY computed_at DESC LIMIT 1
    """).fetchone()

    conn.close()

    return {
        "metrics":  [dict(r) for r in rows],
        "freshness": [dict(r) for r in freshness],
        "audit_log": [dict(r) for r in audit],
        "run_info": {
            "week_start":  latest["week_start"]  if latest else "—",
            "computed_at": latest["computed_at"][:16].replace("T", " ") if latest else "—",
        }
    }


# ── HTTP Handler ───────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        ts = datetime.now().strftime("%H:%M:%S")
        print(f"[{ts}] {self.address_string()} — {fmt % args}")

    def send_json(self, data, status=200):
        body = json.dumps(data, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", len(body))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def send_html(self, path):
        try:
            with open(path, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(body))
            self.end_headers()
            self.wfile.write(body)
        except FileNotFoundError:
            self.send_json({"error": "dashboard.html not found"}, 404)

    def do_GET(self):
        path = urlparse(self.path).path

        if path in ("/", "/index.html"):
            self.send_html(DASHBOARD)

        elif path == "/api/metrics":
            try:
                data = get_metrics()
                self.send_json(data)
            except Exception as e:
                self.send_json({"error": str(e)}, 500)

        elif path == "/api/health":
            self.send_json({"status": "ok", "db": DB_PATH,
                            "ts": datetime.now().isoformat()})

        else:
            self.send_json({"error": "Not found"}, 404)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
        self.end_headers()


# ── Entry point ────────────────────────────────────────────────────────────────

def main():
    print(f"\n{'='*50}")
    print(f"  GIVA Intelligence Dashboard")
    print(f"  DB:   {DB_PATH}")
    print(f"  Port: {PORT}")
    print(f"  URL:  http://localhost:{PORT}")
    print(f"{'='*50}\n")

    server = HTTPServer(("0.0.0.0", PORT), Handler)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[server] Stopped.")


if __name__ == "__main__":
    main()
