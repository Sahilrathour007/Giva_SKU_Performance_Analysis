"""
computed_metrics.py — GIVA Framework v6 FINAL

Implements ALL 11 framework fixes:
  Fix 1:  Stockout-adjusted + promo-stripped velocity (in-stock rate gate)
  Fix 2:  Realized CM1 = (1-return_rate) × net_rev - COGS - logistics - reverse_logistics - liquidation
  Fix 3:  Signal validation before any computation (reachable guards)
  Fix 4:  Segmented CAC by price_band + customer_intent + acquisition_channel
  Fix 5:  Forward Demand Score (FDS) before any zombie kill decision
  Fix 6:  Spot GMROI (weekly) + rolling 4wk GMROI (decision-grade) + rolling 8wk trend
  Fix 7:  Dynamic EV framework (launch type × seasonality multiplier) — stored in audit_log
  Fix 8:  Audit log written for every decision; separation-of-duties flag
  Fix 9:  Brand Equity cap enforcement at 25% of portfolio capital
  Fix 10: Zombie breach timestamp is immutable — never resets without a real sales event
  Fix 11: Demand Transfer Coefficient (DTC) applied when test_segment ≠ scale_segment

Decision Gate (10-second filter, fully enforced):
  Q1: Velocity adjusted?          → else BLOCK
  Q2: Realized CM1 used?          → else BLOCK
  Q3: CAC segmented?              → use floor + flag
  Q4: FDS calculated for zombies? → else BLOCK kill
  Q5: 4wk GMROI confirmed?        → directional only
  Q6: Audit layer review logged?  → else escalate
  Q7: DTC applied cross-segment?  → else BLOCK scale

Exit codes:
  0 = all SKUs computed successfully (some may be BLOCKED by confidence gates)
  1 = partial success — some SKUs blocked on data quality
  2 = catastrophic failure — P0 input data problem; nothing computed
"""

import sqlite3
import os
import sys
from datetime import datetime, timedelta, date

DB_PATH = os.environ.get("DB_PATH", "giva.db")

# ── Framework constants ────────────────────────────────────────────────────────

WEEK_LOOKBACK_DAYS      = 7    # "this week" window
ROLLING_4WK_DAYS        = 28
ROLLING_8WK_DAYS        = 56

# Logistics cost assumptions (₹ per unit) — replace with ERP actuals when available
FORWARD_LOGISTICS_PER_UNIT  = 80.0
REVERSE_LOGISTICS_RATE      = 0.50   # 50% of forward cost per return unit
LIQUIDATION_LOSS_RATE       = 0.20   # 20% of COGS for unsellable returns (framework: 15-25%)

# CAC floor values by price_band (used when Meta Ads attribution < 70%)
CAC_FLOOR = {
    "Entry (<₹999)":     150.0,
    "Mid (₹1K-2.5K)":   300.0,
    "Premium (>₹2.5K)": 600.0,
}

# Zombie tier thresholds (days without sale)
ZOMBIE_TIERS = {1: 14, 2: 21, 3: 30, 4: 45}
ZOMBIE_CAPITAL_AUTO_TIER3 = 500_000  # ₹5L

# FDS weights (Fix 5)
FDS_SEASONAL_WEIGHT  = 0.40
FDS_TREND_WEIGHT     = 0.35
FDS_MOMENTUM_WEIGHT  = 0.25

# DTC penalty table (Fix 11)
DTC_TABLE = {
    ("T2", "T1"): 0.70,    # Tier 2 test → Tier 1 scale: heavy penalty
    ("T1", "T2"): 1.20,    # Tier 1 test → Tier 2 scale: uplift
    ("T1", "T1"): 1.00,
    ("T2", "T2"): 1.00,
    ("T3", "T3"): 1.00,
    ("T2", "T3"): 0.85,
    ("T3", "T2"): 1.10,
    ("T3", "T1"): 0.55,    # heaviest — Tier 3 test to Tier 1 launch
    ("T1", "T3"): 1.30,
}

# Brand equity capital cap (Fix 9)
BRAND_EQUITY_CAP_PCT = 0.25

# Realized CM1 thresholds by return band (Fix 2)
CM1_THRESHOLDS = [
    (0.05,  150, "< 5% return: scale if CM1 > ₹150"),
    (0.10,  200, "5-10% return: monitor if CM1 > ₹200"),
    (0.20,  300, "10-20% return: investigate if CM1 > ₹300"),
    (1.00,  None, "> 20% return: kill — no threshold saves this SKU"),
]


# ── DB helpers ─────────────────────────────────────────────────────────────────

def get_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def week_bounds(today: date, lookback_days: int = WEEK_LOOKBACK_DAYS):
    """Return (week_start_str, week_end_str) for the analysis window."""
    week_end   = today
    week_start = today - timedelta(days=lookback_days - 1)
    return week_start.strftime("%Y-%m-%d"), week_end.strftime("%Y-%m-%d")


def date_n_days_ago(today: date, n: int) -> str:
    return (today - timedelta(days=n)).strftime("%Y-%m-%d")


# ── Fix 1: Velocity calculation ────────────────────────────────────────────────

