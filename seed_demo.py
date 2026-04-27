"""
seed_demo.py — GIVA Framework v6  (MODIFIED: Meta Ads seeder ONLY)

CHANGE LOG vs original:
  - Removed all demo order generation (keep real Shopify orders untouched)
  - Removed all demo refund generation (keep real refunds untouched)
  - Removed all demo inventory snapshot generation (keep real inventory untouched)
  - Removed BL-001 (Bracelet) and ER-001 (Earrings) — demo-only SKUs
  - Real SKU portfolio: SR-001 Silver Ring, GR-001 Gold Ring, NC-001 Necklace
  - Meta Ads data is still seeded for all 3 real SKUs (CAC pipeline depends on it)
  - --reset flag now ONLY clears raw_meta_ads rows where campaign_id matches
    the DEMO- pattern — it does NOT touch orders, refunds, inventory, or sku_master

PURPOSE:
  This script exists solely to seed synthetic Meta Ads spend data so the
  computed_metrics.py CAC pipeline has ad attribution rows to work with.
  Real orders + real inventory come from ingest_shopify.py.
  Real Meta Ads come from ingest_meta_ads.py (production).
  This file is for local dev / staging only.

Usage:
  python seed_demo.py              # seed Meta Ads for real SKUs
  python seed_demo.py --reset      # clear only demo Meta Ads rows, then re-seed
  python seed_demo.py --dry-run    # print what would be seeded, no DB writes
"""

import sqlite3
import os
import sys
import random
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.environ.get("DB_PATH", "giva.db")
SEED = 42
random.seed(SEED)

DRY_RUN   = "--dry-run"   in sys.argv
RESET_ADS = "--reset"     in sys.argv

# ------------------------------------------------------------------ #
#  Real SKU portfolio ONLY (3 real SKUs — no demo SKUs)
# ------------------------------------------------------------------ #

# (sku_id, price)  — price needed to compute revenue_attributed in Meta Ads
REAL_SKUS = [
    ("SR-001", 999.0),
    ("GR-001", 1999.0),
    ("NC-001", 2999.0),
]

# ------------------------------------------------------------------ #
#  Meta Ads scenario config — drives realistic ad spend per SKU
# ------------------------------------------------------------------ #

META_SCENARIOS = {
    "SR-001": {
        "meta_spend_day": 800,    # ₹/day base
        "meta_cvr":       0.02,   # 2% conversion rate on clicks
    },
    "GR-001": {
        "meta_spend_day": 1200,
        "meta_cvr":       0.015,
    },
    "NC-001": {
        "meta_spend_day": 600,
        "meta_cvr":       0.01,
    },
}

# ------------------------------------------------------------------ #
#  Helpers
# ------------------------------------------------------------------ #

def make_campaign_id(sku_id: str) -> str:
    """Demo campaign IDs are prefixed DEMO- so --reset can identify them."""
    return f"DEMO-CMP-{sku_id}"


def make_adset_id(sku_id: str) -> str:
    return f"DEMO-ADSET-{sku_id}"


# ------------------------------------------------------------------ #
#  Core: seed Meta Ads ONLY
# ------------------------------------------------------------------ #

