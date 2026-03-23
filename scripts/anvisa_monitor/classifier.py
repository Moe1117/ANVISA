"""
Uses Claude API to classify scraped ANVISA/DOU publications and extract
structured ingredient change data from regulatory text.

Claude is better than regex here because:
- ANVISA legislation is dense Portuguese legalese
- Amendments reference other documents by number
- Changes can be additions, removals, dose modifications, or reclassifications
"""

import json
import logging
from typing import Any

import anthropic

logger = logging.getLogger(__name__)

client = anthropic.Anthropic()

CLASSIFICATION_PROMPT = """You are a Brazilian regulatory specialist with expertise in ANVISA supplement regulations (suplementos alimentares).

Analyze this ANVISA/DOU publication and extract structured information.

Publication data:
Title: {title}
Source: {source}
URL: {url}
Date: {pub_date}
Text: {text}

Return a JSON object with exactly this structure:
{{
  "is_relevant": true/false,
  "relevance_reason": "brief reason or null",
  "publication_type": "RDC" | "IN" | "DOU_notice" | "consultation" | "other" | null,
  "publication_number": "e.g. RDC 243 or IN 28" or null,
  "publication_year": 2024 or null,
  "summary_pt": "1-2 sentence Portuguese summary" or null,
  "summary_en": "1-2 sentence English summary" or null,
  "change_type": "addition" | "removal" | "dose_modification" | "reclassification" | "new_category" | "consultation" | "none",
  "affected_categories": ["vitamins", "minerals", "amino_acids", "proteins", "enzymes", "probiotics", "bioactives", "plant_extracts"] or [],
  "ingredients_added": [
    {{"name_pt": "...", "name_en": "...", "category": "...", "max_dose": "...", "dose_unit": "..."}}
  ],
  "ingredients_removed": [
    {{"name_pt": "...", "name_en": "...", "reason": "..."}}
  ],
  "ingredients_modified": [
    {{"name_pt": "...", "name_en": "...", "change_detail": "e.g. dose limit changed from X to Y"}}
  ],
  "amends_document": "e.g. IN 28/2018" or null,
  "effective_date": "ISO date string or null",
  "urgency": "high" | "medium" | "low",
  "urgency_reason": "brief explanation"
}}

Rules:
- is_relevant = true only if this directly affects suplementos alimentares ingredient permissions, dose limits, or positive lists
- urgency = high if ingredients are added/removed/dose-changed; medium if it's a consultation or minor update; low if administrative only
- Extract all ingredient names exactly as written in the Portuguese source
- If text is too short to determine relevance, set is_relevant to false
- Return ONLY valid JSON, no markdown fences, no explanation"""


def classify_publication(pub: dict) -> dict:
    """
    Send a publication to Claude for classification.
    Returns the publication dict enriched with classification fields.
    """
    text = pub.get("full_text") or pub.get("raw_text") or ""

    if len(text.strip()) < 20:
        logger.debug(f"Skipping classification — insufficient text: {pub.get('title', '')[:60]}")
        return {**pub, "classification": {"is_relevant": False, "relevance_reason": "insufficient_text"}}

    prompt = CLASSIFICATION_PROMPT.format(
        title=pub.get("title", "")[:300],
        source=pub.get("source", ""),
        url=pub.get("url", ""),
        pub_date=pub.get("pub_date", "unknown"),
        text=text[:3500],
    )

    try:
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1500,
            messages=[{"role": "user", "content": prompt}],
        )

        raw = message.content[0].text.strip()

        # Strip markdown fences if Claude added them anyway
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        classification = json.loads(raw)
        logger.info(
            f"Classified: '{pub.get('title', '')[:60]}' → "
            f"relevant={classification.get('is_relevant')}, "
            f"urgency={classification.get('urgency')}, "
            f"type={classification.get('change_type')}"
        )
        return {**pub, "classification": classification}

    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error for '{pub.get('title', '')[:60]}': {e}")
        return {**pub, "classification": {"is_relevant": False, "relevance_reason": "parse_error"}}
    except anthropic.APIError as e:
        logger.error(f"Anthropic API error: {e}")
        return {**pub, "classification": {"is_relevant": False, "relevance_reason": "api_error"}}


def classify_batch(publications: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Classify all publications.
    Returns (relevant, irrelevant) tuple.
    """
    if not publications:
        return [], []

    logger.info(f"Classifying {len(publications)} publications with Claude...")

    classified = []
    for i, pub in enumerate(publications):
        logger.debug(f"Classifying {i+1}/{len(publications)}: {pub.get('title', '')[:60]}")
        result = classify_publication(pub)
        classified.append(result)

    relevant = [p for p in classified if p.get("classification", {}).get("is_relevant")]
    irrelevant = [p for p in classified if not p.get("classification", {}).get("is_relevant")]

    logger.info(f"Classification complete: {len(relevant)} relevant, {len(irrelevant)} irrelevant")
    return relevant, irrelevant


def generate_run_summary(relevant: list[dict], irrelevant: list[dict], dry_run: bool) -> str:
    """
    Ask Claude to write a concise English summary of this month's findings.
    Used in the email notification body.
    """
    if not relevant:
        return "No relevant ANVISA publications found this month affecting supplement ingredient positive lists."

    items_text = "\n\n".join([
        f"Title: {p.get('title', 'N/A')}\n"
        f"Date: {p.get('pub_date', 'N/A')}\n"
        f"Type: {p.get('classification', {}).get('publication_type', 'N/A')}\n"
        f"Change: {p.get('classification', {}).get('change_type', 'N/A')}\n"
        f"Summary: {p.get('classification', {}).get('summary_en', 'N/A')}\n"
        f"Urgency: {p.get('classification', {}).get('urgency', 'N/A')}\n"
        f"URL: {p.get('url', 'N/A')}"
        for p in relevant[:10]
    ])

    prompt = f"""Summarise these ANVISA regulatory findings for a supplement manufacturer.
Be direct and technical. Highlight what has actually changed and what action is needed.
Max 250 words. Write in English.

{'DRY RUN — no database changes were made.' if dry_run else 'Database has been updated.'}

Findings:
{items_text}"""

    try:
        message = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=400,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text.strip()
    except Exception as e:
        logger.error(f"Summary generation failed: {e}")
        return f"Found {len(relevant)} relevant ANVISA publications. Check logs for details."
