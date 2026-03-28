#!/usr/bin/env python3
"""
FDA Regulatory Monitoring Dashboard Scraper

Fetches articles from FDA RSS feeds and web sources, deduplicates by URL,
merges with existing articles, and regenerates the HTML dashboard.

Runs via GitHub Actions every 30 minutes.
Central timezone for timestamps.
"""

import json
import os
import sys
import hashlib
import re
from datetime import datetime
from typing import Dict, List, Optional, Set
from pathlib import Path
import logging

try:
    import feedparser
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    print("ERROR: Required packages not installed.")
    print("Install with: pip install feedparser requests beautifulsoup4")
    sys.exit(1)

# Configuration
RSS_FEEDS = [
    "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/drugs/rss.xml",
    "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml",
    "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/medwatch-safety-alerts/rss.xml",
]

SCRAPE_URLS = [
    ("https://www.beckershospitalreview.com/pharmacy", "Becker's Hospital Review"),
    ("https://www.fiercepharma.com/pharma", "Fierce Pharma"),
    ("https://www.statnews.com/pharmalot/", "STAT News"),
    ("https://www.thefdalawblog.com/", "FDA Law Blog (HP&M)"),
    ("https://a4pc.org/news", "Alliance for Pharmacy Compounding"),
    ("https://natlawreview.com/topic/health-care-life-sciences", "National Law Review"),
]

KEYWORDS = [
    "compounding", "compounded", "503a", "503b", "outsourcing facility",
    "peptide", "bpc-157", "thymosin", "category 1", "category 2",
    "glp-1", "glp1", "semaglutide", "tirzepatide", "ozempic", "wegovy", "zepbound",
    "orforglipron", "telehealth", "telemedicine", "hims", "novo nordisk", "eli lilly",
    "bulk drug substance", "essentially a copy", "warning letter",
]

PRIMARY_SOURCES = {"FDA.gov", "Federal Register", "Congress.gov", "HHS.gov"}

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

TIMEOUT = 15
DOCS_DIR = Path("docs")
ARTICLES_FILE = DOCS_DIR / "articles.json"
HTML_FILE = DOCS_DIR / "index.html"

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def load_existing_articles() -> Dict[str, Dict]:
    """Load existing articles from JSON file."""
    if ARTICLES_FILE.exists():
        try:
            with open(ARTICLES_FILE, "r") as f:
                articles = json.load(f)
                logger.info(f"Loaded {len(articles)} existing articles")
                return articles
        except Exception as e:
            logger.error(f"Error loading articles: {e}")
            return {}
    logger.info("No existing articles file found")
    return {}


def save_articles(articles: Dict[str, Dict]) -> None:
    """Save articles to JSON file."""
    DOCS_DIR.mkdir(parents=True, exist_ok=True)
    try:
        with open(ARTICLES_FILE, "w") as f:
            json.dump(articles, f, indent=2)
        logger.info(f"Saved {len(articles)} articles to {ARTICLES_FILE}")
    except Exception as e:
        logger.error(f"Error saving articles: {e}")


def get_article_id(url: str) -> str:
    """Generate a unique ID for an article based on its URL."""
    return "art-" + hashlib.md5(url.encode()).hexdigest()[:12]


def categorize_article(title: str, summary: str) -> str:
    """Categorize article based on keywords in title and summary."""
    text = f"{title} {summary}".lower()

    if any(word in text for word in ["enforcement", "warning letter", "seizure", "injunction", "fda action"]):
        return "Enforcement"
    if any(word in text for word in ["glp-1", "glp1", "semaglutide", "tirzepatide", "ozempic", "wegovy", "zepbound"]):
        return "GLP-1"
    if any(word in text for word in ["peptide", "bpc-157", "thymosin"]):
        return "Peptides"
    if any(word in text for word in ["guidance", "guidance document", "fda guidance"]):
        return "Guidance"
    if any(word in text for word in ["compounding", "503a", "503b", "outsourcing facility"]):
        return "Compounding"
    if any(word in text for word in ["telehealth", "telemedicine", "hims"]):
        return "Telehealth"
    if any(word in text for word in ["advertising", "marketing", "claims"]):
        return "Advertising"
    if any(word in text for word in ["supply", "shortage", "api", "bulk drug"]):
        return "Drug Supply"

    return "General"


def fetch_rss_articles() -> List[Dict]:
    """Fetch articles from FDA RSS feeds."""
    articles = []

    for feed_url in RSS_FEEDS:
        try:
            logger.info(f"Fetching RSS feed: {feed_url}")
            feed = feedparser.parse(feed_url)

            if feed.bozo:
                logger.warning(f"Feed parsing warning for {feed_url}: {feed.bozo_exception}")

            for entry in feed.entries[:20]:
                try:
                    title = entry.get("title", "").strip()
                    url = entry.get("link", "").strip()
                    summary = entry.get("summary", "").strip()

                    if not url or not title:
                        continue

                    text_to_check = f"{title} {summary}".lower()
                    if not any(keyword in text_to_check for keyword in KEYWORDS):
                        continue

                    pub_date = entry.get("published", "")
                    date_obj = parse_date(pub_date)

                    if date_obj is None:
                        logger.info(f"Skipping RSS article without parseable date: {title[:60]}")
                        continue

                    article = {
                        "title": title,
                        "url": url,
                        "summary": summary[:500],
                        "date": date_obj.strftime("%Y-%m-%d"),
                        "source": "FDA.gov",
                        "category": categorize_article(title, summary),
                        "jurisdiction": "federal",
                        "date_display": date_obj.strftime("%b %d, %Y"),
                        "tier": "primary",
                    }
                    articles.append(article)
                    logger.info(f"Added RSS article: {title[:60]}")

                except Exception as e:
                    logger.warning(f"Error processing RSS entry: {e}")
                    continue

        except Exception as e:
            logger.error(f"Error fetching RSS feed {feed_url}: {e}")
            continue

    return articles


