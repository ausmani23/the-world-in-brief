# The World in Brief

A personal news briefing, delivered to my inbox. Two cadences:

- **Daily** — modelled on The Economist's *The World in Brief* in form: terse, precise, globally minded. ~10 stories synthesising the past 24 hours of headlines from a wide range of global outlets.
- **Weekly (Sunday)** — a longer, more reflective companion: themed paragraphs weaving together human rights reports, long-form essays, and political-economy substacks from the past week, ending with five must-reads.

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
4. **Emails** a formatted HTML digest to recipients (BCC).

The exact source list lives in `briefing.py` — `RSS_FEEDS`, `SCRAPE_SOURCES`, `RIGHT_WING_FEEDS`, `STATE_MEDIA_SOURCES`.

## Weekly digest (Sunday)

The workflow at `.github/workflows/weekly.yml` runs `weekly.py`, which:

1. **Fetches** the last 7 days from three feed groups:
   - Human rights / accountability orgs (Amnesty, HRW, MSF, ICRC, Crisis Group, CPJ, Article 19)
   - Long-form journals (LRB, NYRB, NLR, Sidecar, Jacobin, Dissent, n+1, Boston Review, The Baffler, Phenomenal World, Verso)
   - Political-economy Substacks (Chartbook, The Overshoot, BIG, Apricitas, Polycrisis, Konczal, DeLong, Milanović, Varoufakis, Origins of Our Time)
2. **Dedups** against `cache/weekly_seen.json` so an item is never sent twice.
3. **Pre-screens** with Haiku, then **synthesises** with Opus into 4-6 themes (each a paragraph weaving multiple items via inline `(Lastname, Abbrev)` hyperlinked citations) plus 5 must-reads.
4. **Emails** to the weekly subscriber list (separate from daily).

## Running locally

```bash
pip install -r requirements.txt
cp .env.example .env  # then fill in keys

python briefing.py             # daily — fetches, calls Claude, sends email
python briefing.py --dry-run   # full pipeline but writes cache/briefing.html, no email
python briefing.py --cached    # re-render HTML from cache/briefing.json (no fetch / API)

python weekly.py               # weekly equivalents — same flags
python weekly.py --dry-run
python weekly.py --cached
```

## Environment variables

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key (~$0.20/day for daily, similar for weekly) |
| `SMTP_USER` | Gmail address used to send |
| `SMTP_PASSWORD` | Gmail App Password (not your real password) |
| `EMAIL_TO` | Daily recipients, comma-separated |
| `EMAIL_WEEKLY` | Weekly recipients, comma-separated (separate list from `EMAIL_TO`) |
| `EMAIL_DEV` | Test-mode recipient — used when running `workflow_dispatch` with mode=test |
| `SMTP_HOST` | Default `smtp.gmail.com` |
| `SMTP_PORT` | Default `587` |

In GitHub Actions these are stored as repository secrets.

## Schedule

| Workflow | Cron (UTC) | Local |
|---|---|---|
| Daily briefing | `30 16 * * *` | 12:30pm EDT |
| Weekly digest | `0 18 * * 0` | 2:00pm EDT, Sundays |

DST shifts: when clocks go back in November, bump each cron up an hour (`30 16` → `30 17` for daily; `0 18` → `0 19` for weekly). When they go forward in March, revert.

To trigger a manual run: **Actions** → pick the workflow → **Run workflow**. Both workflows expose a `mode` input — `test` sends to `EMAIL_DEV` only, `production` sends to the real list.

## Cost

| Service | Cost |
|---|---|
| Anthropic API (daily + weekly) | ~$8/month |
| GitHub Actions | Free |
| Gmail SMTP | Free |
| **Total** | **~$8/month** |
