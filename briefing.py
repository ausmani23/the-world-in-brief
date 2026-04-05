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
import feedparser
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

    # ── Persian/Farsi: Iranian state perspective ──────────────────────────────
    "Tasnim News":         "https://www.tasnimnews.com/en/rss/feed/0/2/0/",

    # ── Portuguese: genuine Brazilian editorial voice ─────────────────────────
    "Folha de Sao Paulo":  "https://feeds.folha.uol.com.br/mundo/rss091.xml",

    # ── Chinese (Mandarin): genuine Chinese editorial voice ───────────────────
    "DW Chinese":          "https://rss.dw.com/rdf/rss-chi-all",

    # ── Hindi: genuine Indian editorial voice ─────────────────────────────────
    "Dainik Bhaskar":      "https://www.bhaskar.com/rss-v1--category-1061.xml",

    # ── State / official media (use biases strategically) ────────────────────
    "RT":                  "https://www.rt.com/rss/news/",
    "People's Daily":      "http://en.people.cn/rss/world.xml",
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

def fetch_headlines():
    cutoff    = utcnow() - datetime.timedelta(hours=HOURS_BACK)
    all_items = []

    import socket
    for source, url in RSS_FEEDS.items():
        try:
            old_timeout = socket.getdefaulttimeout()
            socket.setdefaulttimeout(FEED_TIMEOUT)
            try:
                feed = feedparser.parse(url)
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

def prescreen_items(items, client):
    # Pass 1: send titles only to Haiku; get back the most newsworthy subset
    title_lines = [f"{i}: [{item['source']}] {item['title']}" for i, item in enumerate(items)]
    titles_text = "\n".join(title_lines)
    log.info(f"Pass 1 - pre-screening {len(items)} headlines with Haiku...")
    message = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1000,
        messages=[{
            "role": "user",
            "content": PRESCREEN_PROMPT + "\n\n--- HEADLINES ---\n" + titles_text + "\n--- END ---"
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

# Sources that publish in non-English languages
NON_ENGLISH_SOURCES = {
    "Al Jazeera (AR)", "Telesur", "Le Monde (FR)", "RFI",
    "Deutsche Welle (DE)", "Der Spiegel", "Tasnim News",
    "Folha de Sao Paulo", "DW Chinese", "Dainik Bhaskar",
}

def enforce_language_floor(selected, all_items, floor=0.30):
    """
    Ensure at least `floor` fraction of selected items come from non-English sources.
    If Haiku under-selected non-English items, top up by sampling from the full pool.
    """
    import random
    non_eng = [i for i, x in enumerate(selected) if x["source"] in NON_ENGLISH_SOURCES]
    target  = max(1, int(len(selected) * floor))

    if len(non_eng) >= target:
        log.info(f"Language floor met: {len(non_eng)}/{len(selected)} non-English items ({len(non_eng)/len(selected):.0%})")
        return selected

    # How many more do we need?
    needed = target - len(non_eng)
    log.info(f"Language floor not met: {len(non_eng)}/{len(selected)} non-English. Adding {needed} more...")

    # Find non-English items in the full pool that weren't already selected
    selected_titles = {x["title"] for x in selected}
    candidates = [
        x for x in all_items
        if x["source"] in NON_ENGLISH_SOURCES
        and x["title"] not in selected_titles
    ]
    random.shuffle(candidates)
    additions = candidates[:needed]
    result = selected + additions
    log.info(f"After top-up: {target}/{len(result)} non-English items ({target/len(result):.0%})")
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
    "  ]\n"
    "}\n\n"
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

def synthesize_briefing(items, client, previous_briefing=None):
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

    prompt = (
        f"Today is {today_str}. "
        "The following items were pre-selected as the most newsworthy from the last 24 hours.\n\n"
        "Each story should be approximately 120 words — 3-4 substantive sentences.\n"
        "Do not shorten stories to fit more in. Depth per story is more important than total length.\n\n"
        "--- HEADLINES ---\n"
        f"{headlines}\n"
        "--- END ---\n\n"
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

    source_list  = ", ".join(RSS_FEEDS.keys())
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
    msg["To"]      = ", ".join(EMAIL_TO)
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

    # Fetch everything published in the last 24 hours across all sources
    items = fetch_headlines()
    if not items:
        log.error("No headlines fetched. Aborting.")
        return

    # Single shared API client for both passes
    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    # Pass 1: Haiku quickly picks the ~40 most newsworthy headlines
    curated = prescreen_items(items, client)
    if not curated:
        log.warning("Pre-screener returned nothing - falling back to all items")
        curated = items

    # Enforce 30% non-English floor in code, not just in prompt
    curated = enforce_language_floor(curated, items, floor=0.30)

    # Load yesterday's briefing (if any) so Opus can avoid stale repeats
    previous = load_previous_briefing()

    # Pass 2: Opus writes the full Economist-style briefing from curated headlines
    briefing = synthesize_briefing(curated, client, previous_briefing=previous)
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
