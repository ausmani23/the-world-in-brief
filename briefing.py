#!/usr/bin/env python3
"""
Daily News Briefing Agent — v3
- Economist "World in Brief" aesthetic
- Pulls all items from the last 24 hours
- Inline source attribution + links
- Expanded global sources
- No "watch for" closing section
"""

import os
import re
import json
import smtplib
import logging
import datetime
import hashlib
import concurrent.futures
import feedparser
import requests
import trafilatura
import htmldate
import anthropic
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Configuration ─────────────────────────────────────────────────────────────

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY")
SMTP_HOST         = os.getenv("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT         = int(os.getenv("SMTP_PORT", "587"))
SMTP_USER         = os.getenv("SMTP_USER")
SMTP_PASSWORD     = os.getenv("SMTP_PASSWORD")
EMAIL_TO_RAW      = os.getenv("EMAIL_TO", "")
EMAIL_TO          = [e.strip() for e in EMAIL_TO_RAW.split(",") if e.strip()]

TARGET_WORD_COUNT = 1800
HOURS_BACK        = 24
FEED_TIMEOUT      = 20   # seconds before giving up on a slow/dead feed

# ── News sources ──────────────────────────────────────────────────────────────

RSS_FEEDS = {
    # ── English: core global wires ────────────────────────────────────────────
    "BBC World":           "http://feeds.bbci.co.uk/news/world/rss.xml",
    "Al Jazeera (EN)":     "https://www.aljazeera.com/xml/rss/all.xml",
    "NYT World":           "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    "The Guardian":        "https://www.theguardian.com/world/rss",
    "FT World":            "https://www.ft.com/world?format=rss",
    "Dawn Pakistan":       "https://www.dawn.com/feeds/home",
    "Middle East Eye":     "https://www.middleeasteye.net/rss",
    "Mercopress":          "https://en.mercopress.com/rss/latin-america",
    "Africanews":          "https://www.africanews.com/feed/rss",
    "Nikkei Asia":         "https://asia.nikkei.com/rss/feed/nar",
    "SCMP":                "https://www.scmp.com/rss/91/feed",
    "Moscow Times":        "https://www.themoscowtimes.com/rss/news",
    "France 24":           "https://www.france24.com/en/rss",

    # ── Arabic: genuine Arab editorial voices ─────────────────────────────────
    "Al Jazeera (AR)":     "https://www.aljazeera.net/xml/rss/all.xml",

    # ── Spanish: Latin American left perspective ──────────────────────────────
    "Telesur":             "https://www.telesurenglish.net/rss/News.xml",

    # ── French: genuine French editorial voices ───────────────────────────────
    "Le Monde (FR)":       "https://www.lemonde.fr/rss/une.xml",
    "RFI":                 "https://www.rfi.fr/fr/rss",

    # ── German: genuine German editorial voices ───────────────────────────────
    "Deutsche Welle (DE)": "https://rss.dw.com/rdf/rss-de-all",
    "Der Spiegel":         "https://www.spiegel.de/schlagzeilen/tops/index.rss",

    # ── Persian/Farsi: Iranian state perspective (Tasnim DNS unreachable) ──────
    # "Tasnim News":       "https://www.tasnimnews.com/en/rss/feed/0/2/0/",

    # ── Portuguese: genuine Brazilian editorial voice ─────────────────────────
    "Folha de Sao Paulo":  "https://feeds.folha.uol.com.br/mundo/rss091.xml",

    # ── Chinese (Mandarin): genuine Chinese editorial voice ───────────────────
    "DW Chinese":          "https://rss.dw.com/rdf/rss-chi-all",

    # ── Hindi: genuine Indian editorial voice ─────────────────────────────────
    "Dainik Bhaskar":      "https://www.bhaskar.com/rss-v1--category-1061.xml",

    # ── State / official media (use biases strategically) ────────────────────
    "RT":                  "https://www.rt.com/rss/news/",
    "CGTN":                "https://www.cgtn.com/subscribe/rss/section/world.xml",
    "Global Times":        "https://www.globaltimes.cn/rss/outbrain.xml",
}

# ── Right-wing sources (kept out of the general pool, used for
#    "The View From the Right" section only) ──────────────────────────────────

RIGHT_WING_FEEDS = {
    "Fox News":            "https://moxie.foxnews.com/google-publisher/world.xml",
    "Breitbart":           "https://feeds.feedburner.com/breitbart",
    "Daily Wire":          "https://www.dailywire.com/feeds/rss.xml",
    "NY Post":             "https://nypost.com/feed/",
    "WSJ Opinion":         "https://feeds.a.dj.com/rss/RSSOpinion",
    "The Telegraph":       "https://www.telegraph.co.uk/rss.xml",
    "GB News":             "https://www.gbnews.com/feeds/rss",
    "National Review":     "https://www.nationalreview.com/feed/",
}

# ── Step 1: Fetch headlines from the last 24 hours ────────────────────────────

def utcnow():
    return datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)

def parse_published(entry):
    """Returns (datetime, has_timestamp). If no timestamp, returns (utcnow, False)."""
    t = entry.get("published_parsed") or entry.get("updated_parsed")
    if t:
        return datetime.datetime(*t[:6]), True
    return utcnow(), False

def fetch_headlines(feeds=None):
    feeds     = feeds or RSS_FEEDS
    cutoff    = utcnow() - datetime.timedelta(hours=HOURS_BACK)
    all_items = []

    import socket
    for source, url in feeds.items():
        try:
            old_timeout = socket.getdefaulttimeout()
            socket.setdefaulttimeout(FEED_TIMEOUT)
            try:
                # Pass a real-browser UA — feedparser's default UA is blocked by
                # several feeds we want (Polycrisis, Geoeconomics, Substack-on-
                # custom-domain, n+1, Catalyst, etc.) behind anti-bot services.
                feed = feedparser.parse(url, agent=ARTICLE_UA)
            finally:
                socket.setdefaulttimeout(old_timeout)

            count = 0
            for entry in feed.entries:
                pub, has_timestamp = parse_published(entry)
                if has_timestamp and pub < cutoff:
                    continue
                title   = entry.get("title", "").strip()
                summary = re.sub(r"<[^>]+>", "", entry.get("summary", "")).strip()[:300]
                link    = entry.get("link", "")
                pub_str = pub.strftime("%H:%M UTC") if has_timestamp else "recent"
                all_items.append({
                    "source":    source,
                    "title":     title,
                    "summary":   summary,
                    "link":      link,
                    "published": pub_str,
                })
                count += 1
            log.info(f"  {source}: {count} items in last {HOURS_BACK}h")
        except Exception as e:
            log.error(f"Failed to fetch {source}: {e}")

    log.info(f"Total items fetched: {len(all_items)}")
    return all_items