def is_junk_link(title: str, href: str) -> bool:
    """Filter out navigation pages, promotional content, and non-article links."""
    title_lower = title.lower()
    junk_patterns = [
        "find a compounding pharmacy", "compounders on capitol hill",
        "educon", "owner summit", "federal advocacy", "state level resources",
        "comppac", "position statements", "ethical compounding",
        "foundations course", "best practices", "registered outsourcing facilities",
        "bulk drug substances used in compounding", "subscribe", "sign up",
        "contact us", "about us", "privacy policy", "terms of use",
        "advertise", "newsletter", "login", "sign in", "careers",
    ]
    if any(p in title_lower for p in junk_patterns):
        return True
    # Reject very short titles that are likely nav links
    if len(title.split()) < 4:
        return True
    # Reject titles that run together with descriptions (scraper artifact)
    if len(title) > 200:
        return True
    return False


def extract_date_from_article_page(article_url: str) -> Optional[datetime]:
    """Fetch an article page and try to extract the real publication date."""
    try:
        headers = {"User-Agent": USER_AGENT}
        response = requests.get(article_url, headers=headers, timeout=TIMEOUT)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, "html.parser")

        # Strategy 1: Look for common meta tags with dates
        date_meta_names = [
            "article:published_time", "og:article:published_time",
            "datePublished", "date", "DC.date.issued", "sailthru.date",
            "publication_date", "parsely-pub-date",
        ]
        for name in date_meta_names:
            tag = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
            if tag and tag.get("content"):
                parsed = parse_date(tag["content"].strip()[:25])
                if parsed and parsed.year >= 2020:
                    return parsed

        # Strategy 2: Look for <time> elements with datetime attribute
        time_tags = soup.find_all("time", attrs={"datetime": True})
        for tt in time_tags[:3]:
            parsed = parse_date(tt["datetime"].strip()[:25])
            if parsed and parsed.year >= 2020:
                return parsed

        # Strategy 3: Look for JSON-LD structured data
        for script in soup.find_all("script", type="application/ld+json"):
            try:
                import json as _json
                ld = _json.loads(script.string or "")
                if isinstance(ld, list):
                    ld = ld[0] if ld else {}
                pub = ld.get("datePublished") or ld.get("dateCreated")
                if pub:
                    parsed = parse_date(pub.strip()[:25])
                    if parsed and parsed.year >= 2020:
                        return parsed
            except Exception:
                pass

    except Exception as e:
        logger.debug(f"Could not fetch article page for date extraction: {e}")

    return None


def fetch_web_articles() -> List[Dict]:
    """Scrape articles from web sources, extracting real publication dates."""
    articles = []

    for url, source_name in SCRAPE_URLS:
        try:
            logger.info(f"Scraping {source_name}: {url}")
            headers = {"User-Agent": USER_AGENT}
            response = requests.get(url, headers=headers, timeout=TIMEOUT)
            response.raise_for_status()

            soup = BeautifulSoup(response.content, "html.parser")

            # Build base URL for resolving relative links
            from urllib.parse import urljoin
            base_url = url

            links = soup.find_all("a", limit=50)

            for link in links:
                try:
                    title = link.get_text(strip=True)
                    href = link.get("href", "").strip()

                    if not href or not title or len(title) < 10:
                        continue

                    # Resolve relative URLs properly
                    if not href.startswith(("http://", "https://")):
                        href = urljoin(base_url, href)

                    text_to_check = f"{title}".lower()
                    if not any(keyword in text_to_check for keyword in KEYWORDS):
                        continue

                    # Filter out junk/navigation links
                    if is_junk_link(title, href):
                        logger.debug(f"Skipping junk link: {title[:50]}")
                        continue

                    # Try to extract real publication date from the article page
                    date_obj = extract_date_from_article_page(href)
                    if date_obj is None:
                        # Check for date in surrounding HTML (sibling/parent elements)
                        parent = link.parent
                        if parent:
                            time_tag = parent.find("time", attrs={"datetime": True})
                            if time_tag:
                                date_obj = parse_date(time_tag["datetime"].strip()[:25])
                        # If still no date, check for date-like text near the link
                        if date_obj is None:
                            logger.info(f"Skipping article without extractable date: {title[:60]}")
                            continue

                    article = {
                        "title": title[:200],
                        "url": href,
                        "summary": "",
                        "date": date_obj.strftime("%Y-%m-%d"),
                        "source": source_name,
                        "category": categorize_article(title, ""),
                        "jurisdiction": "federal",
                        "date_display": date_obj.strftime("%b %d, %Y"),
                        "tier": "supplementary",
                    }
                    articles.append(article)
                    logger.info(f"Added web article from {source_name}: {title[:60]} (dated {article['date']})")

                except Exception as e:
                    logger.warning(f"Error processing link from {source_name}: {e}")
                    continue

        except Exception as e:
            logger.error(f"Error scraping {source_name}: {e}")
            continue

    return articles


