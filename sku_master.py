"""
sku_master.py — GIVA Framework v6
- Price bands derived programmatically from price; never hardcoded
- Band boundaries: Entry <₹999 inclusive, Mid ₹1K-₹2.5K, Premium >₹2.5K
- Validates cogs < price before insert
"""

import sqlite3
import os

DB_PATH = os.environ.get("DB_PATH", "giva.db")


def price_band(price: float) -> str:
    """Single source of truth for price band classification."""
    if price <= 999:
        return "Entry (<₹999)"
    elif price <= 2500:
        return "Mid (₹1K-2.5K)"
    else:
        return "Premium (>₹2.5K)"


# Define SKUs: (sku_id, sku_name, cogs, price, launch_date)
# Band is derived — do NOT hardcode it here
SKUS_RAW = [
    ("SR-001", "Silver Ring",  400.0,  999.0, "2026-04-25"),
    ("GR-001", "Gold Ring",    800.0, 1999.0, "2026-04-25"),
    ("NC-001", "Necklace",    1200.0, 2999.0, "2026-04-25"),  # 2999 → Premium (>₹2.5K)
]


def load_sku_master(db_path: str = DB_PATH):
    conn = sqlite3.connect(db_path)
    c = conn.cursor()

    skus = []
    for sku_id, sku_name, cogs, price, launch_date in SKUS_RAW:
        if cogs >= price:
            raise ValueError(f"REJECTED {sku_id}: cogs ({cogs}) >= price ({price}). Check data.")
        band = price_band(price)
        skus.append((sku_id, sku_name, cogs, price, band, launch_date))

    c.executemany("""
        INSERT OR REPLACE INTO sku_master
        (sku_id, sku_name, cogs, price, price_band, launch_date)
        VALUES (?, ?, ?, ?, ?, ?)
    """, skus)

    conn.commit()

    c.execute("SELECT sku_id, sku_name, price, price_band FROM sku_master")
    print("SKU Master loaded:")
    print(f"  {'SKU':<8} {'Name':<15} {'Price':>8}  Band")
    print(f"  {'-'*8} {'-'*15} {'-'*8}  {'-'*20}")
    for row in c.fetchall():
        print(f"  {row[0]:<8} {row[1]:<15} ₹{row[2]:>7.0f}  {row[3]}")

    conn.close()


if __name__ == "__main__":
    load_sku_master()
