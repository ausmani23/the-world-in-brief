#!/usr/bin/env python3
"""
Reader — a daily Instapaper queue, companion to The World in Brief.

Every day: pull the last 7 days from a wide set of essay sites and Substacks,
score every piece Sonnet hasn't already scored on a four-dimension rubric tuned
to the reader's interests, pick the top 5 (never re-saving a URL, at most one
per source per day and two per source per week), and push them to Instapaper.
Today's picks are also written into cache/reader_state.json, where briefing.py
finds them and renders a "Worth Your Time" section in the daily email.

    python reader.py              # full run: score, select, save to Instapaper
    python reader.py --dry-run    # score + select + print; no Instapaper, no state
                                  # writes except the score cache
    python reader.py --limit 15   # score at most 15 new items (cheap testing)
"""

import os
import re
import json
import logging
import datetime
import threading
import concurrent.futures
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

import requests
import trafilatura
import anthropic

# Reuse the feed fetcher + env/logging setup from the daily. Importing briefing
# runs its load_dotenv() and logging.basicConfig — same env applies.
import briefing
from briefing import utcnow, fetch_headlines, CACHE_DIR, ARTICLE_UA

log = logging.getLogger("reader")

# ── Configuration ─────────────────────────────────────────────────────────────

HOURS_BACK              = 24 * 7   # fetch window
WINDOW_DAYS             = 7        # a piece competes for 7 days after publication
PICKS_PER_DAY           = 5
MAX_PER_SOURCE_PER_DAY  = 1
MAX_PER_SOURCE_PER_WEEK = 2
MIN_FEED_CONTENT_WORDS  = 400      # trust feed-supplied body only if this long
BODY_MAX_WORDS          = 1500     # what the scorer reads
BODY_FETCH_TIMEOUT      = 20
SCORE_WORKERS           = 6
SCORE_MODEL             = "claude-sonnet-5"
RUBRIC_VERSION          = 1        # bump when the rubric/profile changes → rescores
SCORES_RETENTION_DAYS   = 14
SAVED_RETENTION_DAYS    = 60
PICKS_RETENTION_DAYS    = 14

STATE_JSON = os.path.join(CACHE_DIR, "reader_state.json")

INSTAPAPER_USER     = os.getenv("INSTAPAPER_USER", "")
INSTAPAPER_PASSWORD = os.getenv("INSTAPAPER_PASSWORD", "")
INSTAPAPER_API      = "https://www.instapaper.com/api"

# ── Sources ───────────────────────────────────────────────────────────────────
# Probed 2026-09-10 (claude_workspace/probe_reader_feeds.py). Dropped for no
# working feed: Boston Review (feed returns 0 entries), Hedgehog Review (404),
# Follow the Money / Setser (CFR killed blog feeds), Interfluidity (dormant
# since 2024), The Polycrisis (dormant since spring 2026). Low-cadence sources
# (Dan Wang, Scholar's Stage, Liberties, Nonsite, Three-Toed Sloth) are kept:
# they cost nothing when silent.

JOURNAL_FEEDS = {
    "London Review of Books": "https://www.lrb.co.uk/feeds/rss",
    "New Left Review":        "https://newleftreview.org/feed",
    "Sidecar":                "https://newleftreview.org/sidecar/feed",
    "NYRB":                   "https://www.nybooks.com/rss/",
    "Jacobin":                "https://jacobin.com/feed",
    "Dissent":                "https://www.dissentmagazine.org/feed",
    "n+1":                    "https://www.nplusonemag.com/feed",
    "The Baffler":            "https://thebaffler.com/feed",
    "Phenomenal World":       "https://www.phenomenalworld.org/feed/",
    "Verso":                  "https://www.versobooks.com/blogs/news.atom",
    "Monthly Review":         "https://monthlyreview.org/feed/",
    "Nonsite":                "https://nonsite.org/feed/",
}

