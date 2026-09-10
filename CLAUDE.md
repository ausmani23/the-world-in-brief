# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Two daily pipelines, one GitHub Actions job:

- `briefing.py` — fetches RSS headlines, curates them with Claude, and emails an Economist-style HTML digest.
- `reader.py` — scans ~60 essay sites and Substacks, scores each piece on a rubric tuned to Adaner's interests, saves the top 5 to Instapaper, and hands the picks to `briefing.py` for a "Worth Your Time" section in the email.

Editorial lens is materialist — attentive to economic forces, class interests, social movements, and power.

## Commands

```bash
pip install -r requirements.txt

python briefing.py             # fetch RSS, call Claude, send email
python briefing.py --dry-run   # full pipeline, saves HTML to cache/briefing.html instead of emailing
python briefing.py --cached    # re-render HTML from cache/briefing.json, no fetch or API calls

python reader.py               # score, select, save 5 to Instapaper, record picks
python reader.py --dry-run     # score + select + log ranking; no Instapaper, picks not recorded
python reader.py --limit 15    # score at most 15 new items (cheap local test)
```

There are no tests or linting configured. Verify with `python -m py_compile briefing.py reader.py`. Scratch scripts and session notes live in `claude_workspace/` (gitignored).

## Architecture

### briefing.py — linear four-step pipeline

1. **Fetch** (`fetch_headlines(feeds, hours_back)`): RSS via `feedparser` from ~25 global sources (English + Arabic, Spanish, French, German, Persian, Portuguese, Chinese, Hindi), plus `fetch_scraped_items` for homepage-scraped sources. Items carry `published` (display), `published_iso`, `content` (feed-supplied full text, if any). Items without a timestamp are treated as recent.
2. **Pre-screen** (`prescreen_items`): Claude Haiku picks 40-50 headlines; `enforce_language_floor` guarantees 30% non-English.
3. **Synthesize** (`synthesize_briefing`): Claude Opus writes structured JSON (lede, stories with footnoted sentences, state-media and right-wing sections). `json_repair` fallback.
4. **Email** (`build_html(briefing, date_str, picks)` + `send_email`): styled HTML, region tags, superscript footnotes. `picks` (from `load_reader_picks()`) adds the gold "Worth Your Time" list; `None` omits it.

### reader.py — score, select, save

1. **Fetch** 7 days from `READER_FEEDS` (six grouped dicts) via `briefing.fetch_headlines(hours_back=168)`.
2. **Canonicalise + dedup**: `canonical_url` strips tracking params; skip anything in `state["saved"]` (by URL or `(source, title)`), reuse cached scores.
3. **Score** each new piece once with `claude-sonnet-5` (effort low, cached system prompt, 6 threads). Body = feed content if ≥400 words, else `fetch_body` (uncached trafilatura), else summary marked `thin`. Rubric: `argument`, `relevance`, `rigor`, `durability` (0-10 each, summed via `WEIGHTS`); `disqualified` zeroes the score.
4. **Select** exactly `PICKS_PER_DAY` from the rolling 7-day pool: highest total, newest tiebreak, max 1 per source per day and 2 per source per week.
5. **Save** via Instapaper Simple API (`/api/authenticate` at startup, `/api/add` per pick, 201 = saved); state flushed after each success.

State: `cache/reader_state.json` = `{scores, saved, picks}` with pruning on load. `picks[today]` already present ⇒ the run is a no-op.

## Key Design Decisions

- **Two-pass Claude pipeline in the briefing**: Haiku filters cheaply, Opus synthesises.
- **Reader scores everything with one model, no prescreen**: the weekly digest's Haiku title-only prescreen is what let Jacobin's volume dominate. Per-source caps are enforced in code, not the prompt.
- **Rubric is data**: `READER_PROFILE`, `RUBRIC`, `WEIGHTS` are constants. After editing them bump `RUBRIC_VERSION` so cached scores are discarded.
- **Reader never blocks the briefing**: it runs first with `continue-on-error`; the briefing reads picks from a file and renders nothing if absent. `briefing.py` must never import `reader.py` (circular).
- **Non-English language floor**, **JSON output**, **footnote citation style**, **state media included deliberately** — unchanged from before.

## Deployment

- `.github/workflows/briefing.yml` runs daily at 16:30 UTC: reader step, then briefing step. `workflow_dispatch` `mode=test` sends the email to `EMAIL_DEV` and runs the reader with `--dry-run`. **DST**: cron `30 16` ⇄ `30 17` in November/March.
- State persists via the `briefing-cache` artifact (`cache/briefing_*.json`, `cache/reader_state.json`), downloaded with `workflow_conclusion: ""` so a failed briefing step doesn't roll the reader state back.
- `.github/workflows/keepalive.yml` pushes a monthly empty commit so GitHub doesn't disable the cron.
- **GitHub Secrets**: `ANTHROPIC_API_KEY`, `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_TO`, `EMAIL_DEV`, `SMTP_HOST`, `SMTP_PORT`, `INSTAPAPER_USER`, `INSTAPAPER_PASSWORD`.

## Environment Variables

Configured via `.env` (see `.env.example`). Required: `ANTHROPIC_API_KEY`, `SMTP_USER`, `SMTP_PASSWORD`, `EMAIL_TO`, and for the reader `INSTAPAPER_USER`, `INSTAPAPER_PASSWORD`. Optional: `SMTP_HOST`, `SMTP_PORT`.