# ── Article extraction (trafilatura) ──────────────────────────────────────────
#
# Helper for fetching the title + first few paragraphs of any article URL.
# Used by the homepage-scrape pipeline below to read articles from sources that
# don't publish working RSS feeds (Iranian outlets, Chinese-language papers,
# etc.). We *never* pass full bodies downstream: the newsletter is a survey
# tool, not a summary, so only the opening paragraphs go into Claude's context.

ARTICLE_CACHE_DIR     = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache", "articles")
ARTICLE_MAX_PARAGRAPHS = 5         # ~3-5 grafs ≈ 300-500 words
ARTICLE_FETCH_TIMEOUT  = 15        # seconds per URL
ARTICLE_FETCH_WORKERS  = 10
ARTICLE_CACHE_DAYS     = 14
ARTICLE_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36"
)

def _article_cache_path(url):
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return os.path.join(ARTICLE_CACHE_DIR, f"{h}.json")

def _load_cached_article(url):
    path = _article_cache_path(url)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        text = data.get("text") or None
        if text is None:
            return None
        return {"text": text, "date": data.get("date")}
    except Exception:
        return None

def _save_cached_article(url, text, date_str=None):
    os.makedirs(ARTICLE_CACHE_DIR, exist_ok=True)
    try:
        with open(_article_cache_path(url), "w", encoding="utf-8") as f:
            json.dump({"url": url, "text": text, "date": date_str,
                       "fetched_at": utcnow().isoformat()},
                      f, ensure_ascii=False)
    except Exception as e:
        log.debug(f"  cache write failed for {url}: {e}")

def _cleanup_old_articles():
    if not os.path.isdir(ARTICLE_CACHE_DIR):
        return
    cutoff = utcnow() - datetime.timedelta(days=ARTICLE_CACHE_DAYS)
    for name in os.listdir(ARTICLE_CACHE_DIR):
        path = os.path.join(ARTICLE_CACHE_DIR, name)
        try:
            mtime = datetime.datetime.utcfromtimestamp(os.path.getmtime(path))
            if mtime < cutoff:
                os.remove(path)
        except Exception:
            pass

def fetch_article_text(url):
    """
    Fetch a URL, extract the main article text + publication date with
    trafilatura's bare_extraction(). Returns {"text": ..., "date": "YYYY-MM-DD"
    or None} on success, None on failure. Cached per-URL.
    """
    if not url:
        return None
    cached = _load_cached_article(url)
    if cached is not None:
        return cached
    try:
        resp = requests.get(url, headers={"User-Agent": ARTICLE_UA},
                            timeout=ARTICLE_FETCH_TIMEOUT, allow_redirects=True)
        if resp.status_code != 200 or not resp.text:
            return None
        result = trafilatura.bare_extraction(
            resp.text,
            include_comments=False,
            include_tables=False,
            favor_precision=False,
        )
        if not result or not getattr(result, "text", None):
            return None
        text = result.text
        paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
        truncated = "\n\n".join(paragraphs[:ARTICLE_MAX_PARAGRAPHS]).strip()
        if not truncated:
            return None
        date_str = getattr(result, "date", None)  # "YYYY-MM-DD" or None
        # Fallback: htmldate is more aggressive — checks meta tags, <time>
        # elements, JSON-LD, and text patterns that trafilatura may skip.
        if not date_str:
            try:
                date_str = htmldate.find_date(
                    resp.text, extensive_search=True, original_date=True
                )
            except Exception:
                pass
        _save_cached_article(url, truncated, date_str)
        return {"text": truncated, "date": date_str}
    except Exception as e:
        log.debug(f"  trafilatura fetch failed for {url}: {e}")
        return None

# ── Homepage scrape pipeline ──────────────────────────────────────────────────
#
# For news sources that lack a working RSS feed (especially non-English
# outlets), we scrape the homepage instead. The pipeline per source is:
#   1. Fetch the homepage HTML with requests + desktop UA
#   2. Extract + rank candidate links using DOM position and URL heuristics
#   3. For each candidate, call fetch_article_text() to get title + grafs
#      and publication date via trafilatura's bare_extraction()
#   4. Keep only articles published within the last 24h (skip undated ones)
#   5. Emit items in the same shape as fetch_headlines() so they slot into
#      the existing prescreening pool
#
# No API calls needed — trafilatura's extraction quality is the natural
# filter (non-article pages yield no text), and its date metadata enforces
# the 24h recency window.

# Sources to scrape via homepage. The dict maps display name → homepage URL.
# Curated jointly with the user from regions/languages where RSS coverage is
# weak. JS-only sites (Press TV, etc.) may quietly fail — that's fine, they'll
# be silently skipped.
SCRAPE_SOURCES = {
    # ── Africa ────────────────────────────────────────────────────────────────
    "Jeune Afrique":         "https://www.jeuneafrique.com/",
    # "Le Monde Afrique":    "https://www.lemonde.fr/afrique/",   # 402 paywall on bare GET
    "Al-Ahram":              "https://www.ahram.org.eg/",
    "Daily Nation":          "https://nation.africa/kenya",

    # ── Latin America ─────────────────────────────────────────────────────────
    "La Jornada":            "https://www.jornada.com.mx/",
    "Página/12":             "https://www.pagina12.com.ar/",
    "Granma":                "https://www.granma.cu/",

    # ── Middle East (Arab world) ──────────────────────────────────────────────
    "Asharq Al-Awsat":       "https://aawsat.com/",
    # "Al-Akhbar":           "https://al-akhbar.com/",            # 403 Cloudflare challenge
    # "Al Mayadeen":         "https://www.almayadeen.net/",       # 403 Cloudflare challenge
    "Haaretz":               "https://www.haaretz.co.il/",

    # ── Iran ──────────────────────────────────────────────────────────────────
    # Iranian outlets are mostly broken to bare HTTP — JS-rendered SPAs or DNS
    # failures. Press TV and Tehran Times work; the others need a real browser.
    "Press TV":              "https://www.presstv.ir/",
    # "Tasnim News":         "https://www.tasnimnews.com/fa",     # connection failure
    # "IRNA":                "https://www.irna.ir/",              # 200 but JS-only (1.7KB shell)
    "Tehran Times":          "https://www.tehrantimes.com/",

    # ── East Asia ─────────────────────────────────────────────────────────────
    "People's Daily":        "http://www.people.com.cn/",
    "Xinhua":                "http://www.news.cn/",
    # "The Paper":           "https://www.thepaper.cn/",          # 403 bot block
    "Asahi Shimbun":         "https://www.asahi.com/",
    "Hankyoreh":             "https://www.hani.co.kr/",

    # ── South / Southeast Asia ────────────────────────────────────────────────
    "Kompas":                "https://www.kompas.com/",
    "The Hindu":              "https://www.thehindu.com/",

    # ── Europe (continental) ──────────────────────────────────────────────────
    "El País":               "https://elpais.com/",
    "La Repubblica":         "https://www.repubblica.it/",
    "Gazeta Wyborcza":       "https://wyborcza.pl/",

    # ── Russia / former USSR ──────────────────────────────────────────────────
    # "TASS":                "https://tass.ru/",                   # 200 but JS-only (1.7KB shell)
    "Kommersant":            "https://www.kommersant.ru/",
    "Novaya Gazeta Europe":  "https://novayagazeta.eu/",

    # ── Turkey ────────────────────────────────────────────────────────────────
    "Hürriyet":              "https://www.hurriyet.com.tr/",
    "Cumhuriyet":            "https://www.cumhuriyet.com.tr/",
}

