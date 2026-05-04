#!/usr/bin/env python3
"""
Weekly Digest Agent — companion to The World in Brief.

Pulls 7 days of items from human rights orgs, long-form intellectual journals,
and a curated set of political-economy Substacks. Two-pass Haiku→Opus pipeline
with an analytical voice. Persists a "seen" set across weeks to avoid repeating
items.
"""

import os
import re
import json
import smtplib
import logging
import datetime
import concurrent.futures
import anthropic
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

# Reuse helpers + SMTP config from the daily briefing. Importing also runs
# briefing's load_dotenv() and logging.basicConfig — fine, same env applies.
import briefing
from briefing import (
    utcnow,
    fetch_headlines,
    fetch_article_text,
    SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASSWORD, EMAIL_TO,
    CACHE_DIR,
)

log = logging.getLogger("weekly")

# ── Configuration ─────────────────────────────────────────────────────────────

HOURS_BACK             = 24 * 7
PRESCREEN_TARGET       = 18      # items Haiku should pick
ENRICH_TOP_N           = 25      # how many to fetch full text for
WEEKLY_RETENTION_WEEKS = 8       # bound the "seen" set (must exceed window)

WEEKLY_CACHE_JSON = os.path.join(CACHE_DIR, "weekly.json")
WEEKLY_HTML       = os.path.join(CACHE_DIR, "weekly.html")
WEEKLY_SEEN_JSON  = os.path.join(CACHE_DIR, "weekly_seen.json")

# ── Feeds ─────────────────────────────────────────────────────────────────────

HUMAN_RIGHTS_FEEDS = {
    "Amnesty International":      "https://www.amnesty.org/en/latest/news/feed/",
    "Human Rights Watch":         "https://www.hrw.org/rss/news",
    "MSF":                        "https://www.doctorswithoutborders.org/rss.xml",
    "ICRC Law & Policy":          "https://blogs.icrc.org/law-and-policy/feed/",
    "International Crisis Group": "https://www.crisisgroup.org/rss",
    "CPJ":                        "https://cpj.org/feed/",
    "Article 19":                 "https://www.article19.org/feed/",
    # OHCHR, RSF, ACLED dropped — Cloudflare anti-bot or no public RSS.
}

LONG_FORM_FEEDS = {
    "London Review of Books": "https://www.lrb.co.uk/feeds/rss",
    "New Left Review":        "https://newleftreview.org/feed",
    "Sidecar":                "https://newleftreview.org/sidecar/feed",
    "NYRB":                   "https://www.nybooks.com/rss/",
    "Jacobin":                "https://jacobin.com/feed",
    "Dissent":                "https://www.dissentmagazine.org/feed",
    "n+1":                    "https://www.nplusonemag.com/feed",
    "Boston Review":          "https://www.bostonreview.net/feed",
    "The Baffler":            "https://thebaffler.com/feed",
    "Phenomenal World":       "https://www.phenomenalworld.org/feed/",
    "Verso Books":            "https://www.versobooks.com/blogs/news.atom",
    # Catalyst dropped — last published Aug 2024.
}

SUBSTACK_FEEDS = {
    "Chartbook":                  "https://adamtooze.substack.com/feed",
    "The Overshoot":              "https://theovershoot.co/feed",
    "The Polycrisis":             "https://buttondown.com/polycrisisdispatch/rss",
    "Global Inequality":          "https://branko2f7.substack.com/feed",
    "BIG":                        "https://mattstoller.substack.com/feed",
    "Apricitas Economics":        "https://www.apricitas.io/feed",
    "Origins of Our Time":        "https://ourtime.substack.com/feed",
    # Geoeconomics (Vallée) dropped — last post May 2023.
    "Yanis Varoufakis":           "https://www.yanisvaroufakis.eu/feed/",
    "Mike Konczal":               "https://newsletter.mikekonczal.com/feed",
    "DeLong's Grasping Reality":  "https://braddelong.substack.com/feed",
}

CATEGORY_OF = {
    **{s: "Reports"   for s in HUMAN_RIGHTS_FEEDS},
    **{s: "Essays"    for s in LONG_FORM_FEEDS},
    **{s: "Substacks" for s in SUBSTACK_FEEDS},
}

# ── Persistence: items already covered in prior weeklies ─────────────────────

