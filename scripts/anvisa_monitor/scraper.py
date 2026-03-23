"""
Scrapes ANVISA's official portal for new publications related to
suplementos alimentares, RDCs, and instrução normativas.

Primary targets:
  - gov.br/anvisa suplementos alimentares section
  - ANVISA legislation search for RDC/IN with suplemento keywords
"""

import logging
import re
import time
from datetime import datetime, timedelta
from typing import Optional

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

ANVISA_BASE = "https://www.gov.br"

# Key ANVISA pages to monitor
MONITOR_URLS = [
    # Suplementos alimentares landing
    "https://www.gov.br/anvisa/pt-br/assuntos/alimentos/suplementos-alimentares",
    # ANVISA news (catches new RDC/IN announcements)
    "https://www.gov.br/anvisa/pt-br/assuntos/noticias-anvisa",
    # Legislation portal — filter by alimentos
    "https://www.gov.br/anvisa/pt-br/assuntos/legislacao/legislacao-anvisa",
]

# Keywords that indicate relevance to supplement ingredients
RELEVANCE_KEYWORDS = [
    "suplemento alimentar",
    "suplementos alimentares",
    "lista positiva",
    "instrução normativa",
    "ingredientes autorizados",
    "substâncias bioativas",
    "proteínas",
    "aminoácidos",
    "vitaminas",
    "minerais",
    "probióticos",
    "enzimas",
    "extratos vegetais",
    "RDC 243",
    "RDC 240",
    "IN 28",
    "RDC 786",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; ANVISA-RegMonitor/1.0; "
        "regulatory-compliance-bot; +https://github.com/your-org/anvisa-monitor)"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=4, max=30))