# Scrape sources whose primary publication language is not English.
# Used (alongside the existing NON_ENGLISH_SOURCES set below) to count toward
# the 30% non-English language floor enforced after prescreening.
NON_ENGLISH_SCRAPE_SOURCES = {
    name for name in SCRAPE_SOURCES
    if name not in {"Daily Nation", "The Hindu", "Press TV", "Tehran Times"}
}

SCRAPE_LINKS_PER_SOURCE   = 8     # how many article links to keep per source
SCRAPE_MAX_CANDIDATES     = 40    # top-ranked candidates to try trafilatura on
SCRAPE_HOMEPAGE_TIMEOUT   = 15
SCRAPE_SOURCE_WORKERS     = 6     # no more Haiku rate-limit concern
SCRAPE_ARTICLE_WORKERS    = 5     # parallel article fetches within one source

def _extract_date_from_url(url):
    """
    Try to extract a publication date from common URL patterns like
    /2026/04/10/ or /2026-04-10/ or /20260410/. Returns a datetime.date
    or None.
    """
    # /YYYY/MM/DD/ or /YYYY-MM-DD/
    m = re.search(r'/(\d{4})[/-](\d{1,2})[/-](\d{1,2})(?:/|$|\?|-)', url)
    if m:
        try:
            return datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            pass
    # /YYYYMMDD/ (compact, e.g. People's Daily)
    m = re.search(r'/(\d{4})(\d{2})(\d{2})(?:/|$|\?|\.)', url)
    if m:
        try:
            d = datetime.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            # Sanity: must be a plausible news date (2020–next year)
            if 2020 <= d.year <= datetime.date.today().year + 1:
                return d
        except ValueError:
            pass
    return None


def _is_within_24h(d):
    """Check if a date is today or yesterday (covers the 24h window)."""
    today = datetime.date.today()
    return d >= today - datetime.timedelta(days=1)


# Ancestor tags that signal navigational / non-article context
_NAV_ANCESTORS = {'nav', 'header', 'footer', 'aside'}
# URL path segments that indicate non-article pages
_SKIP_URL_PATTERNS = re.compile(
    r'/(category|tag|author|video|podcast|gallery|photo|login|signup|subscribe'
    r'|account|privacy|contact|about|rss|feed|search|archive)(/|$)', re.I
)
# URL path segments that suggest article pages
_ARTICLE_URL_PATTERNS = re.compile(
    r'/(article|news|story|opinion|report|analysis|editorial|feature|noticia'
    r'|actualite|nachricht|articolo|materia)', re.I
)


def _extract_link_candidates(html_content, base_url):
    """
    Parse homepage HTML and return a ranked list of (anchor_text, absolute_url)
    pairs most likely to be article links. Uses DOM position and URL heuristics
    to prioritize — no API calls needed. Pre-filters by URL date when available,
    rejecting links clearly older than 24h.
    """
    from urllib.parse import urljoin, urlparse
    try:
        from lxml import html as lxml_html
        tree = lxml_html.fromstring(html_content)
    except Exception:
        return []

    base_domain = urlparse(base_url).netloc.replace('www.', '')
    raw = []
    seen = set()

    for a in tree.xpath('//a[@href]'):
        href = (a.get('href') or '').strip()
        if not href or href.startswith('#') or href.startswith('javascript:') or href.startswith('mailto:'):
            continue
        url = urljoin(base_url, href)
        if not url.startswith('http'):
            continue
        # Skip links to other domains
        link_domain = urlparse(url).netloc.replace('www.', '')
        if link_domain != base_domain:
            continue
        # Skip known non-article URL patterns
        if _SKIP_URL_PATTERNS.search(url):
            continue

        text = ' '.join((a.text_content() or '').split()).strip()
        if len(text) < 15 or len(text) > 250:
            continue
        if url in seen:
            continue
        seen.add(url)

        # ── URL date filter: reject links clearly older than 24h ──
        url_date = _extract_date_from_url(url)
        if url_date and not _is_within_24h(url_date):
            continue

        # ── Score the candidate ──
        score = 0

        # Prefer links inside <main> or <article> elements
        in_nav = False
        parent = a.getparent()
        while parent is not None:
            tag = parent.tag if isinstance(parent.tag, str) else ''
            if tag in ('main', 'article', 'section'):
                score += 3
            if tag in _NAV_ANCESTORS:
                in_nav = True
            parent = parent.getparent()
        if in_nav:
            score -= 5

        # URL looks like an article
        if _ARTICLE_URL_PATTERNS.search(url):
            score += 2
        # URL has a recent date in it
        if url_date:
            score += 3

        raw.append((score, text, url))

    # Sort by score descending, then by document order (stable sort preserves
    # insertion order for equal scores, so prominent top-of-page links win)
    raw.sort(key=lambda x: -x[0])
    return [(text, url) for _, text, url in raw[:SCRAPE_MAX_CANDIDATES]]

