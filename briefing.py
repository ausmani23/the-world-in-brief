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
EMAIL_TO          = os.getenv("EMAIL_TO")

TARGET_WORD_COUNT = 750
HOURS_BACK        = 24

# ── News sources ──────────────────────────────────────────────────────────────

RSS_FEEDS = {
    "BBC World":          "http://feeds.bbci.co.uk/news/world/rss.xml",
    "BBC Asia":           "http://feeds.bbci.co.uk/news/world/asia/rss.xml",
    "Al Jazeera":         "https://www.aljazeera.com/xml/rss/all.xml",
    "NYT World":          "https://rss.nytimes.com/services/xml/rss/nyt/World.xml",
    "Dawn Pakistan":      "https://www.dawn.com/feeds/home",
    "Reuters":            "https://feeds.reuters.com/reuters/worldNews",
    "AP Top News":        "https://rsshub.app/apnews/topics/apf-topnews",
    "AP World":           "https://rsshub.app/apnews/topics/apf-worldnews",
    "FT World":           "https://www.ft.com/world?format=rss",
    "The Guardian":       "https://www.theguardian.com/world/rss",
    "NPR World":          "https://feeds.npr.org/1004/rss.xml",
    "Democracy Now":      "https://www.democracynow.org/democracynow.rss",
    "The Hindu":          "https://www.thehindu.com/news/international/?service=rss",
    "Middle East Eye":    "https://www.middleeasteye.net/rss",
    "Foreign Policy":     "https://foreignpolicy.com/feed/",
    "Le Monde (English)": "https://www.lemonde.fr/en/rss/une.xml",
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

    for source, url in RSS_FEEDS.items():
        try:
            feed  = feedparser.parse(url)
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
    "You are a senior news editor. Below is a numbered list of headlines from multiple sources.\n"
    "Select the 40-50 most globally significant and newsworthy stories from the last 24 hours.\n"
    "Prioritise: major geopolitical events, conflicts, elections, economic shifts, South Asia/Pakistan.\n"
    "Ignore: celebrity news, sports, lifestyle, weather, minor local stories, near-duplicates.\n\n"
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

# -- Step 2b: Synthesize with Claude Opus (pass 2) ----------------------------

SYSTEM_PROMPT = (
    "You are the senior editor of a daily global news digest modelled on The Economist's "
    "'The World in Brief' - terse, precise, globally minded, analytically sharp.\n\n"
    "Return your response as a valid JSON object with this exact structure:\n"
    "{\n"
    "  \"lede\": \"Single sentence. The most important development in the world in the past 24 hours.\",\n"
    "  \"stories\": [\n"
    "    {\n"
    "      \"headline\": \"Crisp headline, max 8 words.\",\n"
    "      \"region\": \"One of: South Asia, Middle East, United States, Europe, Africa, Asia, Latin America, Global\",\n"
    "      \"body\": \"3-4 sentences. What happened, what it means, why it matters. Use inline attribution naturally at least once, e.g. The NYT reports that... or According to Al Jazeera...\",\n"
    "      \"sources\": [{ \"name\": \"Source Name\", \"url\": \"https://...\" }]\n"
    "    }\n"
    "  ]\n"
    "}\n\n"
    "RULES:\n"
    "- Cover 6-8 stories. Always include at least one from South Asia or Pakistan if newsworthy.\n"
    "- Write like The Economist: no fluff, short declarative sentences, analytical not descriptive.\n"
    "- Deduplicate: synthesise multiple sources on the same event into one account.\n"
    "- Every story must have at least one source URL in the sources array.\n"
    "- Do NOT include a closing or watch-for section.\n"
    "- Return ONLY the JSON object. No preamble, no markdown fences, no extra text."
)

def synthesize_briefing(items, client):
    today_str = datetime.date.today().strftime("%A, %B %d, %Y")
    headlines = items_to_text(items)
    prompt = (
        f"Today is {today_str}. "
        "The following items were pre-selected as the most newsworthy from the last 24 hours.\n\n"
        "--- HEADLINES ---\n"
        f"{headlines}\n"
        "--- END ---\n\n"
        "Write the briefing JSON now. Return only the JSON object, nothing else."
    )
    log.info(f"Pass 2 - synthesizing {len(items)} curated headlines with Opus...")
    message = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=4000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = message.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    briefing = json.loads(raw)
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

def build_source_links(sources):
    if not sources:
        return ""
    links = []
    for s in sources:
        name = s.get("name", "Source")
        url  = s.get("url", "")
        if url:
            links.append(f'<a href="{url}" style="color:#cc0000;text-decoration:none;font-weight:600;">{name}</a>')
        else:
            links.append(f'<span style="color:#888;">{name}</span>')
    return " &middot; ".join(links)

def build_html(briefing, date_str):
    stories_html = ""
    stories      = briefing.get("stories", [])
    for i, story in enumerate(stories):
        color        = region_color(story.get("region", ""))
        source_links = build_source_links(story.get("sources", []))
        is_last      = i == len(stories) - 1
        border       = "" if is_last else "border-bottom:1px solid #e8e8e8;"

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
          <p style="margin:0 0 10px 0;font-size:14.5px;line-height:1.75;color:#2a2a2a;
                    font-family:Georgia,'Times New Roman',serif;">
            {story.get("body", "")}
          </p>
          {f'<p style="margin:0;font-size:12px;color:#888;font-family:Arial,sans-serif;">Sources: {source_links}</p>' if source_links else ""}
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
    msg["To"]      = EMAIL_TO
    msg.attach(MIMEText(html, "html"))

    log.info(f"Sending to {EMAIL_TO}...")
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, EMAIL_TO, msg.as_string())
    log.info("Email sent.")

# ── Main ──────────────────────────────────────────────────────────────────────

def run():
    date_str = datetime.date.today().strftime("%A, %B %d, %Y")
    log.info(f"=== Daily Briefing --- {date_str} ===")

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

    # Pass 2: Opus writes the full Economist-style briefing from curated headlines
    briefing = synthesize_briefing(curated, client)
    html     = build_html(briefing, date_str)
    send_email(html, date_str)
    log.info("Done!")

if __name__ == "__main__":
    run()
