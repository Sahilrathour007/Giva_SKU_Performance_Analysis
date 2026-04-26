"""
run_pipeline.py — GIVA Framework v6  Daily Pipeline Orchestrator

Execution order (mirrors framework's mandatory sequence):
  1. db_setup        — ensure schema is current
  2. ingest_shopify  — orders, refunds, inventory
  3. ingest_meta_ads — ad spend + conversions (skip if not configured)
  4. validate        — logical + cross-table checks; HALT on P0
  5. reconcile       — weekly (runs if today is Monday or forced)
  6. [computed_metrics.py — separate module, run after this script passes]

Usage:
  python run_pipeline.py             # normal daily run
  python run_pipeline.py --reconcile # force reconciliation run
  python run_pipeline.py --dry-run   # setup + validate only; no ingestion
"""

import sys
import os
import subprocess
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
today = datetime.now()
force_reconcile = "--reconcile" in sys.argv
dry_run         = "--dry-run" in sys.argv


def run(script: str, label: str) -> int:
    print(f"\n{'='*50}")
    print(f"  STEP: {label}")
    print(f"{'='*50}")
    result = subprocess.run(
        [sys.executable, os.path.join(HERE, script)],
        capture_output=False
    )
    if result.returncode not in (0, 1):   # 1 = P1 warnings = ok to continue
        print(f"\n❌ {label} exited with code {result.returncode} — PIPELINE HALTED")
        sys.exit(result.returncode)
    return result.returncode


# Step 1
run("db_setup.py", "Schema setup")

if not dry_run:
    # Step 2
    run("ingest_shopify.py", "Shopify ingestion")

    # Step 3 — skip gracefully if Meta not configured
    if os.environ.get("META_ACCESS_TOKEN"):
        run("ingest_meta_ads.py", "Meta Ads ingestion")
    else:
        print("\n[SKIP] Meta Ads: META_ACCESS_TOKEN not set — CAC will use floor values")

# Step 4 — always validate
exit_code = run("validate.py", "Data validation")

# Step 5 — reconcile on Mondays or when forced
if force_reconcile or today.weekday() == 0:
    run("reconcile.py", "Reconciliation")

print("\n" + "="*50)
if exit_code == 0:
    print("  ✅ Pipeline complete. Ready to run computed_metrics.py")
elif exit_code == 1:
    print("  ⚠️  Pipeline complete with P1 warnings. Review before computed_metrics.py")
print("="*50 + "\n")