def fetch_homepage_items(source_name, homepage_url):
    """
    Scrape one homepage source: fetch homepage HTML, rank candidate links using
    DOM/URL heuristics, fetch each candidate with trafilatura until we have
    SCRAPE_LINKS_PER_SOURCE articles confirmed within the last 24h.
    No API calls — trafilatura's extraction quality is the filter.
    Returns items in fetch_headlines() shape, [] on failure.
    """
    try:
        resp = requests.get(homepage_url, headers={"User-Agent": ARTICLE_UA},
                            timeout=SCRAPE_HOMEPAGE_TIMEOUT, allow_redirects=True)
        if resp.status_code != 200 or not resp.text:
            log.warning(f"  {source_name}: homepage returned {resp.status_code}")
            return []
        candidates = _extract_link_candidates(resp.text, homepage_url)
        if not candidates:
            log.warning(f"  {source_name}: no candidate links extracted (likely JS-only)")
            return []
    except Exception as e:
        log.warning(f"  {source_name}: homepage fetch failed: {e}")
        return []

    # Fetch candidates with trafilatura. Date-filter using the extracted
    # publication date; skip articles older than 24h or with no date at all.
    items = []
    skipped_no_date = 0
    skipped_old = 0
    skipped_no_text = 0

    def _try_article(candidate):
        title, url = candidate
        result = fetch_article_text(url)
        if not result:
            return ("no_text", None)
        # Check publication date — prefer trafilatura's extracted date,
        # fall back to URL date pattern
        date_str = result.get("date")
        article_date = None
        if date_str:
            try:
                article_date = datetime.date.fromisoformat(date_str)
            except (ValueError, TypeError):
                pass
        if article_date is None:
            article_date = _extract_date_from_url(url)
        if article_date is None:
            return ("no_date", None)
        if not _is_within_24h(article_date):
            return ("old", None)
        snippet = result["text"].replace("\n\n", " ").strip()[:300]
        return ("ok", {
            "source":    source_name,
            "title":     title,
            "summary":   snippet,
            "link":      url,
            "published": date_str or "recent",
        })

    with concurrent.futures.ThreadPoolExecutor(max_workers=SCRAPE_ARTICLE_WORKERS) as ex:
        futures = {ex.submit(_try_article, c): c for c in candidates}
        for future in concurrent.futures.as_completed(futures):
            status, item = future.result()
            if status == "ok":
                items.append(item)
                if len(items) >= SCRAPE_LINKS_PER_SOURCE:
                    break
            elif status == "no_date":
                skipped_no_date += 1
            elif status == "old":
                skipped_old += 1
            else:
                skipped_no_text += 1

    log.info(f"  {source_name}: {len(items)} items | skipped: {skipped_no_text} no-text, "
             f"{skipped_old} old, {skipped_no_date} no-date")
    return items


def fetch_scraped_items(sources=None):
    """
    Run fetch_homepage_items across all SCRAPE_SOURCES in parallel. Returns a
    flat list of items in the same shape as fetch_headlines().
    No API client needed — scraping is purely HTTP + trafilatura.
    """
    sources = sources if sources is not None else SCRAPE_SOURCES
    if not sources:
        return []
    log.info(f"Scraping {len(sources)} non-RSS sources...")
    _cleanup_old_articles()
    all_items = []

    def _scrape_one(item):
        name, url = item
        return fetch_homepage_items(name, url)

    with concurrent.futures.ThreadPoolExecutor(max_workers=SCRAPE_SOURCE_WORKERS) as ex:
        for items in ex.map(_scrape_one, sources.items()):
            all_items.extend(items)
    log.info(f"Total scraped items: {len(all_items)}")
    return all_items

def items_to_text(items):
    by_source = {}
    for item in items:
        by_source.setdefault(item["source"], []).append(item)
    sections = []
    for source, entries in by_source.items():
        bullets = []
        for e in entries:
            line = f'  - [{e["published"]}] {e["title"]}'
            if e["summary"]:
                line += f': {e["summary"]}'
            if e["link"]:
                line += f' (link: {e["link"]})'
            bullets.append(line)
        sections.append(f"[{source}]\n" + "\n".join(bullets))
    return "\n\n".join(sections)

# -- Step 2a: Pre-screen with Claude Haiku (pass 1) --------------------------

PRESCREEN_PROMPT = (
    "You are a senior news editor selecting stories for a daily global briefing.\n"
    "Below is a numbered list of headlines. Each headline includes a timestamp.\n\n"
    "RULE 1 - RECENCY: Only select headlines with a timestamp from the last 24 hours.\n"
    "   If a headline has no timestamp or is marked 'recent', include it.\n"
    "   If a headline has a timestamp older than 24 hours, exclude it.\n\n"
    "RULE 2 - IMPORTANCE: From the eligible headlines, prioritise:\n"
    "   - Major geopolitical events, conflicts, elections, diplomatic shifts\n"
    "   - Economic developments: markets, trade, sanctions, inequality, labour policy\n"
    "   - Class struggle and social movements: strikes, labour disputes, union activity,\n"
    "     protests, uprisings, riots, occupations, and popular mobilisations of any kind\n"
    "   - State power and its contestation: coups, crackdowns, mass movements, repression\n"
    "   - Corporate power, privatisation, austerity, and their social consequences\n\n"
    "NOTE: Sources are in multiple languages including Arabic, Spanish, French, German,\n"
    "Persian, Portuguese, Chinese, and Hindi. Treat all equally regardless of language.\n"
    "State media (RT, People's Daily) may surface stories Western outlets ignore —\n"
    "include these if genuinely newsworthy, especially on how non-Western states frame\n"
    "global events, the Global South, or critiques of Western power. Be aware each\n"
    "outlet has editorial biases — that is precisely what makes them useful.\n\n"
    "RULE 3 - BREADTH: Ensure geographic spread AND some linguistic diversity.\n"
    "   Geographic: Americas, Europe, Middle East, Africa, South Asia, Southeast/East Asia.\n"
    "   Linguistic: at least 30% of your selected headlines must come from non-English\n"
    "   sources (Arabic, Spanish, French, German, Persian, Portuguese, Chinese, Hindi).\n"
    "   These sources are just as important as English ones and should not be crowded out.\n"
    "   If many headlines cover the same story (e.g. US/Israel/Iran), pick at most 2-3.\n"
    "   Always include at least one story from South Asia or Pakistan if one is present.\n\n"
    "Ignore: celebrity news, sports, lifestyle, weather, minor local stories.\n\n"
    "Select 40-50 headlines total — do not exceed 50.\n"
    "Do not select more than 5 headlines from any single source.\n"
    "If a source has many headlines on the same story, pick only the single best one.\n"
    "Return ONLY a JSON array of selected index numbers, e.g. [0, 3, 7, 12, ...].\n"
    "No explanation, no preamble - just the JSON array."
)