POLITICAL_ECONOMY_FEEDS = {
    "Chartbook":                 "https://adamtooze.substack.com/feed",
    "The Overshoot":             "https://theovershoot.co/feed",
    "Global Inequality":         "https://branko2f7.substack.com/feed",
    "BIG":                       "https://mattstoller.substack.com/feed",
    "Apricitas Economics":       "https://www.apricitas.io/feed",
    "Origins of Our Time":       "https://ourtime.substack.com/feed",
    "Yanis Varoufakis":          "https://www.yanisvaroufakis.eu/feed/",
    "Mike Konczal":              "https://newsletter.mikekonczal.com/feed",
    "DeLong's Grasping Reality": "https://braddelong.substack.com/feed",
    "Notes on the Crises":       "https://www.crisesnotes.com/rss/",
    "Employ America":            "https://www.employamerica.org/feed/",
    "Paul Krugman":              "https://paulkrugman.substack.com/feed",
    "Back of Mind":              "https://backofmind.substack.com/feed",
    "Michael Roberts":           "https://thenextrecession.wordpress.com/feed/",
}

PUNISHMENT_FEEDS = {
    "Inquest":                  "https://inquest.org/feed/",
    "Vital City":               "https://www.vitalcitynyc.org/feed",
    "The Marshall Project":     "https://www.themarshallproject.org/rss/recent",
    "Prison Policy Initiative": "https://www.prisonpolicy.org/blog/feed/",
}

CONTRARIAN_FEEDS = {
    "Astral Codex Ten":      "https://www.astralcodexten.com/feed",
    "Marginal Revolution":   "https://marginalrevolution.com/feed",
    "Slow Boring":           "https://www.slowboring.com/feed",
    "Noahpinion":            "https://www.noahpinion.blog/feed",
    "Symbolic Capital(ism)": "https://musaalgharbi.substack.com/feed",
    "Freddie deBoer":        "https://freddiedeboer.substack.com/feed",
    "The Scholar's Stage":   "https://scholars-stage.org/feed/",
    "Dan Wang":              "https://danwang.co/feed/",
    "Construction Physics":  "https://www.construction-physics.com/feed",
    "Works in Progress":     "https://www.worksinprogress.news/feed",
    "Asterisk":              "https://asteriskmag.com/feed",
    "American Affairs":      "https://americanaffairsjournal.org/feed/",
    "Compact":               "https://www.compactmag.com/feed/",
    "Palladium":             "https://www.palladiummag.com/feed/",
    "The Point":             "https://thepointmag.com/feed/",
    "The Drift":             "https://www.thedriftmag.com/feed/",
    "Liberties":             "https://libertiesjournal.com/feed/",
    "Aeon":                  "https://aeon.co/feed.rss",
    "Noema":                 "https://www.noemamag.com/feed/",
    "Persuasion":            "https://www.persuasion.community/feed",
}

SOCIAL_SCIENCE_FEEDS = {
    "Statistical Modeling": "https://statmodeling.stat.columbia.edu/feed/",
    "Crooked Timber":       "https://crookedtimber.org/feed/",
    "Kieran Healy":         "https://kieranhealy.org/blog/index.xml",
    "Kevin Munger":         "https://kevinmunger.substack.com/feed",
    "Three-Toed Sloth":     "http://bactra.org/weblog/index.rss",
    "Broadstreet":          "https://broadstreet.blog/feed/",
}

WORLD_FEEDS = {
    "ChinaTalk":           "https://www.chinatalk.media/feed",
    "Comment is Freed":    "https://samf.substack.com/feed",
    "Foreign Affairs":     "https://www.foreignaffairs.com/rss.xml",
    "Africa is a Country": "https://africasacountry.com/feed",
    "Himal Southasian":    "https://www.himalmag.com/feed/",
    "Dawn (opinion)":      "https://www.dawn.com/feeds/opinion",
}

READER_FEEDS = {
    **JOURNAL_FEEDS, **POLITICAL_ECONOMY_FEEDS, **PUNISHMENT_FEEDS,
    **CONTRARIAN_FEEDS, **SOCIAL_SCIENCE_FEEDS, **WORLD_FEEDS,
}

# ── Rubric ────────────────────────────────────────────────────────────────────
# Edit READER_PROFILE / RUBRIC freely, then bump RUBRIC_VERSION so cached scores
# from the old rubric are discarded rather than competing with new ones.

