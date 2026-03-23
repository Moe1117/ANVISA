"""
Writes classified ANVISA publications and extracted ingredient changes
to Supabase. Uses upsert logic to avoid duplicate entries.
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any

from supabase import create_client, Client

logger = logging.getLogger(__name__)

_client: Client | None = None


def get_client() -> Client:
    global _client
    if _client is None:
        url = os.environ["SUPABASE_URL"]
        key = os.environ["SUPABASE_SERVICE_KEY"]
        _client = create_client(url, key)
    return _client


def upsert_publication(pub: dict) -> dict | None:
    """
    Upsert a publication record into anvisa_publications table.
    Conflict key: (url) — if URL is empty, use (title, source).
    Returns the inserted/updated record.
    """
    client = get_client()
    classification = pub.get("classification", {})

    record = {
        "title": pub.get("title", "")[:500],
        "url": pub.get("url", "") or None,
        "source": pub.get("source", ""),
        "pub_date": pub.get("pub_date") or None,
        "publication_number": (
            pub.get("publication_number")
            or classification.get("publication_number")
        ),
        "publication_type": classification.get("publication_type"),
        "change_type": classification.get("change_type"),
        "summary_pt": classification.get("summary_pt"),
        "summary_en": classification.get("summary_en"),
        "urgency": classification.get("urgency", "low"),
        "amends_document": classification.get("amends_document"),
        "effective_date": classification.get("effective_date") or None,
        "raw_text": pub.get("raw_text", "")[:2000],
        "is_relevant": classification.get("is_relevant", False),
        "processed_at": datetime.now(timezone.utc).isoformat(),
    }

    try:
        result = (
            client.table("anvisa_publications")
            .upsert(record, on_conflict="url")
            .execute()
        )
        if result.data:
            logger.debug(f"Upserted publication: {record['title'][:60]}")
            return result.data[0]
    except Exception as e:
        logger.error(f"Failed to upsert publication '{record['title'][:60]}': {e}")

    return None


def upsert_ingredient_changes(pub: dict, publication_id: str | None = None) -> int:
    """
    Write individual ingredient changes extracted from a publication.
    Returns count of records written.
    """
    client = get_client()
    classification = pub.get("classification", {})
    count = 0

    def write_change(name_pt: str, name_en: str, change_type: str, extra: dict):
        nonlocal count
        record = {
            "publication_id": publication_id,
            "publication_number": classification.get("publication_number"),
            "pub_date": pub.get("pub_date") or None,
            "ingredient_name_pt": name_pt[:200],
            "ingredient_name_en": (name_en or "")[:200],
            "change_type": change_type,
            "category": extra.get("category"),
            "max_dose": extra.get("max_dose"),
            "dose_unit": extra.get("dose_unit"),
            "change_detail": extra.get("change_detail") or extra.get("reason"),
            "source_url": pub.get("url"),
            "processed_at": datetime.now(timezone.utc).isoformat(),
        }
        try:
            client.table("anvisa_ingredient_changes").insert(record).execute()
            count += 1
        except Exception as e:
            logger.error(f"Failed to write ingredient change for '{name_pt}': {e}")

    for item in classification.get("ingredients_added", []):
        write_change(
            item.get("name_pt", ""), item.get("name_en", ""),
            "added", item
        )

    for item in classification.get("ingredients_removed", []):
        write_change(
            item.get("name_pt", ""), item.get("name_en", ""),
            "removed", item
        )

    for item in classification.get("ingredients_modified", []):
        write_change(
            item.get("name_pt", ""), item.get("name_en", ""),
            "modified", item
        )

    if count:
        logger.info(f"Wrote {count} ingredient changes for: {pub.get('title', '')[:60]}")

    return count


def log_scrape_run(
    run_id: str,
    status: str,
    sources_scraped: list[str],
    total_found: int,
    relevant_count: int,
    ingredient_changes: int,
    error_message: str | None = None,
    dry_run: bool = False,
) -> None:
    """Log a scrape run to the anvisa_scrape_runs table."""
    client = get_client()
    record = {
        "run_id": run_id,
        "run_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "sources_scraped": sources_scraped,
        "total_publications_found": total_found,
        "relevant_count": relevant_count,
        "ingredient_changes_written": ingredient_changes,
        "error_message": error_message,
        "dry_run": dry_run,
    }
    try:
        client.table("anvisa_scrape_runs").insert(record).execute()
        logger.info(f"Run logged: {run_id} → {status}")
    except Exception as e:
        logger.error(f"Failed to log run: {e}")


def process_relevant_publications(
    relevant: list[dict],
    dry_run: bool = False,
) -> int:
    """
    Write all relevant publications and their ingredient changes to Supabase.
    Returns total ingredient change records written.
    """
    if dry_run:
        logger.info(f"DRY RUN: would write {len(relevant)} publications")
        for pub in relevant:
            c = pub.get("classification", {})
            added = len(c.get("ingredients_added", []))
            removed = len(c.get("ingredients_removed", []))
            modified = len(c.get("ingredients_modified", []))
            logger.info(
                f"  [{c.get('urgency', '?').upper()}] {pub.get('title', '')[:70]}\n"
                f"    +{added} added, -{removed} removed, ~{modified} modified"
            )
        return 0

    total_changes = 0
    for pub in relevant:
        pub_record = upsert_publication(pub)
        pub_id = pub_record["id"] if pub_record else None
        changes = upsert_ingredient_changes(pub, publication_id=pub_id)
        total_changes += changes

    logger.info(f"DB update complete: {len(relevant)} publications, {total_changes} ingredient changes")
    return total_changes