def prescreen_items(items, client, previous_briefing=None):
    # Pass 1: send titles only to Haiku; get back the most newsworthy subset
    title_lines = [f"{i}: [{item['source']}] {item['title']}" for i, item in enumerate(items)]
    titles_text = "\n".join(title_lines)

    dedup_block = ""
    if previous_briefing:
        prev_headlines = [s["headline"] for s in previous_briefing.get("stories", [])]
        if prev_headlines:
            prev_list = "\n".join(f"  - {h}" for h in prev_headlines)
            dedup_block = (
                "\n\nRULE 4 - AVOID DUPLICATES: Yesterday's briefing already covered these stories:\n"
                f"{prev_list}\n"
                "Do NOT select headlines that cover the same story unless the headline clearly\n"
                "indicates a genuinely new development (new actions, reactions, data, escalations).\n"
                "If in doubt, skip it — today's briefing should feel fresh.\n"
            )

    log.info(f"Pass 1 - pre-screening {len(items)} headlines with Haiku...")
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1000,
        messages=[{
            "role": "user",
            "content": PRESCREEN_PROMPT + dedup_block +
                       "\n\n--- HEADLINES ---\n" + titles_text + "\n--- END ---"
        }],
    )
    raw = message.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    selected_indices = json.loads(raw)
    selected_indices = [i for i in selected_indices if 0 <= i < len(items)]
    selected = [items[i] for i in selected_indices]
    log.info(f"Pass 1 done - {len(selected)} headlines selected")
    return selected

# State / official media — used for "View From Russia, China and Iran" section
STATE_MEDIA_SOURCES = {"RT", "CGTN", "Global Times"}

# Sources that publish in non-English languages.
# Includes both RSS sources (top set) and the non-English entries from
# SCRAPE_SOURCES (added via | NON_ENGLISH_SCRAPE_SOURCES).
NON_ENGLISH_SOURCES = {
    "Al Jazeera (AR)", "Telesur", "Le Monde (FR)", "RFI",
    "Deutsche Welle (DE)", "Der Spiegel",
    "Folha de Sao Paulo", "DW Chinese", "Dainik Bhaskar",
} | NON_ENGLISH_SCRAPE_SOURCES

def enforce_language_floor(selected, all_items, floor=0.30):
    """
    Ensure at least `floor` fraction of the FINAL list comes from non-English
    sources. Solves for the right number of additions so the ratio is met after
    the list grows: needed = ceil((floor * S - N) / (1 - floor)), where S is
    the original size and N is the current non-English count.
    """
    import math, random
    n_non_eng = sum(1 for x in selected if x["source"] in NON_ENGLISH_SOURCES)
    s = len(selected)

    if s > 0 and n_non_eng / s >= floor:
        log.info(f"Language floor met: {n_non_eng}/{s} non-English items ({n_non_eng/s:.0%})")
        return selected

    # Solve: (n_non_eng + needed) / (s + needed) >= floor
    needed = max(1, math.ceil((floor * s - n_non_eng) / (1 - floor)))
    log.info(f"Language floor not met: {n_non_eng}/{s} non-English ({n_non_eng/s:.0%} < {floor:.0%}). "
             f"Adding {needed} non-English items...")

    selected_titles = {x["title"] for x in selected}
    candidates = [
        x for x in all_items
        if x["source"] in NON_ENGLISH_SOURCES
        and x["title"] not in selected_titles
    ]
    random.shuffle(candidates)
    additions = candidates[:needed]
    result = selected + additions
    final_non_eng = n_non_eng + len(additions)
    log.info(f"After top-up: {final_non_eng}/{len(result)} non-English items ({final_non_eng/len(result):.0%})")
    return result

# -- Step 2b: Synthesize with Claude Opus (pass 2) ----------------------------

SYSTEM_PROMPT = (
    "You are the senior editor of a daily global news digest. Your editorial model is\n"
    "The Economist's 'The World in Brief' in form - terse, precise, globally minded -\n"
    "but your analytical lens is materialist. You read the world through the logic of\n"
    "capital, class, and power.\n\n"
    "EDITORIAL VOICE:\n"
    "- Write with a materialist sensibility. Weave analysis of economic forces, class\n"
    "  interests, and power into the prose — don't announce it. The reader should feel\n"
    "  the lens in the choice of facts and framing, not in explicit declarations.\n"
    "- Headlines come from sources in many languages. Translate and synthesise across\n"
    "  them freely. Aim for at least 30% of your footnote citations across the whole\n"
    "  briefing to draw on non-English sources. Do not rely only on English-language\n"
    "  outlets even when they cover the same story.\n"
    "- Note when a story is covered differently by outlets with different editorial\n"
    "  perspectives (e.g. Western vs. Chinese vs. Russian media) — this divergence is\n"
    "  itself worth a sentence when it illuminates something the dominant framing misses.\n"
    "- Do not editorialize explicitly. Do not end paragraphs with a sentence that draws\n"
    "  a moral or analytical conclusion for the reader. Trust the reported facts to speak.\n"
    "- Take social movements, strikes, protests, uprisings, and popular mobilisations\n"
    "  seriously as historical forces, not merely as disruptions to be managed.\n"
    "- Be analytically sharp but never didactic. Let the facts carry the argument.\n"
    "- Write like The Economist in style: short declarative sentences, no fluff.\n\n"
    "CITATION STYLE — law review footnotes:\n"
    "Each sentence in the body must end with a superscript footnote number in square\n"
    "brackets, e.g. [1], [2]. At the end of the story, list the footnotes with the\n"
    "source name and URL. Multiple sources for one sentence go in one footnote,\n"
    "comma-separated. Sources may recur across footnotes with new numbers each time.\n\n"
    "Return your response as a valid JSON object with this exact structure:\n"
    "{\n"
    "  \"lede\": \"Single sentence. The most important development in the world in the past 24 hours.\",\n"
    "  \"stories\": [\n"
    "    {\n"
    "      \"headline\": \"Crisp headline, max 8 words.\",\n"
    "      \"region\": \"One of: South Asia, Middle East, United States, Europe, Africa, Asia, Latin America, Global\",\n"
    "      \"sentences\": [\n"
    "        { \"text\": \"Sentence text ending with superscript e.g. [1]\", \"refs\": [1] },\n"
    "        { \"text\": \"Next sentence.[2]\", \"refs\": [2] }\n"
    "      ],\n"
    "      \"note\": \"Each story should have 3-4 sentences — same depth as before.\",\n"
    "      \"footnotes\": [\n"
    "        { \"n\": 1, \"name\": \"Source Name\", \"url\": \"https://...\" },\n"
    "        { \"n\": 2, \"name\": \"Source Name\", \"url\": \"https://...\" }\n"
    "      ]\n"
    "    }\n"
    "  ],\n"
    "  \"other_side\": {\n"
    "    \"sentences\": [\n"
    "      { \"text\": \"Sentence with footnote.[1]\", \"refs\": [1] }\n"
    "    ],\n"
    "    \"footnotes\": [\n"
    "      { \"n\": 1, \"name\": \"RT\", \"url\": \"https://...\" }\n"
    "    ]\n"
    "  },\n"
    "  \"right_wing\": {\n"
    "    \"sentences\": [\n"
    "      { \"text\": \"Sentence with footnote.[1]\", \"refs\": [1] }\n"
    "    ],\n"
    "    \"footnotes\": [\n"
    "      { \"n\": 1, \"name\": \"Fox News\", \"url\": \"https://...\" }\n"
    "    ]\n"
    "  }\n"
    "}\n\n"
    "THE 'OTHER_SIDE' SECTION — 'The View From Russia, China and Iran':\n"
    "This section summarises what state media (RT, People's Daily, Tasnim News, etc.) are\n"
    "saying about the REST OF THE WORLD — not about their own governments. What narratives\n"
    "are they pushing about Western policy, global conflicts, or the international order?\n"
    "Write 3-4 sentences with footnotes citing the state media sources. If no state media\n"
    "headlines are provided, set other_side to null.\n\n"
    "THE 'RIGHT_WING' SECTION — 'The View From the Right':\n"
    "This section summarises what conservative and right-wing outlets (Fox News, Breitbart,\n"
    "Daily Wire, Wall Street Journal opinion, National Review, NY Post, GB News, The Telegraph,\n"
    "etc.) are paying attention to and how they are framing it. What stories dominate their\n"
    "coverage? What narratives are they advancing about domestic politics, culture, the economy,\n"
    "or international affairs? Note divergences from mainstream or left-leaning coverage when\n"
    "illuminating. Write 3-4 sentences with footnotes citing the right-wing sources. If no\n"
    "right-wing headlines are provided, set right_wing to null.\n\n"
    "IMPORTANT: When quoting someone within a JSON string field, always use single quotes\n"
    "e.g. Trump said 'very good' talks — never double quotes, which break JSON parsing.\n\n"
    "RULES:\n"
    "- Cover 9-12 stories. Always include at least one from South Asia or Pakistan if newsworthy.\n"
    "- Always include labour, social movement, or class struggle stories if present in the headlines.\n"
    "- Deduplicate: synthesise multiple sources on the same event into one account.\n"
    "- Every sentence must have at least one footnote reference.\n"
    "- Do NOT include a closing or watch-for section.\n"
    "- Return ONLY the JSON object. No preamble, no markdown fences, no extra text."
)

