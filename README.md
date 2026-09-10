# The World in Brief

A personal news briefing, delivered to my inbox, plus a daily reading queue pushed to Instapaper.

- **Daily briefing** — modelled on The Economist's *The World in Brief* in form: terse, precise, globally minded. ~10 stories synthesising the past 24 hours of headlines from a wide range of global outlets.
- **Reader** — every day, scans ~60 essay sites and Substacks, scores each piece on a rubric tuned to my interests, and saves the top 5 to Instapaper. The five also appear as a "Worth Your Time" section at the bottom of the daily email.

Editorial lens is materialist: attentive to economic forces, class interests, social movements, and power.

## Daily briefing

Every day, the workflow at `.github/workflows/briefing.yml` runs `briefing.py`, which:

1. **Fetches** items from the last 24 hours across:
   - ~25 global RSS feeds (English, Arabic, Spanish, French, German, Persian, Portuguese, Chinese, Hindi)
   - ~25 homepage-scraped sources whose RSS is broken or absent (extracted via trafilatura + htmldate)
   - A separate pool of right-wing feeds (Fox, Breitbart, WSJ Opinion, etc.)
   - A separate pool of state-media feeds (RT, CGTN, Global Times)
2. **Pre-screens** with Claude Haiku — picks the 40-50 most newsworthy headlines from the main pool, with a 30% non-English language floor enforced both in the prompt and programmatically.
3. **Synthesises** with Claude Opus — 9-12 stories, ~120 words each, with per-sentence law-review-style footnotes linking back to original sources. Plus two distinct sections: *The View From Russia, China and Iran* (state media perspective) and *The View From the Right*.
4. **Emails** a formatted HTML digest to recipients (BCC), appending today's reader picks if `reader.py` ran first.

The exact source list lives in `briefing.py` — `RSS_FEEDS`, `SCRAPE_SOURCES`, `RIGHT_WING_FEEDS`, `STATE_MEDIA_SOURCES`.

## Reader (daily Instapaper queue)

The same workflow runs `reader.py` immediately before the briefing. It:

1. **Fetches** the last 7 days from ~60 feeds in six groups (journals, political economy, punishment/criminal justice, contrarian-and-smart across the spectrum, social science and methods, world/China/South Asia). The list is `READER_FEEDS` in `reader.py`.
2. **Scores** every piece it hasn't scored before with Claude Sonnet, reading up to ~1,500 words of the body. Four dimensions, each 0-10, summed: *argument*, *relevance*, *rigor*, *durability*. Podcast notices, promos, link lists, paywall stubs, poetry and letters are disqualified outright. The rubric and reader profile are constants at the top of `reader.py` — edit them and bump `RUBRIC_VERSION` so old scores are discarded.
3. **Selects** exactly 5 from everything unsaved in the rolling 7-day pool: highest score first, at most 1 per source per day and 2 per source per week (so no single high-volume source can dominate).
4. **Saves** each to Instapaper via the Simple API and records the day's picks in `cache/reader_state.json`, which `briefing.py` reads to render the "Worth Your Time" section.

State (`scores`, `saved`, `picks`) persists across runs via the same GitHub artifact as the briefing cache. A second run on the same day is a no-op. Scores are cached, so a piece is only ever read once.

## Running locally

```bash
pip install -r requirements.txt
cp .env.example .env  # then fill in keys

python briefing.py             # daily — fetches, calls Claude, sends email
python briefing.py --dry-run   # full pipeline but writes cache/briefing.html, no email
python briefing.py --cached    # re-render HTML from cache/briefing.json (no fetch / API)

python reader.py               # score, select, save 5 to Instapaper
python reader.py --dry-run     # score + select + print ranking; nothing sent, picks not recorded
python reader.py --limit 15    # score at most 15 new items (cheap testing)
```

## Environment variables

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `SMTP_USER` | Gmail address used to send |
| `SMTP_PASSWORD` | Gmail App Password (not your real password) |
| `EMAIL_TO` | Daily recipients, comma-separated |
| `EMAIL_DEV` | Test-mode recipient — used when running `workflow_dispatch` with mode=test |
| `INSTAPAPER_USER` | Instapaper login (email) |
| `INSTAPAPER_PASSWORD` | Instapaper password |
| `SMTP_HOST` | Default `smtp.gmail.com` |
| `SMTP_PORT` | Default `587` |

In GitHub Actions these are stored as repository secrets.

## Schedule

| Workflow | Cron (UTC) | Local |
|---|---|---|
| Daily briefing (reader + briefing) | `30 16 * * *` | 12:30pm EDT |

DST shift: when clocks go back in November, bump the cron up an hour (`30 16` → `30 17`). When they go forward in March, revert.

To trigger a manual run: **Actions** → **Daily News Briefing** → **Run workflow**. The `mode` input — `test` sends the email to `EMAIL_DEV` only and runs the reader as a dry run (nothing sent to Instapaper); `production` sends to the real list and saves to Instapaper.

## Cost

| Service | Cost |
|---|---|
| Anthropic API — briefing (Haiku + Opus) | ~$6/month |
| Anthropic API — reader (Sonnet, ~50 pieces/day) | ~$5-8/month |
| GitHub Actions | Free |
| Gmail SMTP | Free |
| **Total** | **~$12-15/month** |