def fetch_page(url: str, timeout: int = 20) -> Optional[BeautifulSoup]:
    """Fetch a page with retry logic. Returns BeautifulSoup or None."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        return BeautifulSoup(resp.text, "lxml")
    except requests.RequestException as e:
        logger.warning(f"Failed to fetch {url}: {e}")
        raise


def parse_date(text: str) -> Optional[datetime]:
    """Parse Brazilian date formats (DD/MM/YYYY, DD de Mês de YYYY, etc.)."""
    if not text:
        return None

    # Normalise Portuguese month names
    pt_months = {
        "janeiro": "january", "fevereiro": "february", "março": "march",
        "abril": "april", "maio": "may", "junho": "june",
        "julho": "july", "agosto": "august", "setembro": "september",
        "outubro": "october", "novembro": "november", "dezembro": "december",
    }
    normalised = text.lower().strip()
    for pt, en in pt_months.items():
        normalised = normalised.replace(pt, en)

    try:
        return dateparser.parse(normalised, dayfirst=True)
    except Exception:
        return None


def is_relevant(text: str) -> bool:
    """Check if text contains any ANVISA supplement-related keywords."""
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in RELEVANCE_KEYWORDS)


def extract_publication_number(text: str) -> Optional[str]:
    """Extract RDC/IN number from title text."""
    patterns = [
        r"RDC\s*n[°º.]?\s*(\d+)",
        r"IN\s*n[°º.]?\s*(\d+)",
        r"Instrução Normativa\s*n[°º.]?\s*(\d+)",
        r"Resolução.*?n[°º.]?\s*(\d+)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            prefix = "RDC" if "RDC" in pattern or "Resolu" in pattern else "IN"
            return f"{prefix} {match.group(1)}"
    return None


def scrape_anvisa_news(since: datetime) -> list[dict]:
    """Scrape ANVISA news page for recent relevant publications."""
    results = []
    url = "https://www.gov.br/anvisa/pt-br/assuntos/noticias-anvisa"

    logger.info(f"Scraping ANVISA news: {url}")
    soup = fetch_page(url)
    if not soup:
        return results

    # ANVISA uses Plone CMS — news items are in listing tiles
    items = soup.find_all("article", class_=re.compile(r"tileItem|newsItem|listing"))
    if not items:
        # Fallback: find all links with dates nearby
        items = soup.find_all("div", class_=re.compile(r"item|tile|news"))

    for item in items:
        try:
            # Extract title
            title_el = item.find(["h2", "h3", "h4", "a"])
            if not title_el:
                continue
            title = title_el.get_text(strip=True)

            if not is_relevant(title):
                continue

            # Extract date
            date_el = item.find(class_=re.compile(r"date|data|published"))
            if not date_el:
                date_el = item.find("span", string=re.compile(r"\d{2}/\d{2}/\d{4}"))
            pub_date = parse_date(date_el.get_text() if date_el else "")

            if pub_date and pub_date < since:
                continue  # Skip if older than lookback window

            # Extract URL
            link_el = item.find("a", href=True)
            full_url = ""
            if link_el:
                href = link_el["href"]
                full_url = href if href.startswith("http") else f"{ANVISA_BASE}{href}"

            results.append({
                "source": "anvisa_news",
                "title": title,
                "url": full_url,
                "pub_date": pub_date.isoformat() if pub_date else None,
                "publication_number": extract_publication_number(title),
                "raw_text": title,
            })

        except Exception as e:
            logger.debug(f"Error parsing news item: {e}")

    logger.info(f"ANVISA news: found {len(results)} relevant items")
    return results


def scrape_anvisa_suplementos_page() -> list[dict]:
    """
    Scrape the suplementos alimentares main page for linked legislation.
    This catches the 'legislação' links that reference the positive lists.
    """
    results = []
    url = "https://www.gov.br/anvisa/pt-br/assuntos/alimentos/suplementos-alimentares"

    logger.info(f"Scraping ANVISA suplementos page: {url}")
    soup = fetch_page(url)
    if not soup:
        return results

    # Find all links to legislation docs
    for link in soup.find_all("a", href=True):
        text = link.get_text(strip=True)
        href = link["href"]

        if not text or len(text) < 5:
            continue

        if not is_relevant(text) and not extract_publication_number(text):
            continue

        full_url = href if href.startswith("http") else f"{ANVISA_BASE}{href}"

        results.append({
            "source": "anvisa_suplementos_page",
            "title": text,
            "url": full_url,
            "pub_date": None,  # No date on these static links
            "publication_number": extract_publication_number(text),
            "raw_text": text,
        })

    logger.info(f"ANVISA suplementos page: found {len(results)} relevant links")
    return results


def fetch_publication_detail(url: str) -> str:
    """
    Fetch the full text of a publication page.
    Returns first 4000 chars — enough for Claude to classify.
    """
    if not url:
        return ""
    try:
        soup = fetch_page(url)
        if not soup:
            return ""

        # Remove nav/footer noise
        for tag in soup.find_all(["nav", "footer", "header", "script", "style"]):
            tag.decompose()

        body = soup.find("main") or soup.find("article") or soup.find("body")
        if body:
            text = body.get_text(separator="\n", strip=True)
            return text[:4000]
    except Exception as e:
        logger.debug(f"Could not fetch detail for {url}: {e}")
    return ""


def run_anvisa_scraper(since: datetime) -> list[dict]:
    """
    Main entry point. Returns deduplicated list of relevant ANVISA publications
    found since the given datetime.
    """
    all_results = []

    # Source 1: News feed
    try:
        news = scrape_anvisa_news(since)
        all_results.extend(news)
        time.sleep(2)  # Polite delay
    except Exception as e:
        logger.error(f"ANVISA news scraper failed: {e}")

    # Source 2: Suplementos alimentares static page
    try:
        suplementos = scrape_anvisa_suplementos_page()
        all_results.extend(suplementos)
        time.sleep(2)
    except Exception as e:
        logger.error(f"ANVISA suplementos scraper failed: {e}")

    # Deduplicate by URL
    seen_urls = set()
    deduped = []
    for item in all_results:
        key = item["url"] or item["title"]
        if key not in seen_urls:
            seen_urls.add(key)
            deduped.append(item)

    # Enrich with full text for items that have a URL and no date filter applied
    for item in deduped:
        if item["url"] and not item.get("full_text"):
            item["full_text"] = fetch_publication_detail(item["url"])
            time.sleep(1)

    logger.info(f"ANVISA scraper total: {len(deduped)} unique items")
    return deduped