def parse_date(date_string: str) -> Optional[datetime]:
    """Parse date string into datetime object. Returns None if unparseable."""
    if not date_string:
        return None

    # Strip timezone offset suffix for formats that don't handle %z well
    cleaned = date_string.strip()

    formats = [
        "%a, %d %b %Y %H:%M:%S %z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d",
        "%B %d, %Y",
        "%b %d, %Y",
        "%d %B %Y",
        "%d %b %Y",
        "%m/%d/%Y",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(cleaned, fmt)
        except ValueError:
            continue

    # Try stripping trailing timezone info and re-parsing
    import re as _re
    stripped = _re.sub(r'[+-]\d{2}:\d{2}$', '', cleaned).strip()
    if stripped != cleaned:
        for fmt in ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"]:
            try:
                return datetime.strptime(stripped, fmt)
            except ValueError:
                continue

    return None


def merge_articles(existing: Dict[str, Dict], new_articles: List[Dict]) -> Dict[str, Dict]:
    """Merge new articles with existing ones, deduplicating by URL."""
    result = existing.copy()
    urls_seen = {article["url"] for article in result.values()}

    for article in new_articles:
        url = article.get("url")

        if not url:
            continue

        if url in urls_seen:
            logger.debug(f"Skipping duplicate: {url}")
            continue

        article_id = get_article_id(url)
        article["id"] = article_id
        result[article_id] = article
        urls_seen.add(url)
        logger.info(f"Added new article: {article.get('title', 'Unknown')[:60]}")

    return result


def generate_html(articles: Dict[str, Dict]) -> str:
    """Generate the HTML dashboard with embedded articles JSON."""
    articles_list = list(articles.values())
    articles_json = json.dumps(articles_list, indent=12)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>FDA Regulatory Monitor</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}

        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Helvetica Neue", sans-serif;
            background-color: #f8f9fa;
            color: #333;
            line-height: 1.6;
        }}

        header {{
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            color: white;
            padding: 2rem;
            box-shadow: 0 2px 8px rgba(0, 0, 0, 0.1);
            position: sticky;
            top: 0;
            z-index: 100;
        }}

        .header-content {{
            max-width: 1400px;
            margin: 0 auto;
        }}

        .header-top {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 1.5rem;
        }}

        .header-title {{
            display: flex;
            flex-direction: column;
        }}

        .header-title h1 {{
            font-family: "Georgia", serif;
            font-size: 2rem;
            margin-bottom: 0.25rem;
            font-weight: 600;
        }}

        .header-title p {{
            font-size: 0.95rem;
            opacity: 0.9;
        }}

        .header-meta {{
            display: flex;
            flex-direction: column;
            align-items: flex-end;
        }}

        .meta-label {{
            color: #C9A227;
            font-weight: 600;
            margin-bottom: 0.25rem;
            font-size: 0.85rem;
        }}

        .meta-value {{
            color: #fff;
            font-size: 0.95rem;
        }}

        .stats-bar {{
            background-color: #fff;
            padding: 1.5rem 2rem;
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 2rem;
            border-bottom: 1px solid #ddd;
            margin-bottom: 1.5rem;
        }}

        .stat-item {{
            text-align: center;
        }}

        .stat-number {{
            font-family: "Georgia", serif;
            font-size: 2.5rem;
            font-weight: bold;
            color: #C9A227;
            margin-bottom: 0.5rem;
        }}

        .stat-label {{
            font-size: 0.9rem;
            color: #666;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}

        .stat-link {{
            text-decoration: none;
            cursor: pointer;
            border-radius: 6px;
            padding: 1.25rem;
            transition: background-color 0.2s, transform 0.15s;
            display: block;
        }}

        .stat-link:hover {{
            background-color: #f0f0f0;
            transform: translateY(-2px);
        }}

        .stats-bar .stat-item {{
            margin: 0;
        }}

        .jurisdiction-toggle {{
            max-width: 1400px;
            margin: 0 auto;
            padding: 0 2rem 1.5rem;
            display: flex;
            align-items: center;
            gap: 1rem;
            background-color: #fff;
            border-bottom: 1px solid #ddd;
        }}

        .jurisdiction-label {{
            font-weight: 600;
            color: #333;
            font-size: 0.95rem;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}

        .toggle-buttons {{
            display: flex;
            gap: 0.5rem;
            background-color: #f0f0f0;
            padding: 0.35rem;
            border-radius: 20px;
        }}

        .toggle-btn {{
            padding: 0.5rem 1rem;
            border: none;
            background-color: transparent;
            color: #666;
            cursor: pointer;
            border-radius: 16px;
            font-size: 0.9rem;
            font-weight: 500;
            transition: all 0.2s;
            white-space: nowrap;
        }}

        .toggle-btn.active {{
            background-color: #C9A227;
            color: #1a1a2e;
            font-weight: 600;
        }}

        .toggle-btn:hover {{
            background-color: rgba(201, 162, 39, 0.1);
        }}

        .toggle-btn.active:hover {{
            background-color: #C9A227;
        }}

        .tabs {{
            max-width: 1400px;
            margin: 0 auto;
            display: flex;
            gap: 1rem;
            border-bottom: 2px solid #ddd;
            padding: 0 2rem;
            background-color: #fff;
        }}

        .tab-button {{
            padding: 1rem 1.5rem;
            border: none;
            background-color: transparent;
            cursor: pointer;
            font-size: 1rem;
            color: #666;
            font-weight: 500;
            border-bottom: 3px solid transparent;
            transition: all 0.3s;
            position: relative;
            bottom: -2px;
        }}

        .tab-button:hover {{
            color: #333;
        }}

        .tab-button.active {{
            color: #1a1a2e;
            border-bottom-color: #C9A227;
        }}

        .tab-container {{
            background-color: #fff;
        }}

        .tab-content {{
            display: none;
            max-width: 1400px;
            margin: 0 auto;
            padding: 2rem;
        }}

        .tab-content.active {{
            display: block;
        }}

        .container {{
            max-width: 1400px;
            margin: 0 auto;
        }}

        .filter-section {{
            background-color: #f8f9fa;
            padding: 1.5rem;
            border-radius: 8px;
            margin-bottom: 2rem;
        }}

        .filter-buttons {{
            display: flex;
            flex-wrap: wrap;
            gap: 0.75rem;
            margin-bottom: 1rem;
        }}

        .filter-btn {{
            padding: 0.65rem 1.25rem;
            border: 1px solid #ddd;
            background-color: white;
            border-radius: 20px;
            cursor: pointer;
            font-size: 0.9rem;
            font-weight: 500;
            color: #666;
            transition: all 0.2s;
        }}

        .filter-btn:hover {{
            border-color: #C9A227;
            color: #C9A227;
        }}

        .filter-btn.active {{
            background-color: #C9A227;
            color: white;
            border-color: #C9A227;
        }}

        .search-box {{
            position: relative;
        }}

        #search-input {{
            width: 100%;
            padding: 0.85rem 1rem;
            border: 1px solid #ddd;
            border-radius: 6px;
            font-size: 0.95rem;
            transition: border-color 0.2s;
        }}

        #search-input:focus {{
            outline: none;
            border-color: #C9A227;
            box-shadow: 0 0 0 3px rgba(201, 162, 39, 0.1);
        }}

        .key-dev-section {{
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            padding: 2rem;
            border-radius: 8px;
            margin-bottom: 2rem;
        }}

        .key-dev-title {{
            color: white;
            font-family: "Georgia", serif;
            font-size: 1.5rem;
            margin-bottom: 1.5rem;
        }}

        .key-dev-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            gap: 1.5rem;
        }}

        .key-dev-item {{
            background-color: rgba(255, 255, 255, 0.05);
            padding: 1.25rem;
            border-radius: 8px;
            border-left: 3px solid #C9A227;
        }}

        .key-dev-date {{
            color: #C9A227;
            font-weight: 600;
            font-size: 0.85rem;
            text-transform: uppercase;
            margin-bottom: 0.5rem;
            letter-spacing: 0.5px;
        }}

        .key-dev-title-item {{
            color: white;
            font-weight: 600;
            margin-bottom: 0.75rem;
            line-height: 1.4;
        }}

        .key-dev-source {{
            color: #C9A227;
            font-size: 0.85rem;
            font-weight: 500;
        }}

        .articles-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(340px, 1fr));
            gap: 1.5rem;
        }}

        .article-card {{
            background-color: #fff;
            border: 1px solid #ddd;
            border-radius: 8px;
            padding: 1.5rem;
            transition: all 0.3s;
            display: flex;
            flex-direction: column;
            border-left: 4px solid transparent;
        }}

        .article-card.federal-article {{
            border-left-color: transparent;
        }}

        .article-card.state-article {{
            border-left-color: #3a7ca5;
            background-color: #fafcfd;
        }}

        .article-card:hover {{
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
            transform: translateY(-2px);
        }}

        .article-card.federal-article:hover {{
            border-color: #C9A227;
            border-left-color: #C9A227;
        }}

        .article-card.state-article:hover {{
            border-color: #3a7ca5;
            border-left-color: #3a7ca5;
        }}

        .article-meta {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 1rem;
            gap: 1rem;
        }}

        .article-date {{
            font-size: 0.85rem;
            color: #999;
            font-weight: 500;
            flex-shrink: 0;
        }}

        .article-badges {{
            display: flex;
            gap: 0.5rem;
            flex-wrap: wrap;
            justify-content: flex-end;
        }}

        .source-badge {{
            display: inline-block;
            padding: 0.35rem 0.75rem;
            border-radius: 12px;
            font-size: 0.8rem;
            font-weight: 600;
            white-space: nowrap;
        }}

        .source-badge.primary {{
            background-color: #C9A227;
            color: #1a1a2e;
        }}

        .source-badge.secondary {{
            background-color: #e8e8e8;
            color: #666;
        }}

        .category-tag {{
            display: inline-block;
            padding: 0.35rem 0.75rem;
            background-color: #f0f0f0;
            color: #1a1a2e;
            border-radius: 12px;
            font-size: 0.8rem;
            font-weight: 600;
            white-space: nowrap;
        }}

        .jurisdiction-badge {{
            display: inline-block;
            padding: 0.35rem 0.75rem;
            background-color: #e1eef5;
            color: #2a6a8a;
            border-radius: 12px;
            font-size: 0.8rem;
            font-weight: 700;
            white-space: nowrap;
            margin-left: 0.5rem;
            letter-spacing: 0.3px;
        }}

        .article-title {{
            margin-bottom: 0.75rem;
            flex-grow: 1;
        }}

        .article-title a {{
            color: #1a1a2e;
            text-decoration: none;
            font-weight: 600;
            font-family: "Georgia", serif;
            font-size: 1.1rem;
            line-height: 1.4;
            transition: color 0.2s;
        }}

        .article-title a:hover {{
            color: #C9A227;
        }}

        .article-summary {{
            color: #666;
            font-size: 0.95rem;
            margin-bottom: 1rem;
            line-height: 1.5;
            flex-grow: 1;
        }}

        .article-link {{
            color: #C9A227;
            text-decoration: none;
            font-weight: 600;
            font-size: 0.9rem;
            transition: color 0.2s;
            align-self: flex-start;
        }}

        .article-link:hover {{
            color: #1a1a2e;
            text-decoration: underline;
        }}

        .no-results {{
            text-align: center;
            padding: 3rem;
            color: #999;
        }}

        .no-results h3 {{
            color: #666;
            margin-bottom: 0.5rem;
        }}

        .insights-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 1.5rem;
            margin-bottom: 2rem;
        }}

        .insight-card {{
            background-color: #fff;
            border: 1px solid #ddd;
            border-radius: 8px;
            padding: 1.5rem;
            transition: all 0.3s;
        }}

        .insight-card:hover {{
            box-shadow: 0 4px 12px rgba(0, 0, 0, 0.1);
            border-color: #C9A227;
        }}

        .insight-title {{
            font-family: "Georgia", serif;
            font-size: 1.25rem;
            color: #1a1a2e;
            margin-bottom: 1rem;
            font-weight: 600;
        }}

        .insight-text {{
            color: #666;
            font-size: 0.95rem;
            line-height: 1.6;
        }}

        .chart-container {{
            background-color: #fff;
            border: 1px solid #ddd;
            border-radius: 8px;
            padding: 2rem;
            margin-bottom: 2rem;
        }}

        .chart-title {{
            font-family: "Georgia", serif;
            font-size: 1.25rem;
            color: #1a1a2e;
            margin-bottom: 1.5rem;
            font-weight: 600;
        }}

        .bar-chart {{
            display: flex;
            align-items: flex-end;
            gap: 1rem;
            height: 250px;
        }}

        .bar-item {{
            display: flex;
            flex-direction: column;
            align-items: center;
            flex: 1;
        }}

        .bar {{
            width: 100%;
            background: linear-gradient(to top, #C9A227, #d4b03f);
            border-radius: 4px 4px 0 0;
            min-height: 20px;
            transition: all 0.3s;
            cursor: pointer;
        }}

        .bar:hover {{
            background: linear-gradient(to top, #a88917, #C9A227);
        }}

        .bar-value {{
            margin-top: 0.75rem;
            font-weight: 600;
            color: #1a1a2e;
            font-size: 1.1rem;
        }}

        .bar-label {{
            margin-top: 0.5rem;
            color: #666;
            font-size: 0.85rem;
            text-align: center;
            max-width: 80px;
            word-wrap: break-word;
        }}

        .timeline {{
            position: relative;
            padding: 2rem 0;
        }}

        .timeline-item {{
            display: flex;
            gap: 2rem;
            margin-bottom: 2rem;
            position: relative;
        }}

        .timeline-date {{
            font-weight: 700;
            color: #C9A227;
            min-width: 100px;
            font-size: 0.95rem;
        }}

        .timeline-content {{
            flex: 1;
        }}

        .timeline-content h4 {{
            color: #1a1a2e;
            margin-bottom: 0.5rem;
            font-family: "Georgia", serif;
        }}

        .timeline-content p {{
            color: #666;
            font-size: 0.95rem;
            line-height: 1.5;
        }}

        .timeline-marker {{
            position: absolute;
            left: 47px;
            top: 30px;
            width: 14px;
            height: 14px;
            background-color: #C9A227;
            border: 3px solid white;
            border-radius: 50%;
            box-shadow: 0 0 0 2px #C9A227;
        }}

        .risk-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 1.5rem;
        }}

        .risk-card {{
            background-color: #fff;
            border-left: 4px solid #C9A227;
            border-radius: 8px;
            padding: 1.5rem;
            border: 1px solid #ddd;
            border-left: 4px solid #C9A227;
        }}

        .risk-level {{
            display: inline-block;
            padding: 0.35rem 0.75rem;
            border-radius: 12px;
            font-size: 0.8rem;
            font-weight: 700;
            margin-bottom: 0.75rem;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}

        .risk-critical {{
            background-color: #fee;
            color: #c00;
        }}

        .risk-high {{
            background-color: #fef3cd;
            color: #856404;
        }}

        .risk-medium {{
            background-color: #d1ecf1;
            color: #0c5460;
        }}

        .risk-title {{
            font-family: "Georgia", serif;
            font-size: 1.15rem;
            color: #1a1a2e;
            margin: 0.75rem 0;
            font-weight: 600;
        }}

        .risk-description {{
            color: #666;
            font-size: 0.95rem;
            line-height: 1.5;
        }}

        footer {{
            background-color: #1a1a2e;
            color: white;
            padding: 2rem;
            text-align: center;
            margin-top: 3rem;
            font-size: 0.9rem;
        }}

        .footer-disclaimer {{
            max-width: 900px;
            margin: 0 auto;
            line-height: 1.6;
        }}

        @media (max-width: 1024px) {{
            .header-top {{
                flex-direction: column;
                align-items: flex-start;
                gap: 1rem;
            }}

            .header-meta {{
                align-items: flex-start;
            }}

            .stats-bar {{
                grid-template-columns: repeat(2, 1fr);
            }}

            .tabs {{
                overflow-x: auto;
            }}

            .articles-grid {{
                grid-template-columns: 1fr;
            }}

            .bar-chart {{
                gap: 0.5rem;
            }}
        }}

        @media (max-width: 768px) {{
            .header-title h1 {{
                font-size: 1.5rem;
            }}

            .stats-bar {{
                grid-template-columns: 1fr;
                gap: 1rem;
            }}

            .jurisdiction-toggle {{
                flex-direction: column;
                align-items: flex-start;
            }}

            .toggle-buttons {{
                width: 100%;
                justify-content: flex-start;
            }}

            .tab-content {{
                padding: 1rem;
            }}

            .filter-section {{
                padding: 1rem;
            }}

            .key-dev-grid {{
                grid-template-columns: 1fr;
            }}
        }}
    </style>
</head>
<body>
    <header>
        <div class="header-content">
            <div class="header-top">
                <div class="header-title">
                    <h1>FDA Regulatory Monitor</h1>
                    <p>Comprehensive tracking of federal and state compounding regulations</p>
                </div>
                <div class="header-meta">
                    <div class="meta-label">Last Updated</div>
                    <div class="meta-value" id="last-updated"></div>
                </div>
            </div>
        </div>
    </header>

    <div class="stats-bar">
        <div class="stat-item">
            <a href="#" class="stat-link" data-stat-filter="all">
                <div class="stat-number" id="total-articles">0</div>
                <div class="stat-label">Total Articles</div>
            </a>
        </div>
        <div class="stat-item">
            <a href="#" class="stat-link" data-stat-filter="primary-sources">
                <div class="stat-number" id="total-sources">0</div>
                <div class="stat-label">Unique Sources</div>
            </a>
        </div>
        <div class="stat-item">
            <a href="#" class="stat-link" data-stat-filter="Enforcement">
                <div class="stat-number" id="enforcement-actions">0</div>
                <div class="stat-label">Enforcement Actions</div>
            </a>
        </div>
        <div class="stat-item">
            <a href="#" class="stat-link" data-stat-filter="Guidance">
                <div class="stat-number" id="active-legislation">0</div>
                <div class="stat-label">Active Legislation</div>
            </a>
        </div>
    </div>

    <div class="jurisdiction-toggle">
        <div class="jurisdiction-label">Jurisdiction</div>
        <div class="toggle-buttons">
            <button class="toggle-btn active" data-jurisdiction="federal">Federal</button>
            <button class="toggle-btn" data-jurisdiction="state">State & Local</button>
            <button class="toggle-btn" data-jurisdiction="all">All Jurisdictions</button>
        </div>
    </div>

    <div class="tab-container">
        <div class="tabs">
            <button class="tab-button active" data-tab="feed">Regulatory Feed</button>
            <button class="tab-button" data-tab="insights">Insights & Trends</button>
            <button class="tab-button" data-tab="timeline">Enforcement Timeline</button>
            <button class="tab-button" data-tab="risks">Risk Assessment</button>
        </div>

        <div id="feed" class="tab-content active">
            <div class="filter-section">
                <div style="margin-bottom: 1rem;">
                    <label for="search-input" style="font-weight: 600; color: #333; display: block; margin-bottom: 0.5rem;">Search Articles</label>
                    <div class="search-box">
                        <input type="text" id="search-input" placeholder="Search by title, summary, or source...">
                    </div>
                </div>

                <div>
                    <label style="font-weight: 600; color: #333; display: block; margin-bottom: 0.75rem;">Filter by Category</label>
                    <div class="filter-buttons">
                        <button class="filter-btn active" data-filter="all">All Categories</button>
                        <button class="filter-btn" data-filter="Enforcement">Enforcement</button>
                        <button class="filter-btn" data-filter="GLP-1">GLP-1</button>
                        <button class="filter-btn" data-filter="Peptides">Peptides</button>
                        <button class="filter-btn" data-filter="Guidance">Guidance</button>
                        <button class="filter-btn" data-filter="Compounding">Compounding</button>
                        <button class="filter-btn" data-filter="Drug Supply">Drug Supply</button>
                        <button class="filter-btn" data-filter="Advertising">Advertising</button>
                        <button class="filter-btn" data-filter="Telehealth">Telehealth</button>
                        <button class="filter-btn" data-filter="General">General</button>
                    </div>
                </div>
            </div>
            <div id="articles-container" class="articles-grid"></div>
        </div>

        <div id="insights" class="tab-content">
            <div class="key-dev-section">
                <div class="key-dev-title">Key Developments</div>
                <div class="key-dev-grid" id="key-dev-container"></div>
            </div>

            <div class="insights-grid" id="insights-container"></div>
        </div>

        <div id="timeline" class="tab-content">
            <div class="timeline" id="timeline-container"></div>
        </div>

        <div id="risks" class="tab-content">
            <div class="risk-grid" id="risk-container"></div>
        </div>
    </div>

    <footer>
        <div class="footer-disclaimer">
            <p><strong>Disclaimer:</strong> This dashboard is for informational purposes only and does not constitute legal advice. It is updated regularly but may not reflect the most current regulatory developments. Users should consult legal counsel for compliance guidance.</p>
        </div>
    </footer>

    <script>
        const articles = {articles_json};

        function updateStats() {{
            let total = articles.length;
            let sources = new Set();
            let enforcement = 0;
            let guidance = 0;

            articles.forEach(article => {{
                sources.add(article.source);
                if (article.category === 'Enforcement') enforcement++;
                if (article.category === 'Guidance') guidance++;
            }});

            document.getElementById('total-articles').textContent = total;
            document.getElementById('total-sources').textContent = sources.size;
            document.getElementById('enforcement-actions').textContent = enforcement;
            document.getElementById('active-legislation').textContent = guidance;
        }}

        function updateTimestamp() {{
            const now = new Date();
            const formatted = now.toLocaleString('en-US', {{
                timeZone: 'America/Chicago',
                year: 'numeric',
                month: 'short',
                day: 'numeric',
                hour: '2-digit',
                minute: '2-digit',
                second: '2-digit'
            }});
            const parts = formatted.split(', ');
            const datePart = parts[0];
            const timePart = parts[1];
            document.getElementById('last-updated').textContent = `${{datePart}} at ${{timePart}} CT`;
        }}
        updateTimestamp();

        updateStats();

        const keyDevs = [
            articles.find(a => a.source === 'FDA.gov'),
            articles[0],
            articles[1]
        ].filter(Boolean);

        function renderKeyDevelopments() {{
            const container = document.getElementById('key-dev-container');
            container.innerHTML = keyDevs.map(article => `
                <div class="key-dev-item">
                    <div class="key-dev-date">${{article.date_display}}</div>
                    <div class="key-dev-title-item">${{article.title}}</div>
                    <div class="key-dev-source">${{article.source}}</div>
                </div>
            `).join('');
        }}
        renderKeyDevelopments();

        function isPrimarySource(source) {{
            return ['FDA.gov', 'Federal Register', 'Congress.gov', 'HHS.gov'].includes(source);
        }}

        let activeJurisdiction = 'federal';
        let activeFilter = 'all';
        let searchTerm = '';

        function renderArticles(articlesToRender) {{
            const container = document.getElementById('articles-container');
            if (articlesToRender.length === 0) {{
                container.innerHTML = '<div class="no-results"><h3>No articles found</h3><p>Try adjusting your filters or search terms.</p></div>';
                return;
            }}
            container.innerHTML = articlesToRender.map(article => {{
                const jurisdictionClass = article.jurisdiction === 'state' ? 'state-article' : 'federal-article';
                const sourceClass = isPrimarySource(article.source) ? 'primary' : 'secondary';
                return `
                    <div class="article-card ${{jurisdictionClass}}">
                        <div class="article-meta">
                            <div class="article-date">${{article.date_display}}</div>
                            <div class="article-badges">
                                <span class="source-badge ${{sourceClass}}">${{article.source}}</span>
                                <span class="category-tag">${{article.category}}</span>
                                ${{article.jurisdiction === 'state' ? `<span class="jurisdiction-badge">${{article.state || 'STATE'}}</span>` : ''}}
                            </div>
                        </div>
                        <div class="article-title">
                            <a href="${{article.url}}" target="_blank" rel="noopener noreferrer">${{article.title}}</a>
                        </div>
                        <div class="article-summary">${{article.summary || 'No summary available'}}</div>
                        <a href="${{article.url}}" target="_blank" rel="noopener noreferrer" class="article-link">Read more →</a>
                    </div>
                `;
            }}).join('');
        }}

        function filterArticles() {{
            let filtered = articles;

            if (activeJurisdiction === 'federal') {{
                filtered = filtered.filter(a => a.jurisdiction === 'federal');
            }} else if (activeJurisdiction === 'state') {{
                filtered = filtered.filter(a => a.jurisdiction === 'state');
            }}

            if (activeFilter !== 'all') {{
                filtered = filtered.filter(a => a.category === activeFilter);
            }}

            if (searchTerm) {{
                const term = searchTerm.toLowerCase();
                filtered = filtered.filter(a =>
                    a.title.toLowerCase().includes(term) ||
                    a.summary.toLowerCase().includes(term) ||
                    a.source.toLowerCase().includes(term)
                );
            }}

            renderArticles(filtered);
        }}

        document.querySelectorAll('.jurisdiction-toggle .toggle-btn').forEach(btn => {{
            btn.addEventListener('click', function() {{
                document.querySelectorAll('.jurisdiction-toggle .toggle-btn').forEach(b => b.classList.remove('active'));
                this.classList.add('active');
                activeJurisdiction = this.dataset.jurisdiction;
                filterArticles();
            }});
        }});

        document.querySelectorAll('.filter-btn').forEach(btn => {{
            btn.addEventListener('click', function() {{
                document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
                this.classList.add('active');
                activeFilter = this.dataset.filter;
                filterArticles();
            }});
        }});

        document.getElementById('search-input').addEventListener('keyup', function() {{
            searchTerm = this.value;
            filterArticles();
        }});

        document.querySelectorAll('.tab-button').forEach(btn => {{
            btn.addEventListener('click', function() {{
                document.querySelectorAll('.tab-button').forEach(b => b.classList.remove('active'));
                document.querySelectorAll('.tab-content').forEach(c => c.classList.remove('active'));
                this.classList.add('active');
                const tabId = this.dataset.tab;
                document.getElementById(tabId).classList.add('active');
            }});
        }});

        function renderInsights() {{
            const insights = [
                {{ title: 'Enforcement Escalation', text: 'FDA enforcement activity is accelerating with multiple warning letters issued in early 2026. The agency appears ready to pursue pharmaceutical manufacturers and telehealth platforms marketing compounded products.' }},
                {{ title: 'Product Quality Concerns', text: 'Testing by branded manufacturers has documented impurities in compounded injectable and oral formulations. Third-party quality control is becoming essential.' }},
                {{ title: 'Legal Uncertainty', text: 'Regulatory treatment of compounded drugs as manufacturing versus compounding remains in flux. Patent litigation is introducing new risks for compounders.' }}
            ];

            const container = document.getElementById('insights-container');
            container.innerHTML = insights.map(insight => `
                <div class="insight-card">
                    <div class="insight-title">${{insight.title}}</div>
                    <div class="insight-text">${{insight.text}}</div>
                </div>
            `).join('');
        }}
        renderInsights();

        function renderCategoryChart() {{
            const categories = {{}};
            articles.forEach(article => {{
                categories[article.category] = (categories[article.category] || 0) + 1;
            }});

            const container = document.getElementById('timeline-container');
            if (!container) return;

            if (!document.getElementById('category-chart')) {{
                const chartDiv = document.createElement('div');
                chartDiv.id = 'category-chart';
                chartDiv.className = 'chart-container';
                const title = document.createElement('div');
                title.className = 'chart-title';
                title.textContent = 'Articles by Category';
                chartDiv.appendChild(title);
                const barChart = document.createElement('div');
                barChart.className = 'bar-chart';
                barChart.id = 'category-chart-bars';
                chartDiv.appendChild(barChart);
                container.parentElement.insertBefore(chartDiv, container);
            }}

            const chartHTML = Object.entries(categories)
                .sort((a, b) => b[1] - a[1])
                .map(([category, count]) => {{
                    const maxCount = Math.max(...Object.values(categories));
                    const heightPercent = (count / maxCount) * 100;
                    return `
                        <div class="bar-item">
                            <div class="bar" style="height: ${{heightPercent}}%;" title="${{category}}: ${{count}} articles"></div>
                            <div class="bar-value">${{count}}</div>
                            <div class="bar-label">${{category}}</div>
                        </div>
                    `;
                }})
                .join('');

            const chartBarsContainer = document.getElementById('category-chart-bars');
            if (chartBarsContainer) {{
                chartBarsContainer.innerHTML = chartHTML;
            }}
        }}

        function renderTimeline() {{
            const milestones = [
                {{ date: 'Mar 26, 2026', title: 'NPR Reports on FDA Restrictions and Overseas Suppliers', content: 'NPR highlights how FDA restrictions on peptides have driven patients to unregulated overseas suppliers.' }},
                {{ date: 'Mar 13, 2026', title: 'Eli Lilly Issues Impurity Warning on Compounded GLP-1s', content: 'Eli Lilly documents chemical impurities and contamination in compounded products.' }},
                {{ date: 'Mar 09, 2026', title: 'Novo-Hims Partnership Agreement Announced', content: 'Hims & Hers will offer Novo Nordisk branded medications while ceasing compounded GLP-1 marketing.' }}
            ];

            const container = document.getElementById('timeline-container');
            container.innerHTML = milestones.map((milestone, index) => `
                <div class="timeline-item">
                    <div class="timeline-date">${{milestone.date}}</div>
                    <div class="timeline-content">
                        <h4>${{milestone.title}}</h4>
                        <p>${{milestone.content}}</p>
                    </div>
                    <div class="timeline-marker"></div>
                </div>
            `).join('');
        }}
        renderTimeline();

        function renderRisks() {{
            const risks = [
                {{
                    level: 'critical',
                    title: 'Enforcement Escalation',
                    description: 'FDA enforcement action is accelerating with warning letters issued in March 2026. The agency appears prepared to pursue DOJ prosecution.'
                }},
                {{
                    level: 'critical',
                    title: 'Patent Litigation Risk',
                    description: 'Novo Nordisk and Eli Lilly are actively litigating against compounders. Patent enforcement requires companies to prove entitlement to injunctions.'
                }},
                {{
                    level: 'high',
                    title: 'Product Quality and Safety Liability',
                    description: 'Testing has documented impurities in injectable and oral formulations. Products marketed to patients may pose health risks.'
                }},
                {{
                    level: 'high',
                    title: 'State Legislative Fragmentation',
                    description: 'Multiple states have enacted new compounding restrictions. Conflicting state requirements create compliance complexity for interstate platforms.'
                }},
                {{
                    level: 'high',
                    title: 'Marketing and Advertising Violations',
                    description: 'FDA is aggressively targeting misleading claims in compounded drug advertising. Non-compliance risks seizure and injunction.'
                }},
                {{
                    level: 'medium',
                    title: 'API Sourcing and Supply Chain Risk',
                    description: 'FDA announced plans to restrict GLP-1 APIs used in compounded products. Sourcing transparency is now critical.'
                }}
            ];

            const container = document.getElementById('risk-container');
            container.innerHTML = risks.map(risk => `
                <div class="risk-card">
                    <div class="risk-level risk-${{risk.level}}">${{risk.level.toUpperCase()}}</div>
                    <div class="risk-title">${{risk.title}}</div>
                    <div class="risk-description">${{risk.description}}</div>
                </div>
            `).join('');
        }}
        renderRisks();
    </script>
</body>
</html>"""

    return html


def update_html_articles(articles: Dict[str, Dict]) -> bool:
    """Update only the articles data in the existing HTML file.

    Instead of regenerating the entire HTML template (which would overwrite
    hand-crafted jurisdiction sync logic, insights panels, timeline data,
    and risk assessments), this function finds the 'const articles = [...]'
    block in the existing HTML and replaces just the article data.

    Falls back to full HTML generation if the existing file cannot be patched.
    """
    import re

    if not HTML_FILE.exists():
        logger.info("No existing HTML file found, generating from scratch")
        return False

    try:
        with open(HTML_FILE, "r") as f:
            html_content = f.read()
    except Exception as e:
        logger.error(f"Error reading existing HTML: {e}")
        return False

    articles_list = list(articles.values())
    new_articles_json = json.dumps(articles_list, indent=12)

    pattern = r'const articles = \[.*?\];'
    replacement = f'const articles = {new_articles_json[:-1]}];'

    # Use a lambda to avoid regex interpreting backslash sequences (e.g. \u) in the replacement string
    updated_html, count = re.subn(pattern, lambda m: replacement, html_content, count=1, flags=re.DOTALL)

    if count == 0:
        logger.warning("Could not find articles array in existing HTML")
        return False

    # Also update the timestamp comment so we can track when data was last refreshed
    ts = datetime.now().strftime("%Y-%m-%d %H:%M UTC")
    ts_pattern = r'<!--\s*Data last refreshed:.*?-->'
    ts_replacement = f'<!-- Data last refreshed: {ts} -->'
    if re.search(ts_pattern, updated_html):
        updated_html = re.sub(ts_pattern, ts_replacement, updated_html, count=1)
    else:
        updated_html = updated_html.replace('</head>', f'    {ts_replacement}\n</head>', 1)

    try:
        with open(HTML_FILE, "w") as f:
            f.write(updated_html)
        logger.info(f"Patched articles data in existing HTML ({len(articles_list)} articles)")
        return True
    except Exception as e:
        logger.error(f"Error writing patched HTML: {e}")
        return False


def main() -> None:
    """Main execution function."""
    logger.info("Starting FDA regulatory scraper")

    existing_articles = load_existing_articles()
    logger.info(f"Loaded {len(existing_articles)} existing articles")

    logger.info("Fetching articles from RSS feeds")
    rss_articles = fetch_rss_articles()
    logger.info(f"Found {len(rss_articles)} articles from RSS feeds")

    logger.info("Scraping articles from web sources")
    web_articles = fetch_web_articles()
    logger.info(f"Found {len(web_articles)} articles from web sources")

    new_articles = rss_articles + web_articles
    logger.info(f"Total new articles to process: {len(new_articles)}")

    merged_articles = merge_articles(existing_articles, new_articles)
    logger.info(f"Merged total articles: {len(merged_articles)}")

    save_articles(merged_articles)

    DOCS_DIR.mkdir(parents=True, exist_ok=True)

    # Try to patch articles into existing HTML first (preserves jurisdiction
    # sync, insights panels, timeline, risk assessment customizations)
    if not update_html_articles(merged_articles):
        logger.info("Falling back to full HTML generation")
        html_content = generate_html(merged_articles)
        try:
            with open(HTML_FILE, "w") as f:
                f.write(html_content)
            logger.info(f"Generated HTML dashboard at {HTML_FILE}")
        except Exception as e:
            logger.error(f"Error writing HTML file: {e}")
            sys.exit(1)

    logger.info("FDA regulatory scraper completed successfully")


if __name__ == "__main__":
    main()