def synthesize_briefing(items, client, previous_briefing=None, state_media_items=None, right_wing_items=None):
    today_str = datetime.date.today().strftime("%A, %B %d, %Y")
    headlines = items_to_text(items)

    dedup_block = ""
    if previous_briefing:
        prev_stories = previous_briefing.get("stories", [])
        prev_summary = json.dumps(
            [{"headline": s["headline"], "region": s.get("region", ""),
              "sentences": [sent["text"] for sent in s.get("sentences", [])]}
             for s in prev_stories],
            indent=2, ensure_ascii=False
        )
        dedup_block = (
            "\n--- YESTERDAY'S BRIEFING ---\n"
            "The following stories were covered in yesterday's briefing. Do NOT repeat a story\n"
            "unless today's headlines contain genuinely new developments — new actions, reactions,\n"
            "decisions, data, or escalations. If the only change is that additional outlets are now\n"
            "covering the same facts, skip the story entirely. When a previously covered story does\n"
            "have substantive new developments, cover it fully, focusing on what is new.\n\n"
            f"{prev_summary}\n"
            "--- END YESTERDAY ---\n\n"
        )

    state_media_block = ""
    if state_media_items:
        state_media_text = items_to_text(state_media_items)
        state_media_block = (
            "\n--- STATE MEDIA HEADLINES (for 'The View From Russia, China and Iran') ---\n"
            "Use these for the other_side section. Focus on what these outlets are saying\n"
            "about the rest of the world, not about their own governments.\n\n"
            f"{state_media_text}\n"
            "--- END STATE MEDIA ---\n\n"
        )

    right_wing_block = ""
    if right_wing_items:
        right_wing_text = items_to_text(right_wing_items)
        right_wing_block = (
            "\n--- RIGHT-WING HEADLINES (for 'The View From the Right') ---\n"
            "Use these for the right_wing section. What are these outlets paying attention to\n"
            "and how are they framing it? Note divergences from mainstream coverage.\n\n"
            f"{right_wing_text}\n"
            "--- END RIGHT-WING ---\n\n"
        )

    prompt = (
        f"Today is {today_str}. "
        "The following items were pre-selected as the most newsworthy from the last 24 hours.\n\n"
        "Each story should be approximately 120 words — 3-4 substantive sentences.\n"
        "Do not shorten stories to fit more in. Depth per story is more important than total length.\n\n"
        "--- HEADLINES ---\n"
        f"{headlines}\n"
        "--- END ---\n\n"
        f"{state_media_block}"
        f"{right_wing_block}"
        f"{dedup_block}"
        "Write the briefing JSON now. Return only the JSON object, nothing else."
    )
    log.info(f"Pass 2 - synthesizing {len(items)} curated headlines with Opus...")
    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=8000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        briefing = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning(f"JSON parse failed ({e}), attempting repair...")
        try:
            import json_repair
            briefing = json_repair.loads(raw)
            log.info("JSON repair succeeded")
        except Exception as e2:
            log.error(f"JSON repair also failed: {e2}")
            log.error(f"Raw response (first 2000 chars):\n{raw[:2000]}")
            raise e
    log.info(f"Pass 2 done - {len(briefing.get('stories', []))} stories written")
    return briefing

# ── Step 3: Build HTML email ──────────────────────────────────────────────────

REGION_COLORS = {
    "South Asia":    "#1a6b3c",
    "Pakistan":      "#1a6b3c",
    "Middle East":   "#8b4513",
    "United States": "#1a3a6b",
    "Europe":        "#2c4a7c",
    "Africa":        "#6b4e1a",
    "Asia":          "#4a1a6b",
    "Global":        "#555555",
    "Latin America": "#6b1a1a",
}

def region_color(region):
    for key, color in REGION_COLORS.items():
        if key.lower() in region.lower():
            return color
    return "#555555"

def render_footnotes(footnotes):
    """Render a numbered footnote block with hyperlinks."""
    if not footnotes:
        return ""
    items = []
    for fn in footnotes:
        n    = fn.get("n", "?")
        name = fn.get("name", "Source")
        url  = fn.get("url", "")
        if url:
            link = f'<a href="{url}" style="color:#cc0000;text-decoration:none;">{name}</a>'
        else:
            link = f'<span>{name}</span>'
        items.append(f'<span style="margin-right:12px;">{n}.&nbsp;{link}</span>')
    return "".join(items)