READER_PROFILE = (
    "The reader is Adaner Usmani: a sociologist working in the analytical-Marxist "
    "tradition on class formation and democracy, punishment and mass incarceration, "
    "inequality and development (South Asia especially), historical and comparative "
    "political economy, and causal inference / Bayesian process tracing. He wants "
    "arguments, not takes; evidence, not vibes. He enjoys being disagreed with "
    "intelligently, including from the right, and is bored by pieces that merely "
    "confirm a left consensus. He reads the LRB, NLR, NYRB, Chartbook, and academic "
    "social science; he does not need the news explained to him. You may be given "
    "only an excerpt of a paywalled piece; judge what is there."
)

WEIGHTS = {"argument": 1.0, "relevance": 1.0, "rigor": 1.0, "durability": 1.0}

RUBRIC = (
    "You score one piece of writing for this reader. Score each dimension 0-10.\n\n"
    "ARGUMENT — 10: a thesis with a mechanism that could change how a careful reader "
    "thinks. 5: a competent restatement of a known position. 0: news summary, roundup, "
    "announcement.\n"
    "RELEVANCE — 10: squarely on the reader's interests above, or a smart challenge to "
    "them. 5: adjacent (general politics, economics, history, ideas). 0: unrelated "
    "(celebrity, sport, product reviews, personal life).\n"
    "RIGOR — 10: a careful empirical or analytical case that engages the strongest "
    "counterargument. 5: plausible but assertive. 0: vibes.\n"
    "DURABILITY — 10: still worth reading in a month. 5: a good take on this week's "
    "news. 0: obsolete by Friday.\n\n"
    "DISQUALIFY (set disqualified=true; the scores are then ignored): podcast or video "
    "episode notices; event, fundraising or subscription promos; 'what I'm reading' "
    "link lists and open threads; paywall stubs under ~200 words with no discernible "
    "argument; poetry and fiction; letters to the editor; job postings.\n\n"
    "Return ONLY a JSON object, no markdown fences:\n"
    '{"argument": 0-10, "relevance": 0-10, "rigor": 0-10, "durability": 0-10, '
    '"disqualified": true|false, "reason": "one sentence on what the piece argues '
    'and what makes it worth, or not worth, reading — written as a neutral blurb, '
    'never addressing or naming the reader"}'
)

SYSTEM_PROMPT = READER_PROFILE + "\n\n" + RUBRIC

# ── State ─────────────────────────────────────────────────────────────────────
# {"scores": {url: {...}}, "saved": {url: {date, source, title_key}}, "picks": {date: [...]}}

_state_lock = threading.Lock()

def _empty_state():
    return {"scores": {}, "saved": {}, "picks": {}}

def load_state():
    if not os.path.exists(STATE_JSON):
        return _empty_state()
    try:
        with open(STATE_JSON, "r", encoding="utf-8") as f:
            state = json.load(f)
    except Exception as e:
        log.warning(f"Failed to load {STATE_JSON}: {e} — starting fresh")
        return _empty_state()
    for key in ("scores", "saved", "picks"):
        state.setdefault(key, {})
    today = datetime.date.today()
    def fresh(d, days):
        return d >= (today - datetime.timedelta(days=days)).isoformat()
    state["scores"] = {u: s for u, s in state["scores"].items()
                       if fresh(s.get("scored_on", ""), SCORES_RETENTION_DAYS)}
    state["saved"]  = {u: s for u, s in state["saved"].items()
                       if fresh(s.get("date", ""), SAVED_RETENTION_DAYS)}
    state["picks"]  = {d: p for d, p in state["picks"].items()
                       if fresh(d, PICKS_RETENTION_DAYS)}
    return state

def save_state(state):
    """Atomic-ish write. On Windows under Dropbox the target is briefly locked
    by the sync client after each write, so the rename is retried, and as a
    last resort the file is written in place."""
    import time
    os.makedirs(CACHE_DIR, exist_ok=True)
    with _state_lock:
        payload = json.dumps(state, indent=1, ensure_ascii=False)
        tmp = STATE_JSON + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(payload)
        for attempt in range(5):
            try:
                os.replace(tmp, STATE_JSON)
                return
            except PermissionError:
                time.sleep(0.5 * (attempt + 1))
        log.warning("Rename of state file kept failing (file locked?) — writing in place")
        with open(STATE_JSON, "w", encoding="utf-8") as f:
            f.write(payload)
        try:
            os.remove(tmp)
        except OSError:
            pass

