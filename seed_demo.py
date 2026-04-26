"""
seed_demo.py — GIVA Framework v6  Public Demo Seeder

Creates realistic synthetic data for 5 SKUs across 60 days.
Safe to push to GitHub — no real credentials, no real orders.
Run ONCE before deployment. Idempotent (clears and re-seeds).

SKU portfolio designed to show all framework decision states:
  SR-001  Silver Ring     ₹999    Entry band  — COMPUTED (healthy)
  GR-001  Gold Ring      ₹1,999   Mid band    — COMPUTED (clean economics)
  NC-001  Necklace       ₹2,999   Premium     — BLOCKED (100% return rate)
  BL-001  Bracelet       ₹1,499   Mid band    — COMPUTED (zombie risk flagged)
  ER-001  Earrings        ₹799    Entry band  — BLOCKED (25% return rate)

Usage:
  python seed_demo.py
  python seed_demo.py --reset   # drops and recreates all data
"""

import sqlite3
import os
import sys
import random
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

DB_PATH = os.environ.get("DB_PATH", "giva.db")
SEED = 42  # reproducible random data
random.seed(SEED)

# ------------------------------------------------------------------ #
#  SKU definitions — mirrors sku_master.py logic
# ------------------------------------------------------------------ #

SKUS = [
    # (sku_id, sku_name, cogs, price, launch_date)
    ("SR-001", "Silver Ring",   400.0,   999.0, "2026-02-25"),
    ("GR-001", "Gold Ring",     800.0,  1999.0, "2026-02-25"),
    ("NC-001", "Necklace",     1200.0,  2999.0, "2026-02-25"),
    ("BL-001", "Bracelet",      600.0,  1499.0, "2026-03-01"),
    ("ER-001", "Earrings",      300.0,   799.0, "2026-03-10"),
]


def price_band(price: float) -> str:
    if price <= 999:
        return "Entry (<₹999)"
    elif price <= 2500:
        return "Mid (₹1K-2.5K)"
    return "Premium (>₹2.5K)"


# ------------------------------------------------------------------ #
#  Scenario config — drives how realistic each SKU behaves
# ------------------------------------------------------------------ #

SCENARIOS = {
    "SR-001": {
        "weekly_orders":   7,        # healthy velocity
        "return_rate":    0.10,       # 10% — passes CM1 gate
        "discount_pct":   0.05,       # light promo
        "stockout_days":  5,          # occasional stockout
        "meta_spend_day": 800,        # active paid channel
        "meta_cvr":       0.02,       # 2% CVR
    },
    "GR-001": {
        "weekly_orders":   4,
        "return_rate":    0.0,         # zero returns — premium signal
        "discount_pct":   0.0,
        "stockout_days":  3,
        "meta_spend_day": 1200,
        "meta_cvr":       0.015,
    },
    "NC-001": {
        "weekly_orders":   3,
        "return_rate":    1.0,         # 100% — BLOCKED by CM1 gate
        "discount_pct":   0.10,
        "stockout_days":  0,
        "meta_spend_day": 600,
        "meta_cvr":       0.01,
    },
    "BL-001": {
        "weekly_orders":   2,          # low velocity — zombie risk
        "return_rate":    0.12,
        "discount_pct":   0.08,
        "stockout_days":  10,
        "meta_spend_day": 400,
        "meta_cvr":       0.008,
    },
    "ER-001": {
        "weekly_orders":   5,
        "return_rate":    0.25,        # 25% — BLOCKED, above 20% threshold
        "discount_pct":   0.15,
        "stockout_days":  2,
        "meta_spend_day": 900,
        "meta_cvr":       0.025,
    },
}

# ------------------------------------------------------------------ #
#  Helpers
# ------------------------------------------------------------------ #

def rand_date_in_range(start: datetime, end: datetime) -> datetime:
    delta = (end - start).days
    return start + timedelta(days=random.randint(0, delta))


def make_order_id(i: int) -> str:
    return f"DEMO-{i:05d}"


def make_campaign_id(sku_id: str) -> str:
    return f"CMP-{sku_id}-DEMO"