def load_seen():
    """Returns set of URLs covered in recent weeklies. Purges entries older
    than WEEKLY_RETENTION_WEEKS on read."""
    if not os.path.exists(WEEKLY_SEEN_JSON):
        return set()
    try:
        with open(WEEKLY_SEEN_JSON, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        log.warning(f"Failed to load weekly_seen.json: {e}")
        return set()
    cutoff = (datetime.date.today()
              - datetime.timedelta(weeks=WEEKLY_RETENTION_WEEKS)).isoformat()
    fresh = {url: d for url, d in data.items() if d >= cutoff}
    if len(fresh) != len(data):
        _write_seen(fresh)
    return set(fresh.keys())

def _write_seen(data):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(WEEKLY_SEEN_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def mark_seen(urls):
    today = datetime.date.today().isoformat()
    data = {}
    if os.path.exists(WEEKLY_SEEN_JSON):
        try:
            with open(WEEKLY_SEEN_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            pass
    for u in urls:
        if u and u not in data:
            data[u] = today
    _write_seen(data)

# ── Fetch ─────────────────────────────────────────────────────────────────────

def fetch_weekly_items():
    """Pull last 7 days from all three feed dicts. Annotates each item with
    a `category` field. Reuses briefing.fetch_headlines after temporarily
    bumping its module-level HOURS_BACK to 7 days."""
    saved = briefing.HOURS_BACK
    briefing.HOURS_BACK = HOURS_BACK
    try:
        all_feeds = {**HUMAN_RIGHTS_FEEDS, **LONG_FORM_FEEDS, **SUBSTACK_FEEDS}
        items = fetch_headlines(feeds=all_feeds)
    finally:
        briefing.HOURS_BACK = saved
    for it in items:
        it["category"] = CATEGORY_OF.get(it["source"], "Other")
    return items

def items_to_text_categorised(items):
    """Like briefing.items_to_text but tags each line with category, since the
    weekly pipeline cares about the three-section split."""
    by_source = {}
    for it in items:
        by_source.setdefault((it["category"], it["source"]), []).append(it)
    sections = []
    for (cat, source), entries in sorted(by_source.items()):
        bullets = []
        for e in entries:
            line = f'  - {e["title"]}'
            if e.get("summary"):
                line += f': {e["summary"]}'
            if e.get("body"):
                line += f'\n    [first paragraphs] {e["body"][:600]}'
            if e.get("link"):
                line += f' (link: {e["link"]})'
            bullets.append(line)
        sections.append(f"[{cat} / {source}]\n" + "\n".join(bullets))
    return "\n\n".join(sections)

# ── Pre-screen (Haiku) ────────────────────────────────────────────────────────

PRESCREEN_PROMPT = (
    "You are the editor of a weekly digest of analysis, reporting, and long-form essays.\n"
    "The audience is politically engaged and reads through a materialist lens — attentive\n"
    "to capital, class, the state, geopolitics, and contestation.\n\n"
    "Below is a numbered list of items from the past 7 days. Each line shows\n"
    "[CATEGORY / SOURCE] and the title.\n\n"
    "Categories:\n"
    "  - REPORTS: human rights and accountability bodies (Amnesty, HRW, MSF, OHCHR,\n"
    "    ACLED, CPJ, RSF, ICRC, International Crisis Group)\n"
    "  - ESSAYS: long-form intellectual journals (LRB, NYRB, NLR, Sidecar, Jacobin,\n"
    "    Dissent, n+1, Boston Review, The Baffler, Phenomenal World, Catalyst, Verso)\n"
    "  - SUBSTACKS: political-economy newsletters (Chartbook, The Overshoot, BIG,\n"
    "    Apricitas, Geoeconomics, Polycrisis, Mike Konczal, DeLong, etc.)\n\n"
    f"Select the {PRESCREEN_TARGET} most substantively interesting items, prioritising:\n"
    "  - Original analysis or reporting that shifts understanding of a situation\n"
    "  - Pieces engaging seriously with class, capital, war, climate, labour, the state\n"
    "  - Human rights findings with new evidence, scope, or accountability implications\n"
    "  - Cross-cuts: items in different categories that speak to the same situation\n\n"
    "Avoid: short notices, podcast announcements, fundraising posts, link-list digests,\n"
    "thin opinion takes, items that are mostly book-promotion. Do not select more than\n"
    "3 items from any single source. Aim for a rough mix: 4-6 reports, 4-7 essays,\n"
    "5-8 substacks — but quality trumps the mix.\n\n"
    "Return ONLY a JSON array of selected indices. No preamble, no markdown."
)

def prescreen(items, client):
    title_lines = [
        f"{i}: [{it['category']} / {it['source']}] {it['title']}"
        for i, it in enumerate(items)
    ]
    log.info(f"Pass 1 — pre-screening {len(items)} items with Haiku...")
    msg = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1000,
        messages=[{
            "role": "user",
            "content": PRESCREEN_PROMPT
                       + "\n\n--- ITEMS ---\n"
                       + "\n".join(title_lines)
                       + "\n--- END ---",
        }],
    )
    raw = msg.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    indices = json.loads(raw)
    selected = [items[i] for i in indices if 0 <= i < len(items)]
    log.info(f"Pass 1 done — {len(selected)} items selected")
    return selected

# ── Enrich curated items with full text (trafilatura, no API) ────────────────

def enrich_with_text(items, max_workers=8):
    def _go(it):
        if not it.get("link"):
            return it
        result = fetch_article_text(it["link"])
        if result and result.get("text"):
            it["body"] = result["text"]
        return it
    with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as ex:
        return list(ex.map(_go, items))

# ── Synthesise (Opus) ─────────────────────────────────────────────────────────

SYSTEM_PROMPT = (
    "You are the editor of a weekly digest of analysis, reporting, and long-form essays.\n"
    "The audience is materialist in temperament: attentive to capital, class, the state,\n"
    "and contestation. Your aim is not to summarise items one by one but to organise the\n"
    "week's writing around the substantive THEMES it engages — and then to weave the\n"
    "items into each theme as evidence, argument, and counterpoint.\n\n"
    "STRUCTURE:\n"
    "Identify 4-6 substantive themes the week's items cluster around. Themes are not\n"
    "categories (Reports / Essays / Substacks) but real-world subjects: e.g.\n"
    "  - 'The war on Iran and its ripples'\n"
    "  - 'Resource extraction in the Trump era'\n"
    "  - 'Neoliberalism's mutation, not its end'\n"
    "  - 'Gaza as world event'\n"
    "  - 'Labour and the politics of work'\n"
    "  - 'Climate, authoritarianism, and the state'\n"
    "Each theme weaves together 2-4 items from any combination of categories — the\n"
    "bridges across reports, essays, and substacks are the digest's value.\n\n"
    "VOICE for theme paragraphs:\n"
    "- Analytical and reflective, not terse. 4-7 sentences per theme.\n"
    "- Lead with the substance of the theme, not 'this week, several writers...'\n"
    "- Synthesise: name the argument or finding, then show how the items converge,\n"
    "  diverge, or extend one another. Use writers' names parenthetically, not as\n"
    "  sentence subjects.\n"
    "- Don't editorialise the conclusion. Let the choice of items and framing speak.\n"
    "- Quote sparingly; use single quotes inside JSON strings.\n\n"
    "CITATIONS — inline parentheticals only, no item list:\n"
    "Every claim sourced from an item must end with a parenthetical citation in this\n"
    "exact form, written as a markdown link:\n"
    "    (Lastname, [Abbrev](URL))\n"
    "where Lastname is the author's surname (or first author for multi-author pieces;\n"
    "use just the abbreviation if no author byline exists, e.g. an HRW report) and\n"
    "Abbrev is a short publication tag. Use these abbreviations:\n"
    "  Chartbook = Chartbook   |  The Overshoot = Overshoot   |  BIG = BIG\n"
    "  Apricitas Economics = Apricitas   |  Mike Konczal = Konczal\n"
    "  DeLong's Grasping Reality = DeLong   |  The Polycrisis = Polycrisis\n"
    "  Global Inequality = Milanović  (omit Lastname since it's redundant)\n"
    "  Yanis Varoufakis = Varoufakis  (omit Lastname since it's redundant)\n"
    "  Origins of Our Time = Barker   |  Phenomenal World = PW\n"
    "  London Review of Books = LRB   |  New Left Review = NLR\n"
    "  Sidecar = Sidecar (NLR)   |  NYRB = NYRB   |  Jacobin = Jacobin\n"
    "  Dissent = Dissent   |  n+1 = n+1   |  Boston Review = BR\n"
    "  The Baffler = Baffler   |  Verso Books = Verso\n"
    "  Amnesty International = Amnesty   |  Human Rights Watch = HRW\n"
    "  MSF = MSF   |  ICRC Law & Policy = ICRC   |  CPJ = CPJ\n"
    "  International Crisis Group = Crisis Group   |  Article 19 = Article 19\n"
    "If the abbreviation alone is sufficient (e.g. an organisational report without\n"
    "a personal author), omit Lastname: (HRW), ([Amnesty](URL)). Otherwise: (Tooze,\n"
    "[Chartbook](URL)). Each item should be cited at most once per paragraph; if a\n"
    "paragraph leans on one item heavily, cite at the most natural sentence.\n\n"
    "MUST-READS:\n"
    "After the themes, identify the 5 individual items the reader should not miss this\n"
    "week, given a materialist politics. Prioritise: original analysis or reporting that\n"
    "shifts understanding; essays of unusual depth or reach; rights findings with new\n"
    "evidence; pieces engaging seriously with class, capital, war, climate, labour.\n"
    "Each gets a short reason (1-2 sentences) — concrete, not generic. The must-reads\n"
    "may overlap with items already cited in themes; that's fine and expected.\n\n"
    "OUTPUT — return a single JSON object with this exact structure:\n"
    "{\n"
    '  "intro": "1-2 sentences orienting the reader to the week\'s shape.",\n'
    '  "themes": [\n'
    '    {\n'
    '      "name": "Short theme title (under 60 chars)",\n'
    '      "paragraph": "4-7 sentence synthesis with inline (Lastname, [Abbrev](URL)) citations."\n'
    '    }\n'
    '  ],\n'
    '  "must_reads": [\n'
    '    {\n'
    '      "title": "Original piece title",\n'
    '      "source": "Source name",\n'
    '      "url": "https://...",\n'
    '      "reason": "1-2 sentences on why this is essential reading."\n'
    '    }\n'
    '  ]\n'
    "}\n\n"
    "RULES:\n"
    "- 4-6 themes, each weaving 2+ items via inline parenthetical citations.\n"
    "- Exactly 5 must-reads.\n"
    "- An item may appear in a theme AND in must-reads — duplication is allowed.\n"
    "- Every URL in a citation MUST be from the input items.\n"
    "- Return ONLY the JSON object. No markdown fences, no preamble."
)

def synthesise(items, client):
    today = datetime.date.today().strftime("%A, %B %d, %Y")
    body = items_to_text_categorised(items)
    prompt = (
        f"Today is {today}. The following items were selected from the past 7 days.\n"
        "Each shows category, source, title, summary, and (where available) the first\n"
        "paragraphs of the article body.\n\n"
        "--- ITEMS ---\n"
        f"{body}\n"
        "--- END ---\n\n"
        "Write the weekly digest JSON now. Return only the JSON object."
    )
    log.info(f"Pass 2 — synthesising {len(items)} items with Opus...")
    msg = client.messages.create(
        model="claude-opus-4-5",
        max_tokens=8000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = msg.content[0].text.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        digest = json.loads(raw)
    except json.JSONDecodeError as e:
        log.warning(f"JSON parse failed ({e}), attempting repair...")
        import json_repair
        digest = json_repair.loads(raw)
    themes = digest.get("themes", [])
    total = sum(len(_MD_LINK.findall(t.get("paragraph", ""))) for t in themes)
    log.info(f"Pass 2 done — {len(themes)} themes ({total} cited items), "
             f"{len(digest.get('must_reads', []))} must-reads")
    return digest

# ── Render HTML ───────────────────────────────────────────────────────────────

THEME_COLOR    = "#1a3a4a"  # navy — matches header
MUST_READ_COLOR = "#a8741a"  # warm gold

_MD_LINK = re.compile(r'\[([^\]]+)\]\(([^)]+)\)')

def _render_paragraph(text):
    """Convert markdown links [text](url) inside a paragraph to HTML <a> tags
    styled for inline citations. Citations appear as `(Lastname, Abbrev)` with
    the Abbrev hyperlinked."""
    return _MD_LINK.sub(
        r'<a href="\2" style="color:#1a3a4a;text-decoration:none;border-bottom:1px solid #c9d3da;">\1</a>',
        text,
    )

def build_weekly_html(digest, date_str):
    # ── Themes ──
    themes_html = ""
    themes = digest.get("themes", [])
    for i, theme in enumerate(themes):
        is_last = i == len(themes) - 1
        border = "" if is_last else "border-bottom:1px solid #e8e8e8;"
        name = theme.get("name", "")
        paragraph_html = _render_paragraph(theme.get("paragraph", ""))
        themes_html += f"""
        <div style="margin-bottom:26px;padding-bottom:24px;{border}">
          <table cellpadding="0" cellspacing="0" style="margin-bottom:10px;">
            <tr>
              <td style="background:{THEME_COLOR};color:#fff;font-size:9px;font-weight:700;
                         letter-spacing:0.15em;text-transform:uppercase;padding:4px 10px;
                         border-radius:2px;font-family:Arial,sans-serif;white-space:nowrap;">
                {name}
              </td>
            </tr>
          </table>
          <p style="margin:0;font-size:14.5px;line-height:1.75;color:#2a2a2a;
                    font-family:Georgia,'Times New Roman',serif;">
            {paragraph_html}
          </p>
        </div>"""

    # ── Must-reads ──
    must_reads_html = ""
    must_reads = digest.get("must_reads", [])
    if must_reads:
        rows = ""
        for i, it in enumerate(must_reads):
            n = i + 1
            url = it.get("url", "") or "#"
            title = it.get("title", "")
            source = it.get("source", "")
            reason = it.get("reason", "")
            is_last = i == len(must_reads) - 1
            border = "" if is_last else "border-bottom:1px solid #e8e8e8;"
            rows += f"""
            <div style="margin-bottom:18px;padding-bottom:16px;{border}">
              <table cellpadding="0" cellspacing="0" style="width:100%;">
                <tr>
                  <td style="vertical-align:top;width:32px;padding-right:14px;">
                    <div style="font-family:Georgia,serif;font-size:24px;font-weight:700;color:{MUST_READ_COLOR};line-height:1;">{n}</div>
                  </td>
                  <td style="vertical-align:top;">
                    <p style="margin:0 0 3px 0;font-size:10px;color:#888;font-family:Arial,sans-serif;letter-spacing:0.1em;text-transform:uppercase;">
                      {source}
                    </p>
                    <p style="margin:0 0 6px 0;font-size:15px;font-weight:700;line-height:1.3;font-family:Georgia,'Times New Roman',serif;">
                      <a href="{url}" style="color:#1a1a1a;text-decoration:none;">{title}</a>
                    </p>
                    <p style="margin:0;font-size:13.5px;line-height:1.65;color:#3a3a3a;font-family:Georgia,'Times New Roman',serif;">
                      {reason}
                    </p>
                  </td>
                </tr>
              </table>
            </div>"""
        must_reads_html = f"""
        <div style="margin-top:10px;padding-top:24px;border-top:2px solid {MUST_READ_COLOR};">
          <table cellpadding="0" cellspacing="0" style="margin-bottom:16px;">
            <tr>
              <td style="background:{MUST_READ_COLOR};color:#fff;font-size:9px;font-weight:700;
                         letter-spacing:0.15em;text-transform:uppercase;padding:4px 10px;
                         border-radius:2px;font-family:Arial,sans-serif;white-space:nowrap;">
                Five You Must Read
              </td>
            </tr>
          </table>
          {rows}
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Weekend Reading</title></head>
<body style="margin:0;padding:0;background:#f0ede6;">
<table width="100%" cellpadding="0" cellspacing="0" style="background:#f0ede6;padding:36px 0;">
<tr><td align="center">
<table width="640" cellpadding="0" cellspacing="0" style="max-width:640px;width:100%;">

  <tr><td style="background:#1a3a4a;padding:0;border-radius:4px 4px 0 0;">
    <table width="100%" cellpadding="0" cellspacing="0">
      <tr>
        <td style="padding:22px 32px 18px;">
          <div style="font-family:Georgia,serif;font-size:11px;letter-spacing:0.2em;text-transform:uppercase;color:rgba(255,255,255,0.75);margin-bottom:4px;">
            Weekend Reading
          </div>
          <div style="font-family:Georgia,serif;font-size:28px;font-weight:700;color:#fff;line-height:1.1;">
            {date_str}
          </div>
        </td>
        <td style="padding:22px 32px 18px;text-align:right;vertical-align:bottom;">
          <div style="font-family:Georgia,serif;font-size:11px;color:rgba(255,255,255,0.6);font-style:italic;">
            Reports, essays, dispatches
          </div>
        </td>
      </tr>
    </table>
  </td></tr>

  <tr><td style="background:#1a1a1a;padding:18px 32px;">
    <p style="margin:0;font-family:Georgia,serif;font-size:15px;font-style:italic;color:#f0ede6;line-height:1.6;">
      {digest.get("intro", "")}
    </p>
  </td></tr>

  <tr><td style="background:#1a3a4a;height:3px;font-size:0;line-height:0;">&nbsp;</td></tr>

  <tr><td style="background:#fff;padding:30px 32px 10px;">
    {themes_html}
    {must_reads_html}
  </td></tr>

  <tr><td style="background:#f0ede6;padding:18px 32px;border-top:1px solid #ddd;border-radius:0 0 4px 4px;">
    <p style="margin:0;font-size:11px;color:#999;font-family:Arial,sans-serif;line-height:1.6;text-align:center;">
      A weekly companion to The World in Brief.<br>
      Generated {utcnow().strftime("%H:%M UTC")}
    </p>
  </td></tr>

</table>
</td></tr>
</table>
</body></html>"""

# ── Email / cache ────────────────────────────────────────────────────────────

def send_weekly_email(html, date_str):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"Weekend Reading — {date_str}"
    msg["From"] = SMTP_USER
    msg["Bcc"] = ", ".join(EMAIL_TO)
    msg.attach(MIMEText(html, "html"))
    log.info(f"Sending weekly to {EMAIL_TO}...")
    with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as server:
        server.ehlo()
        server.starttls()
        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_USER, EMAIL_TO, msg.as_string())
    log.info("Email sent.")

def save_weekly_cache(digest):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(WEEKLY_CACHE_JSON, "w", encoding="utf-8") as f:
        json.dump(digest, f, indent=2, ensure_ascii=False)
    log.info(f"Cached digest JSON to {WEEKLY_CACHE_JSON}")

def load_weekly_cache():
    with open(WEEKLY_CACHE_JSON, "r", encoding="utf-8") as f:
        return json.load(f)

def save_weekly_html(html):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(WEEKLY_HTML, "w", encoding="utf-8") as f:
        f.write(html)
    log.info(f"Saved HTML to {WEEKLY_HTML}")

# ── Main ──────────────────────────────────────────────────────────────────────

def run(dry_run=False, cached=False):
    date_str = datetime.date.today().strftime("%B %d, %Y")
    log.info(f"=== Weekly Digest --- {date_str} ===")

    if cached:
        digest = load_weekly_cache()
        html = build_weekly_html(digest, date_str)
        save_weekly_html(html)
        log.info(f"Done. Open {WEEKLY_HTML} to preview.")
        return

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

    items = fetch_weekly_items()
    if not items:
        log.error("No items fetched. Aborting.")
        return

    seen = load_seen()
    pre_dedup = len(items)
    items = [it for it in items if it.get("link") and it["link"] not in seen]
    log.info(f"After dedup against prior weeklies: {len(items)}/{pre_dedup}")

    if not items:
        log.warning("All items already covered. Nothing to send.")
        return

    curated = prescreen(items, client)
    if not curated:
        log.warning("Prescreener returned nothing — falling back to first 30 items")
        curated = items[:30]

    curated = enrich_with_text(curated[:ENRICH_TOP_N])

    digest = synthesise(curated, client)
    html = build_weekly_html(digest, date_str)

    save_weekly_cache(digest)

    # Collect URLs from inline citations in theme paragraphs + must_reads.
    written_urls = []
    for t in digest.get("themes", []):
        for _, url in _MD_LINK.findall(t.get("paragraph", "")):
            written_urls.append(url)
    written_urls += [it.get("url") for it in digest.get("must_reads", [])]
    mark_seen(written_urls)

    if dry_run:
        save_weekly_html(html)
        log.info(f"Dry run — email skipped. Open {WEEKLY_HTML} to preview.")
    else:
        send_weekly_email(html, date_str)
    log.info("Done!")

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Weekly Digest Agent")
    p.add_argument("--dry-run", action="store_true",
                   help="Run pipeline but save HTML instead of emailing")
    p.add_argument("--cached", action="store_true",
                   help="Re-render HTML from cached digest JSON")
    args = p.parse_args()
    run(dry_run=args.dry_run, cached=args.cached)