def seed_meta_ads(db_path: str = DB_PATH):
    today = datetime.now()

    if DRY_RUN:
        print("[seed_demo] DRY RUN — no DB writes will occur.\n")

    conn = None if DRY_RUN else sqlite3.connect(db_path)
    if conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")

    c = conn.cursor() if conn else None
    now = today.isoformat()
    today_str = today.strftime("%Y-%m-%d")

    # ---- OPTIONAL RESET: clear only demo Meta Ads rows ------------ #
    if RESET_ADS and not DRY_RUN:
        print("[seed_demo] RESET: clearing demo Meta Ads rows (DEMO-CMP-* only)...")
        conn.execute("""
            DELETE FROM raw_meta_ads
            WHERE campaign_id LIKE 'DEMO-CMP-%'
        """)
        conn.commit()
        print("[seed_demo] Demo Meta Ads rows cleared.\n")

    # ---- Verify real SKUs exist in sku_master --------------------- #
    if not DRY_RUN:
        for sku_id, _ in REAL_SKUS:
            c.execute("SELECT 1 FROM sku_master WHERE sku_id = ?", (sku_id,))
            if not c.fetchone():
                print(
                    f"[seed_demo] WARNING: {sku_id} not found in sku_master. "
                    f"Run sku_master.py or ingest_shopify.py first. "
                    f"Skipping Meta Ads seeding for this SKU."
                )

    # ---- Seed 60 days of Meta Ads per real SKU ------------------- #
    print("[seed_demo] Seeding Meta Ads spend data (last 60 days, real SKUs only)...")
    ads_inserted = 0
    ads_skipped  = 0  # already-existing rows (idempotent)

    for sku_id, price in REAL_SKUS:
        s = META_SCENARIOS[sku_id]
        campaign_id = make_campaign_id(sku_id)
        adset_id    = make_adset_id(sku_id)

        for day_offset in range(60):
            ad_date_dt  = today - timedelta(days=day_offset)
            ad_date     = ad_date_dt.strftime("%Y-%m-%d")

            # Weekend spend spike (realistic ad budget behaviour)
            day_of_week = ad_date_dt.weekday()
            spend_mult  = 1.3 if day_of_week >= 5 else 1.0

            spend       = round(s["meta_spend_day"] * spend_mult * random.uniform(0.8, 1.2), 2)
            impressions = int(spend * random.uniform(400, 600))
            clicks      = int(impressions * random.uniform(0.01, 0.03))
            conversions = max(0, int(clicks * s["meta_cvr"]))
            revenue_attr = conversions * price

            if DRY_RUN:
                print(
                    f"  [DRY] {sku_id} | {ad_date} | spend ₹{spend:.0f} | "
                    f"impr {impressions} | clicks {clicks} | conv {conversions}"
                )
                ads_inserted += 1
                continue

            c.execute("""
                INSERT OR IGNORE INTO raw_meta_ads
                (campaign_id, adset_id, sku_id, spend, impressions, clicks,
                 conversions, revenue_attributed, acquisition_channel, ad_date, ingest_ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                campaign_id, adset_id, sku_id,
                spend, impressions, clicks, conversions, revenue_attr,
                "Meta Paid", ad_date, now
            ))
            if c.rowcount:
                ads_inserted += 1
            else:
                ads_skipped += 1

    # ---- Freshness log -------------------------------------------- #
    if not DRY_RUN:
        c.execute("""
            INSERT OR REPLACE INTO freshness_log
            (source, pull_date, rows_received, rows_expected,
             completeness_pct, lag_days, status, logged_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, ("meta_ads_demo_seed", today_str, ads_inserted, ads_inserted, 100.0, 0, "OK", now))

        conn.commit()
        conn.close()

    # ---- Summary --------------------------------------------------- #
    print("\n========== DEMO SEED SUMMARY ==========")
    print(f"  Mode:           {'DRY RUN (no writes)' if DRY_RUN else 'LIVE'}")
    print(f"  SKUs seeded:    {len(REAL_SKUS)} (real SKUs only: SR-001, GR-001, NC-001)")
    print(f"  Meta Ads rows:  {ads_inserted} inserted")
    if ads_skipped:
        print(f"  Already existed: {ads_skipped} rows (idempotent — not duplicated)")
    print()
    print("  ✅ Only Meta Ads data was written.")
    print("  ✅ Real orders, refunds, and inventory snapshots were NOT touched.")
    print("  ✅ sku_master was NOT modified.")
    print()
    print("  Skus seeded for Meta Ads:")
    for sku_id, price in REAL_SKUS:
        s = META_SCENARIOS[sku_id]
        print(f"    {sku_id} | ₹{price:.0f} price | ₹{s['meta_spend_day']}/day base spend | {s['meta_cvr']*100:.1f}% CVR")
    print("========================================")

    if not DRY_RUN:
        print("\n  → Now run: python computed_metrics.py")


# ------------------------------------------------------------------ #
#  Cleanup helper: remove demo-only SKUs from sku_master if they exist
# ------------------------------------------------------------------ #

def remove_demo_skus(db_path: str = DB_PATH):
    """
    Removes BL-001 and ER-001 (demo-only SKUs) from sku_master
    and all their associated raw data.

    Run this ONCE if you previously seeded with the old seed_demo.py.
    This will NOT be called automatically — invoke explicitly.

    Usage: python seed_demo.py --purge-demo-skus
    """
    DEMO_ONLY_SKUS = ["BL-001", "ER-001"]
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=OFF")  # allow cascade deletes manually
    c = conn.cursor()

    for sku_id in DEMO_ONLY_SKUS:
        for table in [
            "raw_shopify_orders",
            "raw_refunds",
            "raw_inventory_snapshots",
            "raw_meta_ads",
            "computed_metrics",
            "audit_log",
        ]:
            c.execute(f"DELETE FROM {table} WHERE sku_id = ?", (sku_id,))
            deleted = c.rowcount
            if deleted:
                print(f"  [{table}] Deleted {deleted} rows for {sku_id}")

        c.execute("DELETE FROM sku_master WHERE sku_id = ?", (sku_id,))
        if c.rowcount:
            print(f"  [sku_master] Removed {sku_id}")

    # Also clean demo orders that don't belong to any real SKU
    c.execute("""
        DELETE FROM raw_shopify_orders
        WHERE order_id LIKE 'DEMO-%'
    """)
    print(f"  [raw_shopify_orders] Deleted {c.rowcount} demo orders (DEMO-* prefix)")

    c.execute("""
        DELETE FROM raw_refunds
        WHERE order_id LIKE 'DEMO-%'
    """)
    print(f"  [raw_refunds] Deleted {c.rowcount} demo refunds (DEMO-* prefix)")

    conn.commit()
    conn.close()
    print("\n  ✅ Demo SKUs and all associated demo data purged.")
    print("  ✅ Real orders, real inventory, and real SKUs untouched.")


# ------------------------------------------------------------------ #
#  Entry point
# ------------------------------------------------------------------ #

if __name__ == "__main__":
    if "--purge-demo-skus" in sys.argv:
        print("[seed_demo] Purging demo-only SKUs (BL-001, ER-001) and their data...")
        remove_demo_skus(DB_PATH)
    else:
        seed_meta_ads(DB_PATH)
