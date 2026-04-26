"""
validate.py — GIVA Framework v6  (Fix 3: Strict Logical Validation)

Runs after every ingestion cycle. Hard exits with non-zero code if any
P0 condition is found. P1 and P2 are logged to freshness_log.

Exit codes:
  0 = all checks pass
  1 = P1 issues found (warn; proceed with caution)
  2 = P0 issues found (HALT — do not run computed_metrics)
"""

import sqlite3
import json
import os
import sys
from datetime import datetime, timedelta

DB_PATH = os.environ.get("DB_PATH", "giva.db")


class ValidationError(Exception):
    pass


def run_validation(db_path: str = DB_PATH) -> int:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    c = conn.cursor()
    now = datetime.now().isoformat()
    today = datetime.now().strftime("%Y-%m-%d")

    p0_issues = []
    p1_issues = []
    p2_issues = []

    # ---------------------------------------------------------------- #
    # CHECK 1: revenue > 0 but quantity = 0
    # (Fix 4 from issue list — now actually reachable in the schema)
    # ---------------------------------------------------------------- #
    c.execute("""
        SELECT COUNT(*) AS cnt FROM raw_shopify_orders
        WHERE gross_revenue > 0 AND quantity = 0
    """)
    cnt = c.fetchone()["cnt"]
    if cnt:
        p0_issues.append(f"CHECK1: {cnt} rows with revenue>0 but quantity=0")

    # ---------------------------------------------------------------- #
    # CHECK 2: return value exceeds sale value for same order+sku
    # ---------------------------------------------------------------- #
    c.execute("""
        SELECT r.order_id, r.sku_id,
               r.refund_amount, o.gross_revenue
        FROM raw_refunds r
        JOIN raw_shopify_orders o
          ON r.order_id = o.order_id AND r.sku_id = o.sku_id
        WHERE r.refund_amount > o.gross_revenue * 1.01
    """)
    bad_refunds = c.fetchall()
    if bad_refunds:
        for row in bad_refunds:
            p1_issues.append(
                f"CHECK2: refund {row['refund_amount']:.2f} > sale {row['gross_revenue']:.2f} "
                f"| order={row['order_id']} sku={row['sku_id']}"
            )

    # ---------------------------------------------------------------- #
    # CHECK 3: orders referencing SKUs not in sku_master
    # ---------------------------------------------------------------- #
    c.execute("""
        SELECT DISTINCT o.sku_id FROM raw_shopify_orders o
        LEFT JOIN sku_master sm ON o.sku_id = sm.sku_id
        WHERE sm.sku_id IS NULL AND o.sku_id != 'UNKNOWN'
    """)
    missing_skus = [row[0] for row in c.fetchall()]
    if missing_skus:
        p1_issues.append(f"CHECK3: SKUs in orders but not in sku_master: {missing_skus}")

    # ---------------------------------------------------------------- #
    # CHECK 4: inventory snapshot freshness — should have today's data
    # ---------------------------------------------------------------- #
    c.execute("""
        SELECT COUNT(*) FROM raw_inventory_snapshots
        WHERE snapshot_date = ?
    """, (today,))
    inv_today = c.fetchone()[0]
    if inv_today == 0:
        p1_issues.append(f"CHECK4: No inventory snapshot for today ({today})")

    # ---------------------------------------------------------------- #
    # CHECK 5: return lag > 10 days (Fix 2 from framework)
    # CM1 is unreliable if returns are lagging
    # ---------------------------------------------------------------- #
    c.execute("""
        SELECT COUNT(*) FROM raw_refunds
        WHERE julianday('now') - julianday(refunded_at) > 10
          AND ingest_ts > date('now', '-1 day')
    """)
    # This checks if we ingested old-dated refunds today — a lag signal
    stale_refunds = c.fetchone()[0]
    if stale_refunds > 0:
        p1_issues.append(
            f"CHECK5: {stale_refunds} refunds ingested today are >10 days old — "
            f"return lag exceeds threshold. CM1 confidence = PROVISIONAL."
        )

    # ---------------------------------------------------------------- #
    # CHECK 6: duplicate order_ids (should not happen with UNIQUE, but verify)
    # ---------------------------------------------------------------- #
    c.execute("""
        SELECT order_id, sku_id, COUNT(*) as cnt
        FROM raw_shopify_orders
        GROUP BY order_id, sku_id
        HAVING cnt > 1
    """)
    dupes = c.fetchall()
    if dupes:
        p0_issues.append(
            f"CHECK6: {len(dupes)} duplicate (order_id, sku_id) pairs found — "
            f"idempotency constraint may have been bypassed"
        )

    # ---------------------------------------------------------------- #
    # CHECK 7: Meta Ads data missing (P2 — informational)
    # ---------------------------------------------------------------- #
    c.execute("SELECT COUNT(*) FROM raw_meta_ads WHERE ad_date = ?", (today,))
    ads_today = c.fetchone()[0]
    if ads_today == 0:
        p2_issues.append(f"CHECK7: No Meta Ads data for today — CAC will use floor")

    # ---------------------------------------------------------------- #
    # WRITE RESULTS to freshness_log
    # ---------------------------------------------------------------- #
    all_issues = (
        [("P0", i) for i in p0_issues] +
        [("P1", i) for i in p1_issues] +
        [("P2", i) for i in p2_issues]
    )

    status = "OK"
    if p0_issues:
        status = "BLOCKED"
    elif p1_issues:
        status = "PARTIAL"

    conn.execute("""
        INSERT OR REPLACE INTO freshness_log
        (source, pull_date, rows_received, rows_expected,
         completeness_pct, lag_days, status, logged_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, ("validate", today, len(all_issues), 0, None, 0, status, now))

    conn.commit()
    conn.close()

    # ---------------------------------------------------------------- #
    # REPORT
    # ---------------------------------------------------------------- #
    print(f"\n========== VALIDATION REPORT ({today}) ==========")
    if not all_issues:
        print("  ✓ All checks passed. Status: OK")
    for severity, msg in all_issues:
        tag = "🔴 P0 HALT" if severity == "P0" else ("🟡 P1 WARN" if severity == "P1" else "⚪ P2 INFO")
        print(f"  {tag}: {msg}")
    print(f"=================================================\n")

    if p0_issues:
        print("HALTING: P0 issues found. Do not run computed_metrics until resolved.")
        return 2
    if p1_issues:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(run_validation())
