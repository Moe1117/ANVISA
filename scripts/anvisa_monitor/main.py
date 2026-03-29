"""
ANVISA Regulatory Monitor — Main Orchestrator

Execution flow:
  1. Scrape ANVISA portal (gov.br/anvisa)
  2. Scrape DOU (in.gov.br)
  3. Classify with Claude API
  4. Write relevant items to Supabase
  5. Send Resend email summary
"""

import logging
import os
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Configure logging
log_dir = Path(__file__).parent / "logs"
log_dir.mkdir(exist_ok=True)

run_id = str(uuid.uuid4())[:8]
log_file = log_dir / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{run_id}.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(log_file, encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)


def main():
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
    lookback_days = int(os.environ.get("LOOKBACK_DAYS", "35"))
    since = datetime.now(timezone.utc) - timedelta(days=lookback_days)

    logger.info("=" * 60)
    logger.info(f"ANVISA Monitor starting — Run ID: {run_id}")
    logger.info(f"Mode: {'DRY RUN' if dry_run else 'LIVE'}")
    logger.info(f"Lookback: {lookback_days} days (since {since.strftime('%Y-%m-%d')})")
    logger.info("=" * 60)

    # Validate required env vars
    required_env = ["ANTHROPIC_API_KEY", "SUPABASE_URL", "SUPABASE_SERVICE_KEY"]
    missing = [v for v in required_env if not os.environ.get(v)]
    if missing:
        logger.error(f"Missing required env vars: {missing}")
        sys.exit(1)

    from scraper import run_anvisa_scraper
    from dou_scraper import run_dou_scraper
    from classifier import classify_batch, generate_run_summary
    from supabase_updater import process_relevant_publications, log_scrape_run
    from ingredient_sync import sync_all_changes
    from notifier import send_alert

    all_publications = []
    sources_scraped = []

    # --- Step 1: Scrape ANVISA portal ---
    logger.info("Step 1: Scraping ANVISA portal...")
    try:
        anvisa_results = run_anvisa_scraper(since)
        all_publications.extend(anvisa_results)
        sources_scraped.append("anvisa_portal")
        logger.info(f"ANVISA portal: {len(anvisa_results)} items")
    except Exception as e:
        logger.error(f"ANVISA portal scraper failed: {e}")

    # --- Step 2: Scrape DOU ---
    logger.info("Step 2: Scraping DOU...")
    try:
        dou_results = run_dou_scraper(since)
        all_publications.extend(dou_results)
        sources_scraped.append("dou")
        logger.info(f"DOU: {len(dou_results)} items")
    except Exception as e:
        logger.error(f"DOU scraper failed: {e}")

    total_scraped = len(all_publications)
    logger.info(f"Total publications to classify: {total_scraped}")

    if not all_publications:
        logger.warning("No publications found — check if scrapers need updating")

    # --- Step 3: Classify with Claude ---
    logger.info("Step 3: Classifying with Claude API...")
    relevant, irrelevant = [], []
    try:
        relevant, irrelevant = classify_batch(all_publications)
    except Exception as e:
        logger.error(f"Classification failed: {e}")

    logger.info(f"Relevant: {len(relevant)} | Irrelevant: {len(irrelevant)}")

    # --- Step 4: Update Supabase (publications + changes log) ---
    logger.info("Step 4: Writing publications to Supabase...")
    ingredient_changes = 0
    run_status = "success"
    error_msg = None
    try:
        ingredient_changes = process_relevant_publications(relevant, dry_run=dry_run)
    except Exception as e:
        run_status = "partial_failure"
        error_msg = str(e)
        logger.error(f"Supabase update failed: {e}")

    # --- Step 4b: Sync changes to live anvisa_ingredients table ---
    logger.info("Step 4b: Syncing to live ingredient database...")
    sync_summary = {"total_changes": 0}
    try:
        sync_summary = sync_all_changes(relevant, dry_run=dry_run)
        ingredient_changes += sync_summary["total_changes"]
    except Exception as e:
        logger.error(f"Ingredient sync failed: {e}")
        if run_status == "success":
            run_status = "partial_failure"
        error_msg = (error_msg or "") + f" | Sync error: {e}"

    # Log the run itself
    if not dry_run:
        try:
            log_scrape_run(
                run_id=run_id,
                status=run_status,
                sources_scraped=sources_scraped,
                total_found=total_scraped,
                relevant_count=len(relevant),
                ingredient_changes=ingredient_changes,
                error_message=error_msg,
                dry_run=dry_run,
            )
        except Exception as e:
            logger.error(f"Failed to log run: {e}")

    # --- Step 5: Generate summary + send email ---
    logger.info("Step 5: Sending email alert...")
    try:
        summary = generate_run_summary(relevant, irrelevant, dry_run)
        send_alert(
            summary=summary,
            relevant=relevant,
            total_scraped=total_scraped,
            ingredient_changes=ingredient_changes,
            run_id=run_id,
            dry_run=dry_run,
        )
    except Exception as e:
        logger.error(f"Notification failed: {e}")

    logger.info("=" * 60)
    logger.info(f"Run complete — {run_id}")
    logger.info(f"  Scraped: {total_scraped} | Relevant: {len(relevant)} | Changes: {ingredient_changes}")
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
