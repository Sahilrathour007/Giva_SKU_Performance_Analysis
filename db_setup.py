"""
db_setup.py — GIVA Framework v6 FINAL
"""

import sqlite3
import os

DB_PATH = os.environ.get("DB_PATH", "giva.db")


def setup(db_path: str = DB_PATH):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    c = conn.cursor()

    c.execute("""
    CREATE TABLE IF NOT EXISTS raw_shopify_orders (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id         TEXT    NOT NULL,
        sku_id           TEXT    NOT NULL,
        sku_name         TEXT    NOT NULL,
        quantity         INTEGER NOT NULL CHECK(quantity > 0),
        gross_revenue    REAL    NOT NULL CHECK(gross_revenue >= 0),
        discount_amount  REAL    DEFAULT 0,
        financial_status TEXT,
        created_at       TEXT    NOT NULL,
        ingest_ts        TEXT    NOT NULL,
        UNIQUE(order_id, sku_id)
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS raw_inventory_snapshots (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        sku_id        TEXT NOT NULL,
        stock_level   INTEGER NOT NULL CHECK(stock_level >= 0),
        snapshot_date TEXT NOT NULL,
        ingest_ts     TEXT NOT NULL,
        UNIQUE(sku_id, snapshot_date)
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS sku_master (
        sku_id       TEXT PRIMARY KEY,
        sku_name     TEXT NOT NULL,
        cogs         REAL NOT NULL CHECK(cogs > 0),
        price        REAL NOT NULL CHECK(price > cogs),
        price_band   TEXT NOT NULL CHECK(price_band IN ('Entry (<₹999)', 'Mid (₹1K-2.5K)', 'Premium (>₹2.5K)')),
        launch_date  TEXT NOT NULL
    )
    """)

    # Refunds: dedup on (order_id, sku_id, refunded_at)
    # Same order + same SKU + same timestamp = same refund event
    # A genuine second refund will have a different refunded_at
    c.execute("""
    CREATE TABLE IF NOT EXISTS raw_refunds (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        order_id         TEXT NOT NULL,
        sku_id           TEXT NOT NULL,
        refund_quantity  INTEGER NOT NULL CHECK(refund_quantity > 0),
        refund_amount    REAL    NOT NULL CHECK(refund_amount >= 0),
        refund_reason    TEXT,
        refunded_at      TEXT NOT NULL,
        ingest_ts        TEXT NOT NULL,
        UNIQUE(order_id, sku_id, refunded_at)
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS computed_metrics (
        id                      INTEGER PRIMARY KEY AUTOINCREMENT,
        sku_id                  TEXT    NOT NULL,
        week_start              TEXT    NOT NULL,
        raw_velocity            REAL,
        days_in_stock           INTEGER,
        adjusted_velocity       REAL,
        promo_stripped_velocity REAL,
        return_rate             REAL,
        reverse_logistics       REAL,
        liquidation_loss        REAL,
        realized_cm1            REAL,
        spot_gmroi              REAL,
        rolling_4wk_gmroi       REAL,
        rolling_8wk_gmroi       REAL,
        zombie_tier             INTEGER DEFAULT 0,
        fds_score               REAL,
        zombie_breach_ts        TEXT,
        test_segment            TEXT,
        scale_segment           TEXT,
        acquisition_channel     TEXT,
        customer_intent         TEXT,
        transfer_confidence     TEXT CHECK(transfer_confidence IN ('HIGH', 'LOW', NULL)),
        dtc_applied             REAL,
        cac_confidence          TEXT CHECK(cac_confidence IN ('HIGH', 'MEDIUM', 'LOW', NULL)),
        cm1_confidence          TEXT CHECK(cm1_confidence IN ('COMPLETE', 'PROVISIONAL', 'BLOCKED', NULL)),
        decision_blocked        INTEGER DEFAULT 0,
        alert_severity          TEXT CHECK(alert_severity IN ('P0', 'P1', 'P2', NULL)),
        alert_reason            TEXT,
        data_version            TEXT,
        freshness_status        TEXT CHECK(freshness_status IN ('FRESH', 'STALE', 'PARTIAL', NULL)),
        computed_at             TEXT NOT NULL,
        UNIQUE(sku_id, week_start)
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS audit_log (
        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
        sku_id              TEXT NOT NULL,
        decision_type       TEXT NOT NULL,
        actor               TEXT NOT NULL,
        decision_ts         TEXT NOT NULL,
        data_version        TEXT,
        confidence_score    REAL,
        assumptions_applied TEXT,
        override_reason     TEXT,
        layer               INTEGER
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS freshness_log (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        source           TEXT NOT NULL,
        pull_date        TEXT NOT NULL,
        rows_received    INTEGER,
        rows_expected    INTEGER,
        completeness_pct REAL,
        lag_days         INTEGER,
        status           TEXT NOT NULL CHECK(status IN ('OK', 'PARTIAL', 'BLOCKED', 'FAILED')),
        logged_at        TEXT NOT NULL,
        UNIQUE(source, pull_date)
    )
    """)

    c.execute("""
    CREATE TABLE IF NOT EXISTS raw_meta_ads (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        campaign_id        TEXT NOT NULL,
        adset_id           TEXT,
        sku_id             TEXT,
        spend              REAL,
        impressions        INTEGER,
        clicks             INTEGER,
        conversions        INTEGER,
        revenue_attributed REAL,
        acquisition_channel TEXT DEFAULT 'Meta Paid',
        ad_date            TEXT NOT NULL,
        ingest_ts          TEXT NOT NULL,
        UNIQUE(campaign_id, ad_date)
    )
    """)

    conn.commit()
    conn.close()
    print(f"[db_setup] Schema ready at: {db_path}")
    print(f"[db_setup] 8 tables: orders, inventory, sku_master, refunds,")
    print(f"           computed_metrics, audit_log, freshness_log, meta_ads")


if __name__ == "__main__":
    setup()