def render_body(sentences):
    """
    Render sentences list into HTML, converting [N] markers to
    superscript footnote anchors.
    """
    import re
    if not sentences:
        return ""
    parts = []
    for s in sentences:
        text = s.get("text", "")
        # Replace [N] with superscript
        text = re.sub(
            r'\[(\d+)\]',
            r'<sup style="font-size:9px;color:#cc0000;font-family:Arial,sans-serif;">\1</sup>',
            text
        )
        parts.append(text)
    return " ".join(parts)

def build_html(briefing, date_str):
    stories_html = ""
    stories      = briefing.get("stories", [])
    for i, story in enumerate(stories):
        color   = region_color(story.get("region", ""))
        is_last = i == len(stories) - 1
        border  = "" if is_last else "border-bottom:1px solid #e8e8e8;"

        body_html      = render_body(story.get("sentences", []))
        footnotes_html = render_footnotes(story.get("footnotes", []))

        # Fallback: if Opus returned old-style body/sources, render those
        if not body_html and story.get("body"):
            body_html = story.get("body", "")

        stories_html += f"""
        <div style="margin-bottom:26px;padding-bottom:24px;{border}">
          <table cellpadding="0" cellspacing="0" style="margin-bottom:10px;">
            <tr>
              <td style="background:{color};color:#fff;font-size:9px;font-weight:700;
                         letter-spacing:0.1em;text-transform:uppercase;padding:3px 9px;
                         border-radius:2px;font-family:Arial,sans-serif;white-space:nowrap;">
                {story.get("region", "World")}
              </td>
            </tr>
          </table>
          <p style="margin:0 0 8px 0;font-size:17px;font-weight:700;line-height:1.3;
                    color:#1a1a1a;font-family:Georgia,'Times New Roman',serif;">
            {story.get("headline", "")}
          </p>
          <p style="margin:0 0 8px 0;font-size:14.5px;line-height:1.75;color:#2a2a2a;
                    font-family:Georgia,'Times New Roman',serif;">
            {body_html}
          </p>
          {f'<p style="margin:0;font-size:11px;color:#999;font-family:Arial,sans-serif;line-height:1.8;">{footnotes_html}</p>' if footnotes_html else ""}
        </div>"""

    # "The View From Russia, China and Iran" section — styled like a regular story
    other_side_html = ""
    other_side = briefing.get("other_side")
    if other_side and other_side.get("sentences"):
        os_body = render_body(other_side.get("sentences", []))
        os_footnotes = render_footnotes(other_side.get("footnotes", []))
        other_side_html = f"""
        <div style="margin-bottom:26px;padding-top:24px;border-top:1px solid #e8e8e8;">
          <table cellpadding="0" cellspacing="0" style="margin-bottom:10px;">
            <tr>
              <td style="background:#333;color:#fff;font-size:9px;font-weight:700;
                         letter-spacing:0.1em;text-transform:uppercase;padding:3px 9px;
                         border-radius:2px;font-family:Arial,sans-serif;white-space:nowrap;">
                The View From Russia, China and Iran
              </td>
            </tr>
          </table>
          <p style="margin:0 0 8px 0;font-size:14.5px;line-height:1.75;color:#2a2a2a;
                    font-family:Georgia,'Times New Roman',serif;">
            {os_body}
          </p>
          {f'<p style="margin:0;font-size:11px;color:#999;font-family:Arial,sans-serif;line-height:1.8;">{os_footnotes}</p>' if os_footnotes else ""}
        </div>"""

    # "The View From the Right" section
    right_wing_html = ""
    right_wing = briefing.get("right_wing")
    if right_wing and right_wing.get("sentences"):
        rw_body = render_body(right_wing.get("sentences", []))
        rw_footnotes = render_footnotes(right_wing.get("footnotes", []))
        right_wing_html = f"""
        <div style="margin-bottom:26px;padding-top:24px;border-top:1px solid #e8e8e8;">
          <table cellpadding="0" cellspacing="0" style="margin-bottom:10px;">
            <tr>
              <td style="background:#8b1a1a;color:#fff;font-size:9px;font-weight:700;
                         letter-spacing:0.1em;text-transform:uppercase;padding:3px 9px;
                         border-radius:2px;font-family:Arial,sans-serif;white-space:nowrap;">
                The View From the Right
              </td>
            </tr>
          </table>
          <p style="margin:0 0 8px 0;font-size:14.5px;line-height:1.75;color:#2a2a2a;
                    font-family:Georgia,'Times New Roman',serif;">
            {rw_body}
          </p>
          {f'<p style="margin:0;font-size:11px;color:#999;font-family:Arial,sans-serif;line-height:1.8;">{rw_footnotes}</p>' if rw_footnotes else ""}
        </div>"""

    source_list  = ", ".join(
        list(RSS_FEEDS.keys()) + list(SCRAPE_SOURCES.keys()) + list(RIGHT_WING_FEEDS.keys())
    )
    generated_at = utcnow().strftime("%H:%M UTC")

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Daily Briefing</title>
</head>
<body style="margin:0;padding:0;background:#f0ede6;">
  <table width="100%" cellpadding="0" cellspacing="0" style="background:#f0ede6;padding:36px 0;">
  <tr><td align="center">
  <table width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;">

    <tr><td style="background:#cc0000;padding:0;border-radius:4px 4px 0 0;">
      <table width="100%" cellpadding="0" cellspacing="0">
        <tr>
          <td style="padding:22px 32px 18px;">
            <div style="font-family:Georgia,serif;font-size:11px;letter-spacing:0.2em;
                        text-transform:uppercase;color:rgba(255,255,255,0.75);margin-bottom:4px;">
              The World in Brief
            </div>
            <div style="font-family:Georgia,serif;font-size:28px;font-weight:700;color:#fff;line-height:1.1;">
              {date_str}
            </div>
          </td>
          <td style="padding:22px 32px 18px;text-align:right;vertical-align:bottom;">
            <div style="font-family:Georgia,serif;font-size:11px;color:rgba(255,255,255,0.6);font-style:italic;">
              Your daily global digest
            </div>
          </td>
        </tr>
      </table>
    </td></tr>

    <tr><td style="background:#1a1a1a;padding:18px 32px;">
      <p style="margin:0;font-family:Georgia,serif;font-size:15px;font-style:italic;
                color:#f0ede6;line-height:1.6;">
        {briefing.get("lede", "")}
      </p>
    </td></tr>

    <tr><td style="background:#cc0000;height:3px;font-size:0;line-height:0;">&nbsp;</td></tr>

    <tr><td style="background:#fff;padding:30px 32px 10px;">
      {stories_html}
      {other_side_html}
      {right_wing_html}
    </td></tr>

    <tr><td style="background:#f0ede6;padding:18px 32px;border-top:1px solid #ddd;border-radius:0 0 4px 4px;">
      <p style="margin:0;font-size:11px;color:#999;font-family:Arial,sans-serif;
                line-height:1.6;text-align:center;">
        Compiled from: {source_list}<br>
        Generated {generated_at}
      </p>
    </td></tr>

  </table>
  </td></tr>
  </table>
