"""
ingest_meta_ads.py — GIVA Framework v6
Meta Marketing API ingestion — spend, impressions, conversions per day.
UTM params map ad spend back to SKU for CAC segmentation (Fix 4).

SETUP REQUIRED:
  1. Create a Meta Marketing API app at developers.facebook.com
  2. Add META_AD_ACCOUNT_ID and META_ACCESS_TOKEN to .env
  3. Install: pip install requests python-dotenv
"""

import sqlite3
import os
import json
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

AD_ACCOUNT_ID = os.environ.get("META_AD_ACCOUNT_ID", "")
ACCESS_TOKEN  = os.environ.get("META_ACCESS_TOKEN", "")
DB_PATH       = os.environ.get("DB_PATH", "giva.db")

META_API_VERSION = "v19.0"
BASE_URL = f"https://graph.facebook.com/{META_API_VERSION}"


def fetch_insights(date_start: str, date_stop: str) -> list[dict]:
    """
    Pull spend + conversions from Meta Ads API.
    Requires requests library; falls back to urllib if not available.
    Returns list of ad-level insight dicts.
    """
    try:
        import requests
    except ImportError:
        raise ImportError("pip install requests to use Meta Ads ingestion")

    if not AD_ACCOUNT_ID or not ACCESS_TOKEN:
        raise EnvironmentError(
            "META_AD_ACCOUNT_ID and META_ACCESS_TOKEN must be set in .env"
        )

    params = {
        "fields": (
            "campaign_id,adset_id,ad_id,ad_name,"
            "spend,impressions,clicks,"
            "actions,action_values,"
            "date_start,date_stop"
        ),
        "time_range": json.dumps({"since": date_start, "until": date_stop}),
        "level": "ad",
        "limit": 500,
        "access_token": ACCESS_TOKEN,
    }

    results = []
    url = f"{BASE_URL}/act_{AD_ACCOUNT_ID}/insights"
    while url:
        r = requests.get(url, params=params)
        r.raise_for_status()
        data = r.json()
        results.extend(data.get("data", []))
        # pagination
        url = data.get("paging", {}).get("next")
        params = {}   # next URL has all params baked in

    return results


def extract_conversions(actions: list, action_type: str = "purchase") -> int:
    """Pull purchase count from Meta actions array."""
    for a in (actions or []):
        if a.get("action_type") == action_type:
            return int(float(a.get("value", 0)))
    return 0


def extract_revenue(action_values: list, action_type: str = "purchase") -> float:
    for a in (action_values or []):
        if a.get("action_type") == action_type:
            return float(a.get("value", 0))
    return 0.0


def sku_from_ad_name(ad_name: str) -> str:
    """
    Best-effort SKU extraction from ad name conventions.
    GIVA convention expected: 'GIVA_SR-001_Retargeting_...'
    Update this mapping for your actual naming convention.
    """
    name_upper = ad_name.upper()
    for sku in ["SR-001", "GR-001", "NC-001"]:
        if sku in name_upper:
            return sku
    return "UNKNOWN"


def ingest_meta(days_back: int = 1):
    today = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")

    print(f"\n[Meta Ads] Fetching {start} → {today}")
    insights = fetch_insights(start, today)
    print(f"  Fetched {len(insights)} ad-level insight rows")

    conn = sqlite3.connect(DB_PATH)
    now = datetime.now().isoformat()
    inserted = 0

    for row in insights:
        campaign_id  = row.get("campaign_id", "")
        adset_id     = row.get("adset_id", "")
        ad_name      = row.get("ad_name", "")
        spend        = float(row.get("spend", 0))
        impressions  = int(row.get("impressions", 0))
        clicks       = int(row.get("clicks", 0))
        conversions  = extract_conversions(row.get("actions", []))
        revenue_attr = extract_revenue(row.get("action_values", []))
        ad_date      = row.get("date_start", start)
        sku_id       = sku_from_ad_name(ad_name)

        conn.execute("""
            INSERT OR REPLACE INTO raw_meta_ads
            (campaign_id, adset_id, sku_id, spend, impressions, clicks,
             conversions, revenue_attributed, acquisition_channel, ad_date, ingest_ts)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (campaign_id, adset_id, sku_id, spend, impressions, clicks,
              conversions, revenue_attr, "Meta Paid", ad_date, now))
        inserted += conn.execute("SELECT changes()").fetchone()[0]

    # log freshness
    conn.execute("""
        INSERT OR REPLACE INTO freshness_log
        (source, pull_date, rows_received, rows_expected,
         completeness_pct, lag_days, status, logged_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    """, ("meta_ads", today, inserted, len(insights), None, 0,
          "OK" if inserted > 0 else "FAILED", now))

    conn.commit()
    conn.close()
    print(f"  Meta Ads rows inserted: {inserted}")


if __name__ == "__main__":
    ingest_meta(days_back=1)
