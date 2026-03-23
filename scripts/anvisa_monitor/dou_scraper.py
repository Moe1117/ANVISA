"""
Scrapes the Diário Oficial da União (DOU) for ANVISA publications.

DOU is the authoritative source — legislation is legally in force from
the date of DOU publication, not the ANVISA portal date.

Uses the official DOU search portal at in.gov.br with keyword filters.
Falls back to RSS feeds if the search API is unavailable.
"""

import logging
import time
from datetime import datetime
from typing import Optional
from urllib.parse import urlencode

import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser
from tenacity import retry, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

DOU_BASE = "https://www.in.gov.br"
DOU_SEARCH_URL = "https://www.in.gov.br/consulta/-/busca/dou"

# ANVISA issues supplements legislation in Seção 1 (normative acts)
DOU_SECTIONS = ["1"]  # Seção 1 = normative/legislative acts

# Search terms to query in DOU
DOU_QUERIES = [
    "ANVISA suplemento alimentar",
    "ANVISA lista positiva ingredientes",
    "ANVISA instrução normativa suplemento",
    "ANVISA RDC suplemento alimentar",
    "ANVISA substâncias bioativas",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; ANVISA-RegMonitor/1.0; "
        "regulatory-compliance-bot)"
    ),
    "Accept-Language": "pt-BR,pt;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.in.gov.br/consulta",
}


@retry(stop=stop_after_attempt(3), wait=wait_exponential(multiplier=2, min=4, max=30))
def fetch_dou_search(query: str, start_date: str, end_date: str) -> Optional[BeautifulSoup]:
    """
    Query the DOU search portal.
    Dates in DD/MM/YYYY format.
    """
    params = {
        "q": query,
        "s": "todos",
        "exactDate": "personalizado",
        "startDate": start_date,
        "endDate": end_date,
        "orgaos": "anvisa",  # Filter to ANVISA publications only
    }

    url = f"{DOU_SEARCH_URL}?{urlencode(params)}"
    logger.debug(f"DOU search: {url}")

    try:
        resp = requests.get(url, headers=HEADERS, timeout=25)
        resp.raise_for_status()
        resp.encoding = "utf-8"
        return BeautifulSoup(resp.text, "lxml")
    except requests.RequestException as e:
        logger.warning(f"DOU search failed for '{query}': {e}")
        raise


def parse_dou_results(soup: BeautifulSoup, query: str) -> list[dict]:
    """Parse search results from DOU search page HTML."""
    results = []

    if not soup:
        return results

    # DOU search results are in div.result-item or similar containers
    # The structure varies; try multiple selectors
    result_containers = (
        soup.find_all("div", class_=lambda c: c and "result" in c.lower())
        or soup.find_all("article")
        or soup.find_all("li", class_=lambda c: c and "item" in c.lower() if c else False)
    )

    for container in result_containers:
        try:
            # Title
            title_el = container.find(["h3", "h4", "h2", "strong"])
            if not title_el:
                continue
            title = title_el.get_text(strip=True)
            if not title or len(title) < 5:
                continue

            # URL
            link_el = container.find("a", href=True)
            url = ""
            if link_el:
                href = link_el["href"]
                url = href if href.startswith("http") else f"{DOU_BASE}{href}"

            # Date
            date_el = container.find(
                string=lambda s: s and ("/" in s and len(s.strip()) <= 12)
            )
            pub_date = None
            if date_el:
                try:
                    pub_date = dateparser.parse(date_el.strip(), dayfirst=True)
                except Exception:
                    pass

            # Snippet
            snippet = container.get_text(separator=" ", strip=True)[:500]

            results.append({
                "source": "dou",
                "title": title,
                "url": url,
                "pub_date": pub_date.isoformat() if pub_date else None,
                "publication_number": None,  # Classifier will extract this
                "raw_text": snippet,
                "search_query": query,
            })

        except Exception as e:
            logger.debug(f"Error parsing DOU result: {e}")

    return results


def fetch_dou_rss_fallback(since: datetime) -> list[dict]:
    """
    Fallback: ANVISA publishes an RSS feed of recent acts.
    Use this if the search portal is unavailable.
    """
    rss_urls = [
        "https://www.gov.br/anvisa/pt-br/assuntos/noticias-anvisa/RSS",
        "https://www.gov.br/anvisa/pt-br/RSS",
    ]

    results = []
    for rss_url in rss_urls:
        try:
            resp = requests.get(rss_url, headers=HEADERS, timeout=15)
            if resp.status_code != 200:
                continue

            soup = BeautifulSoup(resp.text, "xml")
            for item in soup.find_all("item"):
                title = item.find("title")
                link = item.find("link")
                pub_date_el = item.find("pubDate")
                description = item.find("description")

                if not title:
                    continue

                title_text = title.get_text(strip=True)
                pub_date = None
                if pub_date_el:
                    try:
                        pub_date = dateparser.parse(pub_date_el.get_text(strip=True))
                    except Exception:
                        pass

                if pub_date and pub_date < since:
                    continue

                results.append({
                    "source": "dou_rss",
                    "title": title_text,
                    "url": link.get_text(strip=True) if link else "",
                    "pub_date": pub_date.isoformat() if pub_date else None,
                    "publication_number": None,
                    "raw_text": description.get_text(strip=True)[:500] if description else title_text,
                    "search_query": "rss_fallback",
                })

            logger.info(f"DOU RSS fallback: {len(results)} items from {rss_url}")
            break  # Stop at first working RSS

        except Exception as e:
            logger.warning(f"RSS fallback failed for {rss_url}: {e}")

    return results


def run_dou_scraper(since: datetime) -> list[dict]:
    """
    Main entry point for DOU scraping.
    Queries multiple search terms, deduplicates, returns results.
    """
    start_date = since.strftime("%d/%m/%Y")
    end_date = datetime.now().strftime("%d/%m/%Y")

    all_results = []
    search_failed = True

    for query in DOU_QUERIES:
        try:
            soup = fetch_dou_search(query, start_date, end_date)
            parsed = parse_dou_results(soup, query)
            all_results.extend(parsed)
            search_failed = False
            logger.info(f"DOU query '{query}': {len(parsed)} results")
            time.sleep(3)  # Respectful crawling
        except Exception as e:
            logger.warning(f"DOU search query '{query}' failed: {e}")
            time.sleep(5)

    # If all search queries failed, try RSS fallback
    if search_failed:
        logger.warning("All DOU search queries failed — attempting RSS fallback")
        try:
            rss_results = fetch_dou_rss_fallback(since)
            all_results.extend(rss_results)
        except Exception as e:
            logger.error(f"DOU RSS fallback also failed: {e}")

    # Deduplicate by URL, fallback to title
    seen = set()
    deduped = []
    for item in all_results:
        key = item.get("url") or item.get("title", "")
        if key and key not in seen:
            seen.add(key)
            deduped.append(item)

    logger.info(f"DOU scraper total: {len(deduped)} unique items")
    return deduped