</body>
</html>"""

# ── Step 4: Send email ────────────────────────────────────────────────────────

def send_email(html, date_str):
    msg            = MIMEMultipart("alternative")
    msg["Subject"] = f"The World in Brief — {date_str}"
    msg["From"]    = SMTP_USER
    msg["Bcc"]     = ", ".join(EMAIL_TO)
    msg.attach(MIMEText(html, "html"))

    log.info(f"Sending to {EMAIL_TO}...")
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, EMAIL_TO, msg.as_string())  # accepts a list
    log.info("Email sent.")

# ── Cache helpers ─────────────────────────────────────────────────────────────

CACHE_DIR       = os.path.join(os.path.dirname(os.path.abspath(__file__)), "cache")
CACHE_JSON      = os.path.join(CACHE_DIR, "briefing.json")
OUTPUT_HTML     = os.path.join(CACHE_DIR, "briefing.html")
CACHE_RETENTION = 14  # days to keep dated briefing files

def save_cache(briefing):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(CACHE_JSON, "w", encoding="utf-8") as f:
        json.dump(briefing, f, indent=2, ensure_ascii=False)
    # Also save a date-stamped copy for dedup and weekly summaries
    dated = os.path.join(CACHE_DIR, f"briefing_{datetime.date.today().isoformat()}.json")
    with open(dated, "w", encoding="utf-8") as f:
        json.dump(briefing, f, indent=2, ensure_ascii=False)
    log.info(f"Cached briefing JSON to {CACHE_JSON} and {dated}")
    cleanup_old_cache()

def cleanup_old_cache():
    """Remove dated briefing files older than CACHE_RETENTION days."""
    import glob
    cutoff = (datetime.date.today() - datetime.timedelta(days=CACHE_RETENTION)).isoformat()
    for path in glob.glob(os.path.join(CACHE_DIR, "briefing_*.json")):
        date_part = os.path.basename(path).replace("briefing_", "").replace(".json", "")
        if date_part < cutoff:
            os.remove(path)
            log.info(f"Removed old cache file: {os.path.basename(path)}")

def load_cache():
    with open(CACHE_JSON, "r", encoding="utf-8") as f:
        return json.load(f)

def load_previous_briefing():
    """Load the most recent dated briefing JSON before today, if any."""
    import glob
    today = datetime.date.today().isoformat()
    files = sorted(glob.glob(os.path.join(CACHE_DIR, "briefing_*.json")))
    # Find the latest file that isn't today's
    for path in reversed(files):
        basename = os.path.basename(path)
        date_part = basename.replace("briefing_", "").replace(".json", "")
        if date_part < today:
            log.info(f"Found previous briefing: {basename}")
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
    log.info("No previous briefing found — first run or cache cleared")
    return None

def save_html(html):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    log.info(f"Saved HTML to {OUTPUT_HTML}")

# ── Main ──────────────────────────────────────────────────────────────────────

def run(dry_run=False, cached=False):
    date_str = datetime.date.today().strftime("%A, %B %d, %Y")
    log.info(f"=== Daily Briefing --- {date_str} ===")

    if cached:
        log.info("Using cached briefing JSON (skipping fetch + API calls)")
        briefing = load_cache()
        html = build_html(briefing, date_str)
        save_html(html)
        log.info(f"Done! Open {OUTPUT_HTML} to preview.")
        return

    # Single shared API client — used by the prescreen (Haiku) and synthesis
    # (Opus) passes. Homepage scraping no longer needs it.
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Fetch everything published in the last 24 hours across all RSS sources
    items = fetch_headlines()

    # Supplement with sources that don't have working RSS feeds — homepage
    # scrape via trafilatura (no API calls, date-filtered to last 24h).
    # These items join the main pool and go through prescreening alongside RSS.
    scraped = fetch_scraped_items()
    items.extend(scraped)
    log.info(f"Total items in pool (RSS + scraped): {len(items)}")

    if not items:
        log.error("No items fetched. Aborting.")
        return

    # Fetch right-wing sources separately (not in the general pool)
    right_wing_items = fetch_headlines(feeds=RIGHT_WING_FEEDS)
    log.info(f"Right-wing items fetched: {len(right_wing_items)}")

    # Load yesterday's briefing (if any) — used by both Haiku (prescreen dedup)
    # and Opus (synthesis dedup) to avoid stale repeats
    previous = load_previous_briefing()

    # Pass 1: Haiku quickly picks the ~40 most newsworthy headlines
    curated = prescreen_items(items, client, previous_briefing=previous)
    if not curated:
        log.warning("Pre-screener returned nothing - falling back to all items")
        curated = items

    # Enforce 30% non-English floor in code, not just in prompt
    curated = enforce_language_floor(curated, items, floor=0.30)

    # Collect state media items not already in the curated set for "The View From Russia, China and Iran"
    curated_titles = {x["title"] for x in curated}
    state_media_extra = [
        x for x in items
        if x["source"] in STATE_MEDIA_SOURCES and x["title"] not in curated_titles
    ]
    log.info(f"State media items for 'Russia/China/Iran': {len(state_media_extra)} "
             f"(plus any already in curated pool)")

    # Pass 2: Opus writes the full Economist-style briefing from curated headlines
    briefing = synthesize_briefing(curated, client, previous_briefing=previous,
                                   state_media_items=state_media_extra,
                                   right_wing_items=right_wing_items)
    html     = build_html(briefing, date_str)

    # Always cache the briefing JSON for --cached reruns
    save_cache(briefing)

    if dry_run:
        save_html(html)
        log.info(f"Dry run — email skipped. Open {OUTPUT_HTML} to preview.")
    else:
        send_email(html, date_str)
    log.info("Done!")

if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Daily News Briefing Agent")
    parser.add_argument("--dry-run", action="store_true",
                        help="Run full pipeline but save HTML to file instead of emailing")
    parser.add_argument("--cached", action="store_true",
                        help="Re-render HTML from cached briefing JSON (skips fetch + API calls)")
    args = parser.parse_args()
    run(dry_run=args.dry_run, cached=args.cached)