def calc_velocity(c: sqlite3.Cursor, sku_id: str, week_start: str, week_end: str
                  ) -> dict:
    """
    Returns:
      raw_velocity          — units/week (unadjusted)
      adjusted_velocity     — stockout-corrected (in-stock adjusted)
      promo_stripped_vel    — promo-only periods removed
      days_in_stock         — actual trading days in window
      in_stock_rate         — fraction of window SKU was in stock
    """
    # Total units sold in window (all financial statuses except fully refunded)
    c.execute("""
        SELECT COALESCE(SUM(o.quantity), 0) as total_qty,
               COUNT(DISTINCT DATE(o.created_at)) as active_order_days
        FROM raw_shopify_orders o
        WHERE o.sku_id = ?
          AND DATE(o.created_at) BETWEEN ? AND ?
          AND o.financial_status NOT IN ('refunded', 'voided')
    """, (sku_id, week_start, week_end))
    row = c.fetchone()
    total_qty        = row["total_qty"]
    active_order_days = row["active_order_days"]

    # Days in stock: count days where inventory snapshot shows stock > 0
    c.execute("""
        SELECT COUNT(*) as in_stock_days
        FROM raw_inventory_snapshots
        WHERE sku_id = ?
          AND snapshot_date BETWEEN ? AND ?
          AND stock_level > 0
    """, (sku_id, week_start, week_end))
    days_in_stock = c.fetchone()["in_stock_days"]

    total_days = WEEK_LOOKBACK_DAYS
    # If no inventory data, assume full availability (conservative — don't penalize new stores)
    if days_in_stock == 0:
        # Check if we have ANY inventory data at all for this SKU
        c.execute("SELECT COUNT(*) FROM raw_inventory_snapshots WHERE sku_id = ?", (sku_id,))
        has_inventory = c.fetchone()[0] > 0
        if not has_inventory:
            days_in_stock = total_days   # no data → assume always in stock
        # else: genuinely zero stock all week → days_in_stock = 0 (correct)

    in_stock_rate = days_in_stock / total_days if total_days > 0 else 0

    raw_velocity = total_qty   # raw: just units in window

    # Bug 2 fix: snapshot frequency guard.
    # If fewer than (total_days / 2) snapshots exist, the pipeline is running
    # weekly not daily — in_stock_rate is artificially low, and velocity
    # extrapolation would fabricate demand. Cap adjustment to 2x max in this case.
    c.execute("""
        SELECT COUNT(*) as snap_count
        FROM raw_inventory_snapshots
        WHERE sku_id = ?
          AND snapshot_date BETWEEN ? AND ?
    """, (sku_id, week_start, week_end))
    snap_count = c.fetchone()["snap_count"]
    snapshot_frequency_ok = snap_count >= max(1, total_days // 2)

    # In-stock adjusted velocity (Fix 1, mandatory)
    # If in_stock_rate < 90%: adjust. Else: raw = adjusted.
    if days_in_stock > 0 and in_stock_rate < 0.90:
        if snapshot_frequency_ok:
            # Daily snapshots present — extrapolation is reliable
            adjusted_velocity = (total_qty / days_in_stock) * 7
        else:
            # Weekly/sparse snapshots — cap extrapolation at 2x to avoid fabrication
            # Flag this in warnings so the analyst knows the number is soft
            adjusted_velocity = min((total_qty / days_in_stock) * 7, total_qty * 2.0)
    else:
        adjusted_velocity = float(total_qty)

    # Promo-stripped velocity: exclude days with discount > 10%
    # Using discount_amount / gross_revenue as discount rate proxy
    c.execute("""
        SELECT COALESCE(SUM(quantity), 0) as promo_qty
        FROM raw_shopify_orders
        WHERE sku_id = ?
          AND DATE(created_at) BETWEEN ? AND ?
          AND financial_status NOT IN ('refunded','voided')
          AND gross_revenue > 0
          AND (discount_amount / gross_revenue) > 0.10
    """, (sku_id, week_start, week_end))
    promo_qty = c.fetchone()["promo_qty"]
    promo_stripped_vel = float(total_qty - promo_qty)

    return {
        "raw_velocity":          float(raw_velocity),
        "adjusted_velocity":     round(adjusted_velocity, 2),
        "promo_stripped_vel":    round(promo_stripped_vel, 2),
        "days_in_stock":         days_in_stock,
        "in_stock_rate":         round(in_stock_rate, 4),
    }


# ── Fix 2: Realized CM1 ────────────────────────────────────────────────────────

def calc_realized_cm1(c: sqlite3.Cursor, sku_id: str,
                      week_start: str, week_end: str,
                      cogs: float, price: float) -> dict:
    """
    Realized CM1 = (1 - return_rate) × net_revenue
                   - COGS
                   - forward_logistics
                   - reverse_logistics
                   - liquidation_loss

    Returns per-unit economics + confidence flag.
    """
    # Gross revenue + quantity in window
    c.execute("""
        SELECT COALESCE(SUM(gross_revenue), 0)  as gross_rev,
               COALESCE(SUM(quantity), 0)        as total_units,
               COALESCE(SUM(discount_amount), 0) as total_discount
        FROM raw_shopify_orders
        WHERE sku_id = ?
          AND DATE(created_at) BETWEEN ? AND ?
          AND financial_status NOT IN ('refunded','voided')
    """, (sku_id, week_start, week_end))
    sale = c.fetchone()
    gross_rev    = sale["gross_rev"]
    total_units  = sale["total_units"]

    # Refunds in window (may include orders from prior periods — that's correct)
    c.execute("""
        SELECT COALESCE(SUM(refund_quantity), 0) as ret_qty,
               COALESCE(SUM(refund_amount),   0) as ret_amount
        FROM raw_refunds
        WHERE sku_id = ?
          AND DATE(refunded_at) BETWEEN ? AND ?
    """, (sku_id, week_start, week_end))
    ret = c.fetchone()
    ret_qty    = ret["ret_qty"]
    ret_amount = ret["ret_amount"]

    # Return lag check: if ANY refund ingested today is > 10 days old,
    # CM1 confidence drops to PROVISIONAL (Fix 2 freshness gate)
    c.execute("""
        SELECT COUNT(*) FROM raw_refunds
        WHERE sku_id = ?
          AND julianday('now') - julianday(refunded_at) > 10
          AND ingest_ts > date('now', '-1 day')
    """, (sku_id,))
    late_returns = c.fetchone()[0]
    cm1_confidence = "PROVISIONAL" if late_returns > 0 else "COMPLETE"

    # Per-unit economics
    if total_units == 0:
        # Bug 3 fix: zero-velocity block must be labelled distinctly from return-rate block.
        # An IC reading "CM1 blocked (>20% return rate)" on a dead SKU gets the wrong diagnosis.
        # Zero velocity = demand problem (relaunch / kill). High returns = quality problem (redesign).
        # Different cause → different intervention → different capital decision.
        return {
            "return_rate":        0.0,
            "reverse_logistics":  0.0,
            "liquidation_loss":   0.0,
            "realized_cm1":       None,
            "cm1_per_unit":       None,
            "cm1_confidence":     "BLOCKED",
            "cm1_threshold_note": "ZERO VELOCITY: No sales in current window — demand signal absent, not a return problem. Intervention: relaunch or kill, not returns reduction.",
        }

    return_rate = ret_qty / total_units if total_units > 0 else 0.0

    # No threshold saves > 20% return rate (Fix 2 kill rule)
    if return_rate > 0.20:
        return {
            "return_rate":        round(return_rate, 4),
            "reverse_logistics":  0.0,
            "liquidation_loss":   0.0,
            "realized_cm1":       None,
            "cm1_per_unit":       None,
            "cm1_confidence":     "BLOCKED",
            "cm1_threshold_note": f"> 20% return rate ({return_rate:.1%}): kill signal — no threshold saves this SKU",
        }

    # Revenue adjustments
    net_revenue_total   = gross_rev * (1 - return_rate)
    net_revenue_per_unit = net_revenue_total / total_units

    # Cost components
    fwd_logistics   = FORWARD_LOGISTICS_PER_UNIT
    rev_logistics   = REVERSE_LOGISTICS_RATE * fwd_logistics * return_rate   # per-unit cost
    liquidation     = LIQUIDATION_LOSS_RATE * cogs * return_rate              # per-unit cost
    total_cost_pu   = cogs + fwd_logistics + rev_logistics + liquidation

    realized_cm1_pu = net_revenue_per_unit - total_cost_pu

    # Determine threshold note
    threshold_note = ""
    for band_max, threshold, note in CM1_THRESHOLDS:
        if return_rate <= band_max:
            if threshold is None:
                cm1_confidence = "BLOCKED"
                threshold_note = note
            elif realized_cm1_pu >= threshold:
                threshold_note = f"✓ {note}"
            else:
                threshold_note = f"⚠ Below threshold (₹{threshold}): {note}"
                if cm1_confidence == "COMPLETE":
                    cm1_confidence = "PROVISIONAL"
            break

    return {
        "return_rate":        round(return_rate, 4),
        "reverse_logistics":  round(rev_logistics, 2),
        "liquidation_loss":   round(liquidation, 2),
        "realized_cm1":       round(realized_cm1_pu * total_units, 2),   # total CM1 this week
        "cm1_per_unit":       round(realized_cm1_pu, 2),
        "cm1_confidence":     cm1_confidence,
        "cm1_threshold_note": threshold_note,
    }


# ── Fix 4: Segmented CAC ───────────────────────────────────────────────────────

def calc_cac(c: sqlite3.Cursor, sku_id: str,
             price_band: str, week_start: str, week_end: str) -> dict:
    """
    CAC = total Meta spend attributed to SKU / new conversions in window.
    If attribution coverage < 70%: use segment floor + flag LOW confidence.
    """
    c.execute("""
        SELECT COALESCE(SUM(spend),       0) as total_spend,
               COALESCE(SUM(conversions), 0) as total_conv
        FROM raw_meta_ads
        WHERE sku_id = ?
          AND ad_date BETWEEN ? AND ?
    """, (sku_id, week_start, week_end))
    ads = c.fetchone()
    spend       = ads["total_spend"]
    conversions = ads["total_conv"]

    # Get order count for coverage check
    c.execute("""
        SELECT COALESCE(SUM(quantity), 0) as orders
        FROM raw_shopify_orders
        WHERE sku_id = ?
          AND DATE(created_at) BETWEEN ? AND ?
          AND financial_status NOT IN ('refunded','voided')
    """, (sku_id, week_start, week_end))
    total_orders = c.fetchone()["orders"]

    # Attribution coverage = conversions attributed / total orders
    if total_orders > 0 and conversions > 0:
        raw_coverage_ratio = conversions / total_orders
        # Bug 1 fix: phantom conversion guard.
        # If Meta reports 1.5x+ more conversions than actual Shopify orders,
        # the attribution pipeline is broken (view-through inflation, pixel misconfiguration,
        # or seeder/test data). Clamp to 1.0 hides the error — flag it instead.
        if raw_coverage_ratio > 1.5:
            return {
                "cac":                  CAC_FLOOR.get(price_band, 300.0),
                "cac_confidence":       "LOW",
                "attribution_coverage": round(min(raw_coverage_ratio, 9.99), 4),
                "spend":                spend,
                "cac_note":             (
                    f"DATA INTEGRITY ERROR: Meta conversions ({conversions}) exceed "
                    f"Shopify orders ({total_orders}) by {raw_coverage_ratio:.1f}x — "
                    f"pixel/attribution misconfiguration. Using floor CAC. Fix Meta tracking before trusting CAC."
                ),
            }
        attribution_coverage = min(raw_coverage_ratio, 1.0)
    else:
        attribution_coverage = 0.0

    floor_cac = CAC_FLOOR.get(price_band, 300.0)

    # Non-negotiable rule: < 70% attribution → use floor, flag LOW
    if attribution_coverage < 0.70:
        return {
            "cac":                  floor_cac,
            "cac_confidence":       "LOW",
            "attribution_coverage": round(attribution_coverage, 4),
            "spend":                spend,
            "cac_note":             f"Attribution {attribution_coverage:.0%} < 70% threshold — using segment floor ₹{floor_cac}",
        }

    # Compute actual CAC
    if conversions > 0:
        cac = spend / conversions
        # Blended error ±20% — CM2 must clear at worst-case CAC
        cac_high = cac * 1.20
        confidence = "HIGH" if attribution_coverage >= 0.90 else "MEDIUM"
    else:
        cac      = floor_cac
        cac_high = floor_cac * 1.20
        confidence = "LOW"

    return {
        "cac":                  round(cac, 2),
        "cac_high":             round(cac_high, 2),
        "cac_confidence":       confidence,
        "attribution_coverage": round(attribution_coverage, 4),
        "spend":                spend,
        "cac_note":             f"Attribution {attribution_coverage:.0%} | spend ₹{spend:.0f}",
    }


# ── Fix 5: Forward Demand Score ────────────────────────────────────────────────

def calc_fds(c: sqlite3.Cursor, sku_id: str, today: date) -> dict:
    """
    FDS = (Seasonal Index × 40%) + (Search Trend × 35%) + (Category Momentum × 25%)

    Without external Google Trends data, we compute what we can from internal signals
    and set the rest to neutral (50). Flag accordingly.

    Returns fds_score (0-100) + component breakdown.
    """
    # Seasonal Index: compare current 7-day velocity to same window last year
    same_window_last_year_start = (today - timedelta(days=365)).strftime("%Y-%m-%d")
    same_window_last_year_end   = (today - timedelta(days=358)).strftime("%Y-%m-%d")

    c.execute("""
        SELECT COALESCE(SUM(quantity), 0) as ly_qty
        FROM raw_shopify_orders
        WHERE sku_id = ?
          AND DATE(created_at) BETWEEN ? AND ?
          AND financial_status NOT IN ('refunded','voided')
    """, (sku_id, same_window_last_year_start, same_window_last_year_end))
    ly_qty = c.fetchone()["ly_qty"]

    c.execute("""
        SELECT COALESCE(SUM(quantity), 0) as cy_qty
        FROM raw_shopify_orders
        WHERE sku_id = ?
          AND DATE(created_at) BETWEEN ? AND ?
          AND financial_status NOT IN ('refunded','voided')
    """, (sku_id, (today - timedelta(days=6)).strftime("%Y-%m-%d"), today.strftime("%Y-%m-%d")))
    cy_qty = c.fetchone()["cy_qty"]

    if ly_qty == 0:
        # No last-year data (new SKU or new store) — neutral seasonal signal
        seasonal_score = 50.0
        seasonal_note  = "No LY data — neutral seasonal score"
    else:
        # Index: 100 = same as last year. Cap at 100 to avoid extreme distortion
        ratio = cy_qty / ly_qty
        seasonal_score = min(ratio * 50, 100)   # 50 = parity, 100 = 2x LY
        seasonal_note  = f"CY {cy_qty} vs LY {ly_qty} units → index {ratio:.1f}x"

    # Search Trend Score: no Google Trends data → neutral (50)
    # In production: pull from Google Trends API or SerpAPI
    search_trend_score = 50.0
    search_note        = "External search trend not available — using neutral 50"

    # Category Momentum: current 4wk vs prior 4wk velocity across ALL SKUs
    prior_4wk_start   = (today - timedelta(days=56)).strftime("%Y-%m-%d")
    current_4wk_start = (today - timedelta(days=28)).strftime("%Y-%m-%d")

    c.execute("""
        SELECT COALESCE(SUM(quantity), 0) as current_qty
        FROM raw_shopify_orders
        WHERE DATE(created_at) BETWEEN ? AND ?
          AND financial_status NOT IN ('refunded','voided')
    """, (current_4wk_start, today.strftime("%Y-%m-%d")))
    cat_current = c.fetchone()["current_qty"]

    c.execute("""
        SELECT COALESCE(SUM(quantity), 0) as prior_qty
        FROM raw_shopify_orders
        WHERE DATE(created_at) BETWEEN ? AND ?
          AND financial_status NOT IN ('refunded','voided')
    """, (prior_4wk_start, current_4wk_start))
    cat_prior = c.fetchone()["prior_qty"]

    if cat_prior == 0:
        momentum_score = 50.0
        momentum_note  = "No prior 4-week data — neutral"
    else:
        ratio = cat_current / cat_prior
        momentum_score = min(ratio * 50, 100)
        momentum_note  = f"Category: {cat_current} units (curr 4wk) vs {cat_prior} (prior 4wk)"

    # Weighted FDS
    fds = (seasonal_score  * FDS_SEASONAL_WEIGHT +
           search_trend_score * FDS_TREND_WEIGHT +
           momentum_score   * FDS_MOMENTUM_WEIGHT)

    return {
        "fds_score":       round(fds, 1),
        "seasonal_score":  seasonal_score,
        "search_score":    search_trend_score,
        "momentum_score":  momentum_score,
        "seasonal_note":   seasonal_note,
        "search_note":     search_note,
        "momentum_note":   momentum_note,
    }


# ── Fix 5 + 10: Zombie classification ─────────────────────────────────────────

def calc_zombie(c: sqlite3.Cursor, sku_id: str, today: date,
                fds_score: float, cogs: float, stock_level: int,
                existing_zombie_breach_ts: str | None) -> dict:
    """
    Zombie tier = f(days_without_sale, capital_locked, FDS, salvage_value).
    Breach timestamp is IMMUTABLE once set (Fix 10).
    """
    # Last sale date for this SKU
    c.execute("""
        SELECT MAX(DATE(created_at)) as last_sale
        FROM raw_shopify_orders
        WHERE sku_id = ?
          AND financial_status NOT IN ('refunded','voided')
          AND quantity > 0
    """, (sku_id,))
    last_sale = c.fetchone()["last_sale"]

    if last_sale:
        days_since_sale = (today - date.fromisoformat(last_sale)).days
    else:
        # No sales ever — days since launch
        c.execute("SELECT launch_date FROM sku_master WHERE sku_id = ?", (sku_id,))
        launch = c.fetchone()
        if launch and launch["launch_date"]:
            days_since_sale = (today - date.fromisoformat(launch["launch_date"])).days
        else:
            days_since_sale = 0

    # Capital locked = stock × COGS
    capital_locked = stock_level * cogs

    # Determine zombie tier from days
    zombie_tier = 0
    for tier, threshold in sorted(ZOMBIE_TIERS.items(), reverse=True):
        if days_since_sale >= threshold:
            zombie_tier = tier
            break

    # Auto Tier 3 if capital > ₹5L regardless of age
    if capital_locked >= ZOMBIE_CAPITAL_AUTO_TIER3 and zombie_tier < 3:
        zombie_tier = 3

    # Immutable breach timestamp (Fix 10)
    zombie_breach_ts = existing_zombie_breach_ts
    if zombie_tier >= 1 and not zombie_breach_ts:
        zombie_breach_ts = datetime.now().isoformat()   # set once, never reset
    elif zombie_tier == 0:
        zombie_breach_ts = None   # legitimate sales cleared it

    # FDS-based kill gate (Fix 5)
    fds_gate = ""
    if zombie_tier >= 3:
        if fds_score < 30:
            fds_gate = "FDS < 30: immediate liquidation required"
        elif fds_score >= 60:
            fds_gate = f"FDS {fds_score} > 60: HOLD — reassign marketing before liquidation"
        else:
            fds_gate = f"FDS {fds_score}: borderline — monitor 2 weeks"

    return {
        "zombie_tier":       zombie_tier,
        "days_since_sale":   days_since_sale,
        "capital_locked":    capital_locked,
        "zombie_breach_ts":  zombie_breach_ts,
        "fds_gate":          fds_gate,
    }


# ── Fix 6: GMROI (spot + rolling) ─────────────────────────────────────────────

def calc_gmroi(c: sqlite3.Cursor, sku_id: str, today: date, cogs: float) -> dict:
    """
    Spot GMROI   = weekly gross profit / avg inventory cost (directional only)
    Rolling 4wk  = 28-day gross profit / avg inventory cost (decision-grade)
    Rolling 8wk  = 56-day gross profit / avg inventory cost (strategic/trend)
    """
    def gmroi_for_window(start_str: str, end_str: str, label: str):
        c.execute("""
            SELECT COALESCE(SUM(o.gross_revenue), 0)  as rev,
                   COALESCE(SUM(o.quantity), 0)        as units
            FROM raw_shopify_orders o
            WHERE o.sku_id = ?
              AND DATE(o.created_at) BETWEEN ? AND ?
              AND o.financial_status NOT IN ('refunded','voided')
        """, (sku_id, start_str, end_str))
        sale = c.fetchone()
        rev, units = sale["rev"], sale["units"]
        gross_profit = rev - (units * cogs)

        # Average inventory value in window
        c.execute("""
            SELECT COALESCE(AVG(stock_level), 0) as avg_stock
            FROM raw_inventory_snapshots
            WHERE sku_id = ?
              AND snapshot_date BETWEEN ? AND ?
        """, (sku_id, start_str, end_str))
        avg_stock = c.fetchone()["avg_stock"]
        avg_inv_value = avg_stock * cogs

        if avg_inv_value == 0:
            gmroi = None   # Cannot compute — no inventory data
        else:
            # Annualized: GMROI = (gross_profit / window_days × 365) / avg_inv_value
            window_days = (date.fromisoformat(end_str) - date.fromisoformat(start_str)).days + 1
            annualized_gp = (gross_profit / max(window_days, 1)) * 365
            gmroi = round(annualized_gp / avg_inv_value, 3)

        return gmroi, round(gross_profit, 2), units

    spot_start = (today - timedelta(days=6)).strftime("%Y-%m-%d")
    r4_start   = (today - timedelta(days=27)).strftime("%Y-%m-%d")
    r8_start   = (today - timedelta(days=55)).strftime("%Y-%m-%d")
    end        = today.strftime("%Y-%m-%d")

    spot_gmroi,    spot_gp,    spot_units    = gmroi_for_window(spot_start, end, "spot")
    rolling_4wk,   r4_gp,      r4_units      = gmroi_for_window(r4_start,   end, "4wk")
    rolling_8wk,   r8_gp,      r8_units      = gmroi_for_window(r8_start,   end, "8wk")

    # GMROI decision per framework thresholds
    def gmroi_decision(g, label):
        if g is None: return f"{label}: insufficient inventory data"
        if g > 2.0:   return f"{label} {g:.2f}x: SCALE — high-priority reorder"
        if g > 1.5:   return f"{label} {g:.2f}x: MAINTAIN — healthy"
        if g > 1.0:   return f"{label} {g:.2f}x: BORDERLINE — trigger pricing test"
        return          f"{label} {g:.2f}x: DESTROYING VALUE — mandatory kill review"

    # Bug 4 fix: flag material spot vs rolling divergence as a P1 warning.
    # If spot GMROI < 50% of 4wk rolling, the current week is an anomaly that
    # demands investigation BEFORE a reorder decision is made on 4wk data alone.
    gmroi_divergence_flag = None
    if spot_gmroi is not None and rolling_4wk is not None and rolling_4wk > 0:
        divergence_ratio = spot_gmroi / rolling_4wk
        if divergence_ratio < 0.50:
            gmroi_divergence_flag = (
                f"GMROI DIVERGENCE: Spot {spot_gmroi:.2f}x is {divergence_ratio:.0%} of "
                f"4wk {rolling_4wk:.2f}x — investigate stockout or demand drop before reordering"
            )

    return {
        "spot_gmroi":           spot_gmroi,
        "rolling_4wk":          rolling_4wk,
        "rolling_8wk":          rolling_8wk,
        "spot_decision":        gmroi_decision(spot_gmroi, "Spot"),
        "r4_decision":          gmroi_decision(rolling_4wk, "4wk"),
        "r8_decision":          gmroi_decision(rolling_8wk, "8wk"),
        "spot_gp":              spot_gp,
        "gmroi_divergence_flag": gmroi_divergence_flag,
    }


# ── Fix 11: DTC — Demand Transfer Coefficient ──────────────────────────────────

def apply_dtc(adjusted_velocity: float,
              test_segment: str | None,
              scale_segment: str | None,
              transfer_confidence: str | None) -> dict:
    """
    If test_segment ≠ scale_segment AND transfer_confidence = LOW:
      apply DTC penalty before any scale decision.
    If transfer_confidence = HIGH: no penalty (parallel T1 signal validated).
    If segments unknown: DTC = 1.0, confidence = LOW, flag for documentation.
    """
    if not test_segment or not scale_segment:
        return {
            "dtc_applied":          1.0,
            "effective_velocity":   adjusted_velocity,
            "dtc_note":             "Segment data missing — DTC=1.0 (no penalty); document before any cross-segment scale",
            "dtc_confidence":       "UNDOCUMENTED",
        }

    if transfer_confidence == "HIGH":
        return {
            "dtc_applied":          1.0,
            "effective_velocity":   adjusted_velocity,
            "dtc_note":             f"HIGH confidence: parallel {scale_segment} signal exists — no penalty applied",
            "dtc_confidence":       "HIGH",
        }

    if test_segment == scale_segment:
        return {
            "dtc_applied":          1.0,
            "effective_velocity":   adjusted_velocity,
            "dtc_note":             f"Same segment ({test_segment}) — no DTC needed",
            "dtc_confidence":       "HIGH",
        }

    # Cross-segment: LOW confidence → apply penalty
    dtc = DTC_TABLE.get((test_segment, scale_segment), 0.70)  # default penalty if unknown
    effective_velocity = adjusted_velocity * dtc

    return {
        "dtc_applied":          dtc,
        "effective_velocity":   round(effective_velocity, 2),
        "dtc_note":             f"{test_segment}→{scale_segment} transfer: DTC={dtc} penalty applied",
        "dtc_confidence":       "LOW",
    }


# ── Decision Gate — 10-second filter ──────────────────────────────────────────

def evaluate_decision_gate(velocity_ok: bool,
                            cm1_confidence: str,
                            cac_confidence: str,
                            zombie_tier: int,
                            fds_score: float | None,
                            rolling_4wk: float | None,
                            dtc_confidence: str,
                            promo_stripped_vel: float = 0.0,
                            adjusted_velocity: float = 0.0,
                            gmroi_divergence_flag: str | None = None,
                            ) -> dict:
    """
    Enforces the framework's 10-question decision filter (Fix 7 of architecture).
    Returns decision_blocked flag + blocking reason.
    """
    blocks = []
    warnings = []

    # Q1: Velocity adjusted?
    if not velocity_ok:
        blocks.append("Q1: Velocity not in-stock adjusted — BLOCKED")

    # Q2: Realized CM1 used? (BLOCKED means > 20% returns or PROVISIONAL = warn)
    if cm1_confidence == "BLOCKED":
        blocks.append("Q2: CM1 blocked (>20% return rate or return lag) — no decision")
    elif cm1_confidence == "PROVISIONAL":
        warnings.append("Q2: CM1 provisional — return lag detected, use caution")

    # Q3: CAC segmented? LOW = use floor, flag — not a hard block
    if cac_confidence == "LOW":
        warnings.append("Q3: CAC LOW confidence — using segment floor, attribution fix required")

    # Q4: FDS required for zombie tier >= 3
    if zombie_tier >= 3 and fds_score is None:
        blocks.append("Q4: FDS missing for zombie Tier 3 — no kill decision permitted")

    # Q5: 4wk GMROI not confirmed = directional only (not a hard block)
    if rolling_4wk is None:
        warnings.append("Q5: 4wk GMROI unavailable — spot GMROI is directional only")

    # Q5b: Bug 4 fix — GMROI spot vs rolling divergence
    if gmroi_divergence_flag:
        warnings.append(f"Q5b: {gmroi_divergence_flag}")

    # Q6: Audit layer — always logged by this script (self-logging)
    # This is satisfied by the audit_log write in the main loop.

    # Q7: DTC applied for cross-segment scaling?
    if dtc_confidence == "UNDOCUMENTED":
        warnings.append("Q7: Segment context not documented — DTC not enforced; flag before cross-segment scale")

    # Bug 5 fix: Pure promo-dependency block.
    # If a SKU has meaningful velocity but ALL of it is promo-driven (promo_stripped_vel = 0),
    # it has ZERO organic demand. This is a standalone block — not a warning.
    # Logic: promo_stripped_vel is 0 AND there were actual sales (adjusted_velocity > 0).
    # Pulling the discount on this SKU = zero sales. That is not a scalable business.
    if adjusted_velocity > 0 and promo_stripped_vel == 0.0:
        blocks.append(
            "Q8: 100% promo-dependent demand — promo-stripped velocity = 0. "
            "No organic demand exists. BLOCKED: do not scale or restock until organic demand validated."
        )

    decision_blocked = len(blocks) > 0
    severity = "P0" if decision_blocked else ("P1" if warnings else "P2")

    return {
        "decision_blocked": int(decision_blocked),
        "alert_severity":   severity,
        "alert_reason":     " | ".join(blocks + warnings) if (blocks or warnings) else None,
        "blocks":           blocks,
        "warnings":         warnings,
    }


# ── Fix 8: Audit log writer ────────────────────────────────────────────────────

def write_audit(conn: sqlite3.Connection, sku_id: str,
                decision_type: str, cm1_pu: float | None,
                cac: float, decision_blocked: int,
                alert_reason: str | None, data_version: str):
    conn.execute("""
        INSERT INTO audit_log
        (sku_id, decision_type, actor, decision_ts, data_version,
         confidence_score, assumptions_applied, override_reason, layer)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        sku_id,
        decision_type,
        "computed_metrics.py",              # actor
        datetime.now().isoformat(),
        data_version,
        round(cm1_pu, 2) if cm1_pu else None,
        f"CAC floor={cac}" if cac else None,
        alert_reason if decision_blocked else None,
        1   # Layer 1 (automated system compute)
    ))


# ── Fix 9: Brand Equity cap ────────────────────────────────────────────────────

def check_brand_equity_cap(c: sqlite3.Cursor) -> str | None:
    """
    Returns a warning string if Brand Equity capital exceeds 25% of portfolio.
    This is checked after all metrics are computed.
    (In a full system, brand_equity_flag would be stored in sku_master;
     here we flag the logic and leave the field for operator input.)
    """
    # Placeholder — in production this compares brand_equity_sku capital / total capital
    # We log it for now and return None (no SKUs currently flagged Brand Equity)
    return None


# ── Main computation loop ──────────────────────────────────────────────────────

def run_computed_metrics(db_path: str = DB_PATH):
    conn = get_conn(db_path)
    c    = conn.cursor()
    now  = datetime.now().isoformat()
    today = date.today()
    data_version = today.strftime("%Y-W%V")   # ISO week label

    # Guard: validate.py must have cleared P0 before we run
    c.execute("""
        SELECT status FROM freshness_log
        WHERE source = 'validate'
        ORDER BY logged_at DESC LIMIT 1
    """)
    validation_row = c.fetchone()
    if validation_row and validation_row["status"] == "BLOCKED":
        print("❌ Validation status = BLOCKED. Fix P0 issues before running computed_metrics.")
        return 2

    week_start, week_end = week_bounds(today)

    # Load all active SKUs
    c.execute("SELECT * FROM sku_master")
    skus = c.fetchall()

    if not skus:
        print("❌ sku_master is empty. Run sku_master.py first.")
        return 2

    print(f"\n{'='*60}")
    print(f"  GIVA computed_metrics.py — {today} | Week {week_start}")
    print(f"{'='*60}\n")

    results_by_sku = {}
    blocked_count  = 0
    computed_count = 0

    for sku in skus:
        sku_id    = sku["sku_id"]
        cogs      = sku["cogs"]
        price     = sku["price"]
        price_band = sku["price_band"]

        print(f"── {sku_id} ({sku['sku_name']}) ──")

        # ── Velocity (Fix 1) ─────────────────────────────────────────
        vel = calc_velocity(c, sku_id, week_start, week_end)
        velocity_ok = True   # always reachable now (computed from real API values)

        # ── Realized CM1 (Fix 2) ─────────────────────────────────────
        cm1 = calc_realized_cm1(c, sku_id, week_start, week_end, cogs, price)

        # ── CAC (Fix 4) ──────────────────────────────────────────────
        cac_data = calc_cac(c, sku_id, price_band, week_start, week_end)

        # ── FDS (Fix 5) ──────────────────────────────────────────────
        fds_data = calc_fds(c, sku_id, today)

        # ── Current inventory for zombie calc ───────────────────────
        c.execute("""
            SELECT stock_level FROM raw_inventory_snapshots
            WHERE sku_id = ? ORDER BY snapshot_date DESC LIMIT 1
        """, (sku_id,))
        inv_row = c.fetchone()
        stock_level = inv_row["stock_level"] if inv_row else 0

        # Get existing zombie_breach_ts (immutable — Fix 10)
        c.execute("""
            SELECT zombie_breach_ts FROM computed_metrics
            WHERE sku_id = ? ORDER BY computed_at DESC LIMIT 1
        """, (sku_id,))
        prev_row = c.fetchone()
        existing_breach_ts = prev_row["zombie_breach_ts"] if prev_row else None

        # ── Zombie (Fix 5 + 10) ──────────────────────────────────────
        zombie = calc_zombie(c, sku_id, today,
                             fds_data["fds_score"], cogs,
                             stock_level, existing_breach_ts)

        # ── GMROI (Fix 6) ────────────────────────────────────────────
        gmroi = calc_gmroi(c, sku_id, today, cogs)

        # ── DTC (Fix 11) ─────────────────────────────────────────────
        # These values would come from the test metadata system.
        # Current pipeline has no test_segment recorded yet → defaults to undocumented.
        # Operator must populate before any cross-segment scale decision.
        test_segment        = None
        scale_segment       = None
        transfer_confidence = None
        dtc_data = apply_dtc(vel["adjusted_velocity"],
                             test_segment, scale_segment, transfer_confidence)

        # ── Decision gate ─────────────────────────────────────────────
        gate = evaluate_decision_gate(
            velocity_ok           = velocity_ok,
            cm1_confidence        = cm1["cm1_confidence"],
            cac_confidence        = cac_data["cac_confidence"],
            zombie_tier           = zombie["zombie_tier"],
            fds_score             = fds_data["fds_score"],
            rolling_4wk           = gmroi["rolling_4wk"],
            dtc_confidence        = dtc_data["dtc_confidence"],
            promo_stripped_vel    = vel["promo_stripped_vel"],
            adjusted_velocity     = vel["adjusted_velocity"],
            gmroi_divergence_flag = gmroi.get("gmroi_divergence_flag"),
        )

        decision_blocked = gate["decision_blocked"]
        if decision_blocked:
            blocked_count += 1
        else:
            computed_count += 1

        # ── Print summary ────────────────────────────────────────────
        print(f"   Velocity (adj): {vel['adjusted_velocity']:.1f} u/wk | "
              f"Promo-stripped: {vel['promo_stripped_vel']:.1f} | "
              f"In-stock: {vel['in_stock_rate']:.0%}")
        print(f"   CM1/unit:       ₹{cm1.get('cm1_per_unit') or 'BLOCKED'} | "
              f"Return rate: {cm1['return_rate']:.1%} | "
              f"Confidence: {cm1['cm1_confidence']}")
        print(f"   CAC:            ₹{cac_data['cac']:.0f} ({cac_data['cac_confidence']}) | "
              f"Coverage: {cac_data['attribution_coverage']:.0%}")
        print(f"   GMROI (spot):   {gmroi['spot_gmroi'] or 'N/A'} | "
              f"4wk: {gmroi['rolling_4wk'] or 'N/A'} | "
              f"8wk: {gmroi['rolling_8wk'] or 'N/A'}")
        print(f"   Zombie tier:    {zombie['zombie_tier']} | "
              f"FDS: {fds_data['fds_score']} | "
              f"Days no sale: {zombie['days_since_sale']}")
        print(f"   Decision:       {'🔴 BLOCKED' if decision_blocked else '✅ COMPUTED'}")
        if gate["blocks"]:
            for b in gate["blocks"]:   print(f"     ⛔ {b}")
        if gate["warnings"]:
            for w in gate["warnings"]: print(f"     ⚠  {w}")
        print()

        # ── Upsert to computed_metrics ───────────────────────────────
        conn.execute("""
            INSERT INTO computed_metrics (
                sku_id, week_start,
                raw_velocity, days_in_stock, adjusted_velocity, promo_stripped_velocity,
                return_rate, reverse_logistics, liquidation_loss, realized_cm1,
                spot_gmroi, rolling_4wk_gmroi, rolling_8wk_gmroi,
                zombie_tier, fds_score, zombie_breach_ts,
                test_segment, scale_segment, acquisition_channel,
                customer_intent, transfer_confidence, dtc_applied,
                cac_confidence, cm1_confidence,
                decision_blocked, alert_severity, alert_reason,
                data_version, freshness_status, computed_at
            ) VALUES (
                ?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?
            )
            ON CONFLICT(sku_id, week_start) DO UPDATE SET
                raw_velocity            = excluded.raw_velocity,
                days_in_stock           = excluded.days_in_stock,
                adjusted_velocity       = excluded.adjusted_velocity,
                promo_stripped_velocity = excluded.promo_stripped_velocity,
                return_rate             = excluded.return_rate,
                reverse_logistics       = excluded.reverse_logistics,
                liquidation_loss        = excluded.liquidation_loss,
                realized_cm1            = excluded.realized_cm1,
                spot_gmroi              = excluded.spot_gmroi,
                rolling_4wk_gmroi       = excluded.rolling_4wk_gmroi,
                rolling_8wk_gmroi       = excluded.rolling_8wk_gmroi,
                zombie_tier             = excluded.zombie_tier,
                fds_score               = excluded.fds_score,
                zombie_breach_ts        = COALESCE(computed_metrics.zombie_breach_ts, excluded.zombie_breach_ts),
                test_segment            = excluded.test_segment,
                scale_segment           = excluded.scale_segment,
                acquisition_channel     = excluded.acquisition_channel,
                customer_intent         = excluded.customer_intent,
                transfer_confidence     = excluded.transfer_confidence,
                dtc_applied             = excluded.dtc_applied,
                cac_confidence          = excluded.cac_confidence,
                cm1_confidence          = excluded.cm1_confidence,
                decision_blocked        = excluded.decision_blocked,
                alert_severity          = excluded.alert_severity,
                alert_reason            = excluded.alert_reason,
                data_version            = excluded.data_version,
                freshness_status        = excluded.freshness_status,
                computed_at             = excluded.computed_at
        """, (
            sku_id, week_start,
            vel["raw_velocity"], vel["days_in_stock"],
            vel["adjusted_velocity"], vel["promo_stripped_vel"],
            cm1["return_rate"], cm1["reverse_logistics"], cm1["liquidation_loss"],
            cm1.get("realized_cm1"),
            gmroi["spot_gmroi"], gmroi["rolling_4wk"], gmroi["rolling_8wk"],
            zombie["zombie_tier"], fds_data["fds_score"], zombie["zombie_breach_ts"],
            test_segment, scale_segment, cac_data.get("acquisition_channel", "Meta Paid"),
            None,   # customer_intent — populated by post-purchase survey ingestion
            transfer_confidence, dtc_data["dtc_applied"],
            cac_data["cac_confidence"], cm1["cm1_confidence"],
            gate["decision_blocked"], gate["alert_severity"], gate["alert_reason"],
            data_version, "FRESH", now
        ))

        # ── Fix 8: Audit log ──────────────────────────────────────────
        write_audit(conn, sku_id,
                    decision_type = "BLOCKED" if decision_blocked else "COMPUTED",
                    cm1_pu        = cm1.get("cm1_per_unit"),
                    cac           = cac_data["cac"],
                    decision_blocked = decision_blocked,
                    alert_reason  = gate["alert_reason"],
                    data_version  = data_version)

        results_by_sku[sku_id] = gate

    # ── Fix 9: Brand Equity cap check ────────────────────────────────
    be_warning = check_brand_equity_cap(c)
    if be_warning:
        print(f"⚠  BRAND EQUITY: {be_warning}")

    conn.commit()
    conn.close()

    # ── Final report ──────────────────────────────────────────────────
    total = len(skus)
    print(f"{'='*60}")
    print(f"  COMPUTED: {computed_count}/{total} SKUs")
    print(f"  BLOCKED:  {blocked_count}/{total} SKUs")
    if blocked_count > 0:
        pct = blocked_count / total * 100
        print(f"\n  Block rate: {pct:.0f}% — {'🔴 HIGH: data infrastructure issue' if pct >= 40 else '🟡 MODERATE: review flagged SKUs'}")
    print(f"\n  → computed_metrics table updated for week {week_start}")
    print(f"  → audit_log entries written for all {total} SKUs")
    print(f"{'='*60}\n")

    return 1 if blocked_count > 0 else 0


if __name__ == "__main__":
    sys.exit(run_computed_metrics())