def make_adset_id(sku_id: str) -> str:
    return f"ADSET-{sku_id}-DEMO"


# ------------------------------------------------------------------ #
#  Core seeder
# ------------------------------------------------------------------ #

def seed(db_path: str = DB_PATH, reset: bool = False):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    c = conn.cursor()
    now = datetime.now().isoformat()
    today = datetime.now()
    sixty_days_ago = today - timedelta(days=60)

    if reset:
        print("[seed_demo] RESET: clearing all demo data...")
        for tbl in ["raw_shopify_orders", "raw_refunds", "raw_inventory_snapshots",
                     "raw_meta_ads", "sku_master", "computed_metrics",
                     "audit_log", "freshness_log"]:
            c.execute(f"DELETE FROM {tbl}")
        conn.commit()
        print("[seed_demo] Tables cleared.\n")

    # ---- 1. SKU MASTER -------------------------------------------- #
    print("[seed_demo] Loading SKU master...")
    for sku_id, sku_name, cogs, price, launch_date in SKUS:
        band = price_band(price)
        c.execute("""
            INSERT OR REPLACE INTO sku_master
            (sku_id, sku_name, cogs, price, price_band, launch_date)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (sku_id, sku_name, cogs, price, band, launch_date))
    conn.commit()
    print(f"  {len(SKUS)} SKUs loaded.")

    # ---- 2. ORDERS + REFUNDS -------------------------------------- #
    print("[seed_demo] Generating 60 days of orders and refunds...")
    order_counter = 1
    orders_inserted = 0
    refunds_inserted = 0

    for sku_id, sku_name, cogs, price, launch_str in SKUS:
        s = SCENARIOS[sku_id]
        launch_dt = datetime.strptime(launch_str, "%Y-%m-%d")
        start_dt  = max(launch_dt, sixty_days_ago)
        total_days = (today - start_dt).days

        # Generate orders week-by-week — guarantees minimum weekly volume
        # so return rates hit scenario targets in computed_metrics
        total_weeks = max(1, total_days // 7)
        for week_offset in range(total_weeks):
            week_start_dt = start_dt + timedelta(weeks=week_offset)
            is_stockout = (s["stockout_days"] > 0 and
                           week_offset < s["stockout_days"] // 7)
            if is_stockout:
                continue

            weekly_count = max(s["weekly_orders"],
                               round(random.gauss(s["weekly_orders"], 1.0)))
            for i in range(weekly_count):
                day_jitter = random.randint(0, 6)
                order_date = week_start_dt + timedelta(days=day_jitter)
                if order_date > today:
                    continue

                order_id  = make_order_id(order_counter)
                order_counter += 1
                discount  = round(price * s["discount_pct"], 2)
                gross_rev = price

                c.execute("""
                    INSERT OR IGNORE INTO raw_shopify_orders
                    (order_id, sku_id, sku_name, quantity, gross_revenue,
                     discount_amount, financial_status, created_at, ingest_ts)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (order_id, sku_id, sku_name, 1, gross_rev,
                      discount, "paid",
                      order_date.strftime("%Y-%m-%dT10:00:00+05:30"), now))
                orders_inserted += c.rowcount

                if random.random() < s["return_rate"]:
                    refund_date = order_date + timedelta(days=random.randint(3, 10))
                    if refund_date <= today:
                        c.execute("""
                            INSERT OR IGNORE INTO raw_refunds
                            (order_id, sku_id, refund_quantity, refund_amount,
                             refund_reason, refunded_at, ingest_ts)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        """, (order_id, sku_id, 1, gross_rev * 0.9,
                              "customer_return",
                              refund_date.strftime("%Y-%m-%dT15:00:00+05:30"), now))
                        refunds_inserted += c.rowcount

    conn.commit()
    print(f"  Orders inserted: {orders_inserted}")
    print(f"  Refunds inserted: {refunds_inserted}")

    # ---- 3. INVENTORY SNAPSHOTS ----------------------------------- #
    print("[seed_demo] Generating inventory snapshots (last 8 weeks)...")
    snap_inserted = 0
    for sku_id, _, _, _, _ in SKUS:
        s = SCENARIOS[sku_id]
        for week in range(8):
            snap_date = (today - timedelta(weeks=week)).strftime("%Y-%m-%d")
            # Simulate restocks — stock varies by week
            base_stock = random.randint(50, 200)
            if week < s["stockout_days"] // 7:
                stock = 0
            else:
                stock = base_stock

            c.execute("""
                INSERT OR IGNORE INTO raw_inventory_snapshots
                (sku_id, stock_level, snapshot_date, ingest_ts)
                VALUES (?, ?, ?, ?)
            """, (sku_id, stock, snap_date, now))
            snap_inserted += c.rowcount

    conn.commit()
    print(f"  Snapshots inserted: {snap_inserted}")

    # ---- 4. META ADS ---------------------------------------------- #
    print("[seed_demo] Generating Meta Ads spend data (last 60 days)...")
    ads_inserted = 0
    for sku_id, _, _, price, _ in SKUS:
        s = SCENARIOS[sku_id]
        for day_offset in range(60):
            ad_date = (today - timedelta(days=day_offset)).strftime("%Y-%m-%d")
            # Spend variance: weekends spike, weekdays flat
            day_of_week = (today - timedelta(days=day_offset)).weekday()
            spend_mult = 1.3 if day_of_week >= 5 else 1.0
            spend       = round(s["meta_spend_day"] * spend_mult * random.uniform(0.8, 1.2), 2)
            impressions = int(spend * random.uniform(400, 600))
            clicks      = int(impressions * random.uniform(0.01, 0.03))
            conversions = max(0, int(clicks * s["meta_cvr"]))
            revenue_attr = conversions * price

            campaign_id = make_campaign_id(sku_id)
            adset_id    = make_adset_id(sku_id)

            c.execute("""
                INSERT OR IGNORE INTO raw_meta_ads
                (campaign_id, adset_id, sku_id, spend, impressions, clicks,
                 conversions, revenue_attributed, acquisition_channel, ad_date, ingest_ts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (campaign_id, adset_id, sku_id, spend, impressions, clicks,
                  conversions, revenue_attr, "Meta Paid", ad_date, now))
            ads_inserted += c.rowcount

    conn.commit()
    print(f"  Meta Ads rows inserted: {ads_inserted}")

    # ---- 5. FRESHNESS LOG ----------------------------------------- #
    today_str = today.strftime("%Y-%m-%d")
    for source, rows in [
        ("shopify_orders",   orders_inserted),
        ("shopify_inventory", snap_inserted),
        ("meta_ads",         ads_inserted),
    ]:
        c.execute("""
            INSERT OR REPLACE INTO freshness_log
            (source, pull_date, rows_received, rows_expected,
             completeness_pct, lag_days, status, logged_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """, (source, today_str, rows, rows, 100.0, 0, "OK", now))

    conn.commit()
    conn.close()

    # ---- SUMMARY -------------------------------------------------- #
    print("\n========== DEMO SEED SUMMARY ==========")
    print(f"  SKUs:           {len(SKUS)}")
    print(f"  Orders:         {orders_inserted}")
    print(f"  Refunds:        {refunds_inserted}")
    print(f"  Inv snapshots:  {snap_inserted}")
    print(f"  Meta Ads rows:  {ads_inserted}")
    print("  Expected decisions after computed_metrics.py:")
    print("    SR-001 Silver Ring  → ✅ COMPUTED")
    print("    GR-001 Gold Ring    → ✅ COMPUTED")
    print("    NC-001 Necklace     → 🔴 BLOCKED  (100% return rate)")
    print("    BL-001 Bracelet     → ✅ COMPUTED  (zombie risk flagged)")
    print("    ER-001 Earrings     → 🔴 BLOCKED  (25% return rate)")
    print("========================================")
    print("\n✅ Demo data ready. Now run: python computed_metrics.py")


if __name__ == "__main__":
    reset_flag = "--reset" in sys.argv
    seed(DB_PATH, reset=reset_flag)