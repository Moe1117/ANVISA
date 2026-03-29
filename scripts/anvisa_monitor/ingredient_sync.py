"""
Auto-sync detected ANVISA ingredient changes to the live anvisa_ingredients
table used by RegCheck360 BR.

When the monitor detects:
  - ingredients_added   → INSERT new rows into anvisa_ingredients
  - ingredients_removed → UPDATE status to 'removed' (soft delete)
  - ingredients_modified → UPDATE dose limits / restrictions / notes

This closes the gap between monitoring and the live website.
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


# Map classifier categories to anvisa_ingredients categories
CATEGORY_MAP = {
    "vitamins": "vitamins",
    "minerals": "minerals",
    "amino_acids": "amino_acids",
    "proteins": "proteins",
    "enzymes": "enzymes",
    "probiotics": "probiotics",
    "bioactives": "bioactives",
    "plant_extracts": "plant_extracts",
}


def sync_ingredient_additions(classification: dict, pub_url: str | None = None) -> int:
    """
    For each ingredient in ingredients_added, insert into anvisa_ingredients
    if not already present. Returns count of new ingredients added.
    """
    client = get_client()
    added = 0

    for item in classification.get("ingredients_added", []):
        name_pt = item.get("name_pt", "").strip()
        name_en = item.get("name_en", "").strip()
        if not name_pt:
            continue

        # Check if already exists (case-insensitive)
        existing = (
            client.table("anvisa_ingredients")
            .select("id")
            .ilike("ingredient_name", name_pt)
            .execute()
        )

        if existing.data:
            logger.info(f"  SKIP (exists): {name_pt}")
            continue

        category = CATEGORY_MAP.get(item.get("category", ""), "bioactives")
        max_dose = item.get("max_dose")
        dose_unit = item.get("dose_unit")

        # Build common_names array
        common_names = []
        if name_en:
            common_names.append(name_en.lower())

        record = {
            "ingredient_name": name_pt,
            "common_names": common_names,
            "category": category,
            "permitted_forms": [],
            "max_daily_dose": f"{max_dose}{dose_unit}" if max_dose and dose_unit else max_dose,
            "dose_unit": dose_unit,
            "restrictions": None,
            "regulation_ref": classification.get("publication_number") or classification.get("amends_document"),
            "status": "permitted",
            "notes": f"Auto-added by ANVISA monitor from {classification.get('publication_number', 'regulatory update')}. Source: {pub_url or 'DOU/ANVISA portal'}",
        }

        try:
            client.table("anvisa_ingredients").insert(record).execute()
            added += 1
            logger.info(f"  ADDED to live DB: {name_pt} ({category})")
        except Exception as e:
            logger.error(f"  FAILED to add {name_pt}: {e}")

    return added


def sync_ingredient_removals(classification: dict) -> int:
    """
    For each ingredient in ingredients_removed, soft-delete by setting
    status='removed' in anvisa_ingredients. Returns count of removals.
    """
    client = get_client()
    removed = 0

    for item in classification.get("ingredients_removed", []):
        name_pt = item.get("name_pt", "").strip()
        if not name_pt:
            continue

        # Find the ingredient
        existing = (
            client.table("anvisa_ingredients")
            .select("id, ingredient_name")
            .ilike("ingredient_name", f"%{name_pt}%")
            .execute()
        )

        if not existing.data:
            logger.warning(f"  SKIP removal (not found): {name_pt}")
            continue

        reason = item.get("reason", "Removed by ANVISA regulatory update")
        pub_ref = classification.get("publication_number", "")

        for row in existing.data:
            try:
                client.table("anvisa_ingredients").update({
                    "status": "removed",
                    "notes": f"REMOVED per {pub_ref}: {reason}. Previous status was permitted.",
                }).eq("id", row["id"]).execute()
                removed += 1
                logger.info(f"  REMOVED from live DB: {row['ingredient_name']}")
            except Exception as e:
                logger.error(f"  FAILED to remove {row['ingredient_name']}: {e}")

    return removed


def sync_ingredient_modifications(classification: dict) -> int:
    """
    For each ingredient in ingredients_modified, update dose limits,
    restrictions, or notes in anvisa_ingredients. Returns count of updates.
    """
    client = get_client()
    modified = 0

    for item in classification.get("ingredients_modified", []):
        name_pt = item.get("name_pt", "").strip()
        if not name_pt:
            continue

        # Find the ingredient
        existing = (
            client.table("anvisa_ingredients")
            .select("id, ingredient_name, max_daily_dose, notes")
            .ilike("ingredient_name", f"%{name_pt}%")
            .execute()
        )

        if not existing.data:
            logger.warning(f"  SKIP modification (not found): {name_pt}")
            continue

        change_detail = item.get("change_detail", "")
        pub_ref = classification.get("publication_number", "")
        update_fields: dict[str, Any] = {}

        # If change_detail mentions dose, try to extract new dose
        if item.get("max_dose"):
            dose_unit = item.get("dose_unit", "")
            update_fields["max_daily_dose"] = f"{item['max_dose']}{dose_unit}"
            if dose_unit:
                update_fields["dose_unit"] = dose_unit

        # Always update notes with change detail
        existing_notes = existing.data[0].get("notes") or ""
        timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        new_note = f"[{timestamp}] Modified per {pub_ref}: {change_detail}"
        update_fields["notes"] = f"{new_note}\n{existing_notes}".strip()

        if update_fields:
            for row in existing.data:
                try:
                    client.table("anvisa_ingredients").update(update_fields).eq("id", row["id"]).execute()
                    modified += 1
                    logger.info(f"  MODIFIED in live DB: {row['ingredient_name']} — {change_detail[:60]}")
                except Exception as e:
                    logger.error(f"  FAILED to modify {row['ingredient_name']}: {e}")

    return modified


def sync_all_changes(relevant_publications: list[dict], dry_run: bool = False) -> dict:
    """
    Process all relevant publications and sync ingredient changes to
    the live anvisa_ingredients table.

    Returns summary dict with counts.
    """
    total_added = 0
    total_removed = 0
    total_modified = 0

    logger.info("=" * 50)
    logger.info("INGREDIENT SYNC — Updating live RegCheck360 BR database")
    logger.info("=" * 50)

    for pub in relevant_publications:
        classification = pub.get("classification", {})
        change_type = classification.get("change_type", "none")

        if change_type == "none":
            continue

        title = pub.get("title", "")[:60]
        pub_url = pub.get("url")
        logger.info(f"\nProcessing: {title}")

        has_additions = bool(classification.get("ingredients_added"))
        has_removals = bool(classification.get("ingredients_removed"))
        has_modifications = bool(classification.get("ingredients_modified"))

        if not any([has_additions, has_removals, has_modifications]):
            logger.info("  No specific ingredient changes to sync")
            continue

        if dry_run:
            if has_additions:
                logger.info(f"  DRY RUN: Would add {len(classification['ingredients_added'])} ingredients")
            if has_removals:
                logger.info(f"  DRY RUN: Would remove {len(classification['ingredients_removed'])} ingredients")
            if has_modifications:
                logger.info(f"  DRY RUN: Would modify {len(classification['ingredients_modified'])} ingredients")
            continue

        if has_additions:
            total_added += sync_ingredient_additions(classification, pub_url)

        if has_removals:
            total_removed += sync_ingredient_removals(classification)

        if has_modifications:
            total_modified += sync_ingredient_modifications(classification)

    summary = {
        "ingredients_added": total_added,
        "ingredients_removed": total_removed,
        "ingredients_modified": total_modified,
        "total_changes": total_added + total_removed + total_modified,
    }

    logger.info(f"\nSync complete: +{total_added} added, -{total_removed} removed, ~{total_modified} modified")
    return summary