# ── Identity ──────────────────────────────────────────────────────────────────

_TRACKING_PARAMS = re.compile(r"^(utm_|fbclid$|gclid$|ref$|mc_cid$|mc_eid$|source$)")

def canonical_url(url):
    """Stable key for a URL: drop fragment + tracking params, lowercase host,
    strip trailing slash. Used for scores/saved keys and the Instapaper POST."""
    if not url:
        return ""
    parts = urlsplit(url.strip())
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not _TRACKING_PARAMS.match(k)]
    path = parts.path.rstrip("/") or "/"
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), path,
                       urlencode(query), ""))

def title_key(source, title):
    return f"{source}::" + re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()

# ── Body text ─────────────────────────────────────────────────────────────────

def _truncate_words(text, max_words):
    words = text.split()
    if len(words) <= max_words:
        return text
    return " ".join(words[:max_words]) + " […]"

def fetch_body(url, max_words=BODY_MAX_WORDS):
    """Uncached article-text fetch (requests + trafilatura). Returns text or
    None. Separate from briefing.fetch_article_text, whose per-URL cache stores
    only the first 5 paragraphs."""
    try:
        resp = requests.get(url, headers={"User-Agent": ARTICLE_UA},
                            timeout=BODY_FETCH_TIMEOUT, allow_redirects=True)
        if resp.status_code != 200 or not resp.text:
            return None
        result = trafilatura.bare_extraction(
            resp.text, include_comments=False, include_tables=False,
            favor_precision=False,
        )
        text = getattr(result, "text", None) if result else None
        if not text or not text.strip():
            return None
        return _truncate_words(text.strip(), max_words)
    except Exception as e:
        log.debug(f"  body fetch failed for {url}: {e}")
        return None

def body_for(item):
    """Feed-supplied content if substantial, else a live fetch, else the RSS
    summary (marked thin)."""
    content = item.get("content") or ""
    if len(content.split()) >= MIN_FEED_CONTENT_WORDS:
        return _truncate_words(content, BODY_MAX_WORDS), False
    fetched = fetch_body(item["link"])
    if fetched and len(fetched.split()) >= 150:
        return fetched, False
    fallback = content or item.get("summary") or ""
    return fallback, True

# ── Scoring ───────────────────────────────────────────────────────────────────

def _parse_score_json(raw):
    raw = raw.strip()
    raw = re.sub(r"^```(?:json)?\s*", "", raw)
    raw = re.sub(r"\s*```$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        import json_repair
        return json_repair.loads(raw)

def _clamp(v):
    try:
        return max(0, min(10, int(round(float(v)))))
    except (TypeError, ValueError):
        return 0

def score_item(client, item):
    """One Sonnet call. Returns a score record or None (transient failure —
    the item stays unscored and is retried on the next run)."""
    body, thin = body_for(item)
    note = ("NOTE: only an excerpt or summary is available; judge what is here.\n"
            if thin else "")
    user = (
        f"SOURCE: {item['source']}\n"
        f"TITLE: {item['title']}\n"
        f"PUBLISHED: {item.get('published_iso') or 'unknown'}\n"
        f"{note}\n--- TEXT ---\n{body}\n--- END ---\n\n"
        "Return only the JSON object."
    )
    try:
        msg = client.messages.create(
            model=SCORE_MODEL,
            max_tokens=2000,
            system=[{"type": "text", "text": SYSTEM_PROMPT,
                     "cache_control": {"type": "ephemeral"}}],
            output_config={"effort": "low"},
            messages=[{"role": "user", "content": user}],
        )
    except anthropic.APIError as e:
        log.warning(f"  score failed for {item['title'][:60]}: {e}")
        return None
    text = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
    try:
        data = _parse_score_json(text)
        if not isinstance(data, dict):
            raise ValueError("not an object")
    except Exception as e:
        log.warning(f"  unparseable score for {item['title'][:60]}: {e}")
        return None
    dims = {k: _clamp(data.get(k)) for k in WEIGHTS}
    disqualified = bool(data.get("disqualified"))
    total = 0.0 if disqualified else sum(WEIGHTS[k] * dims[k] for k in WEIGHTS)
    return {
        "title":          item["title"],
        "source":         item["source"],
        "published_iso":  item.get("published_iso"),
        "first_seen":     item["first_seen"],
        "scored_on":      datetime.date.today().isoformat(),
        "rubric_version": RUBRIC_VERSION,
        "model":          SCORE_MODEL,
        "total":          total,
        "dims":           dims,
        "disqualified":   disqualified,
        "thin":           thin,
        "reason":         str(data.get("reason", "")).strip(),
    }

def score_items(items, state, client):
    """Score in parallel, writing to state every 10 completions."""
    done = 0
    def _go(it):
        return it, score_item(client, it)
    with concurrent.futures.ThreadPoolExecutor(max_workers=SCORE_WORKERS) as ex:
        for it, rec in ex.map(_go, items):
            if rec is None:
                continue
            with _state_lock:
                state["scores"][it["key"]] = rec
            done += 1
            if done % 10 == 0:
                save_state(state)
                log.info(f"  scored {done}/{len(items)}")
    save_state(state)
    log.info(f"Scored {done}/{len(items)} new items")
    return done

# ── Selection ─────────────────────────────────────────────────────────────────

def _effective_date(rec):
    return (rec.get("published_iso") or rec.get("first_seen") or "")[:10]

def select_picks(state, today):
    cutoff = (datetime.date.fromisoformat(today)
              - datetime.timedelta(days=WINDOW_DAYS)).isoformat()
    week_ago = cutoff
    saved_by_source = {}
    for s in state["saved"].values():
        if s.get("date", "") >= week_ago:
            saved_by_source[s.get("source")] = saved_by_source.get(s.get("source"), 0) + 1

    candidates = []
    for url, rec in state["scores"].items():
        if url in state["saved"]:
            continue
        if rec.get("disqualified") or rec.get("rubric_version") != RUBRIC_VERSION:
            continue
        if _effective_date(rec) < cutoff:
            continue
        candidates.append((url, rec))
    # Highest total first; among equals, newest first.
    def _ordinal(rec):
        try:
            return datetime.date.fromisoformat(_effective_date(rec)).toordinal()
        except ValueError:
            return 0
    candidates.sort(key=lambda ur: (-ur[1]["total"], -_ordinal(ur[1])))

    picks, taken_today = [], {}
    for url, rec in candidates:
        src = rec["source"]
        if taken_today.get(src, 0) >= MAX_PER_SOURCE_PER_DAY:
            continue
        if saved_by_source.get(src, 0) >= MAX_PER_SOURCE_PER_WEEK:
            continue
        picks.append({
            "url":    url,
            "title":  rec["title"],
            "source": src,
            "total":  rec["total"],
            "dims":   rec["dims"],
            "reason": rec["reason"],
        })
        taken_today[src] = taken_today.get(src, 0) + 1
        if len(picks) >= PICKS_PER_DAY:
            break
    return picks, candidates

def log_ranking(candidates, picks, n=20):
    chosen = {p["url"] for p in picks}
    log.info(f"Top {min(n, len(candidates))} of {len(candidates)} candidates "
             f"(* = picked; A/R/G/D = argument/relevance/rigor/durability):")
    for url, rec in candidates[:n]:
        d = rec["dims"]
        mark = "*" if url in chosen else " "
        log.info(f"  {mark} {rec['total']:4.0f}  A{d['argument']:<2}R{d['relevance']:<2}"
                 f"G{d['rigor']:<2}D{d['durability']:<2}  {rec['source'][:22]:22s}  "
                 f"{rec['title'][:70]}")
        log.info(f"         {rec['reason'][:160]}")

# ── Instapaper ────────────────────────────────────────────────────────────────

def instapaper_check():
    if not INSTAPAPER_USER:
        raise RuntimeError("INSTAPAPER_USER not set")
    r = requests.post(f"{INSTAPAPER_API}/authenticate",
                      auth=(INSTAPAPER_USER, INSTAPAPER_PASSWORD), timeout=20)
    if r.status_code != 200:
        raise RuntimeError(f"Instapaper authentication failed (HTTP {r.status_code})")
    log.info("Instapaper credentials OK")

def instapaper_add(url, title):
    """POST to the Simple API. True on 201. Retries a 5xx once; 403 is fatal."""
    for attempt in (1, 2):
        r = requests.post(f"{INSTAPAPER_API}/add",
                          auth=(INSTAPAPER_USER, INSTAPAPER_PASSWORD),
                          data={"url": url, "title": title}, timeout=20)
        if r.status_code == 201:
            return True
        if r.status_code == 403:
            raise RuntimeError("Instapaper rejected credentials (403)")
        log.warning(f"  Instapaper add returned {r.status_code} for {url} (attempt {attempt})")
        if r.status_code < 500:
            return False
    return False

# ── Main ──────────────────────────────────────────────────────────────────────

def run(dry_run=False, limit=None):
    today = datetime.date.today().isoformat()
    log.info(f"=== Reader --- {today}{' (dry run)' if dry_run else ''} ===")

    state = load_state()
    if not dry_run and today in state["picks"]:
        log.info(f"Picks for {today} already exist ({len(state['picks'][today])} items) "
                 "— nothing to do. Delete state['picks'][today] to force a rerun.")
        return

    # Fail fast on bad credentials before spending anything on scoring.
    if not dry_run:
        instapaper_check()

    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"),
                                 max_retries=6, timeout=60)

    items = fetch_headlines(feeds=READER_FEEDS, hours_back=HOURS_BACK)
    if not items:
        log.error("No items fetched. Aborting.")
        return

    # Canonicalise, dedup within the fetch, drop saved and already-scored.
    saved_titles = {s.get("title_key") for s in state["saved"].values()}
    seen_keys, new_items = set(), []
    for it in items:
        key = canonical_url(it.get("link"))
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        if key in state["saved"] or title_key(it["source"], it["title"]) in saved_titles:
            continue
        prior = state["scores"].get(key)
        if prior and prior.get("rubric_version") == RUBRIC_VERSION \
                 and prior.get("model") == SCORE_MODEL:
            continue
        it["key"] = key
        it["first_seen"] = (prior or {}).get("first_seen") or today
        new_items.append(it)
    log.info(f"{len(items)} fetched → {len(new_items)} new to score "
             f"({len(state['scores'])} cached scores, {len(state['saved'])} saved)")

    if limit is not None:
        new_items = new_items[:limit]
        log.info(f"--limit: scoring only {len(new_items)}")

    if new_items:
        score_items(new_items, state, client)

    picks, candidates = select_picks(state, today)
    log_ranking(candidates, picks)
    if len(picks) < PICKS_PER_DAY:
        log.warning(f"Only {len(picks)} eligible picks (wanted {PICKS_PER_DAY})")

    if dry_run:
        log.info("Dry run — nothing sent to Instapaper, picks not recorded.")
        return

    saved = []
    for p in picks:
        if instapaper_add(p["url"], p["title"]):
            state["saved"][p["url"]] = {
                "date": today, "source": p["source"],
                "title_key": title_key(p["source"], p["title"]),
            }
            saved.append(p)
            state["picks"][today] = saved
            save_state(state)   # flush after each success — no duplicates on crash
            log.info(f"  saved: [{p['source']}] {p['title'][:70]}")
        else:
            log.error(f"  NOT saved: {p['url']}")
    state["picks"][today] = saved
    save_state(state)
    log.info(f"Done — {len(saved)} items sent to Instapaper.")

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Reader — daily Instapaper queue")
    p.add_argument("--dry-run", action="store_true",
                   help="Score and select but do not save to Instapaper or record picks")
    p.add_argument("--limit", type=int, default=None,
                   help="Score at most N new items (for cheap local testing)")
    args = p.parse_args()
    run(dry_run=args.dry_run, limit=args.limit)
