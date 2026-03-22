# The World in Brief

A personal daily news briefing, delivered to your inbox every morning. Modelled on The Economist's *The World in Brief* in form — terse, precise, globally minded — but with a materialist editorial lens: attentive to economic forces, class interests, social movements, and the contestation of power.

## What it does

Every day at 10:30am EDT, a GitHub Actions workflow:

1. **Fetches** all articles published in the last 24 hours from 14 global news sources
2. **Pre-screens** with Claude Haiku — selects the 40-50 most newsworthy headlines, balancing importance and geographic breadth
3. **Synthesises** with Claude Opus — writes 9-12 stories with per-sentence law-review-style footnotes linking back to original sources
4. **Emails** a formatted HTML digest to one or more recipients

## Sources

| Region | Sources |
|---|---|
| Global | BBC World, Al Jazeera, NYT World, The Guardian, FT |
| South Asia | Dawn (Pakistan) |
| Middle East | Middle East Eye |
| Europe | BBC Europe, Deutsche Welle, Le Monde (English) |
| Latin America | Mercopress |
| Africa | Africanews |
| East / Southeast Asia | Nikkei Asia, SCMP |

## Editorial voice

The briefing is written with a materialist sensibility. Analysis of economic forces, class interests, and power is woven into the prose rather than announced. Social movements, strikes, protests, and popular mobilisations are treated as historical forces, not merely disruptions. The goal is the analytical depth of *The Economist* with the global breadth of *Al Jazeera* — and an eye on the things that a Marxist would notice.

## Architecture

```
GitHub Actions (cron: daily)
    │
    ├── feedparser: fetch RSS from 14 sources (last 24h only)
    │
    ├── Claude Haiku: pre-screen 300+ headlines → select 40-50
    │   (balance: recency, importance, geographic breadth,
    │    max 5 per source)
    │
    ├── Claude Opus: write 9-12 stories (~120 words each)
    │   (materialist lens, law-review footnotes per sentence)
    │
    └── Gmail SMTP: send HTML email to recipients
```

## Setup

### Requirements

- Python 3.12+
- Anthropic API key (console.anthropic.com) — ~$0.20/day
- Gmail account with App Password enabled

### Installation

```bash
git clone https://github.com/YOUR_USERNAME/the-world-in-brief
cd the-world-in-brief
pip install -r requirements.txt
cp .env.example .env
# fill in .env with your API keys
python briefing.py
```

### Environment variables

| Variable | Description |
|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key |
| `SMTP_USER` | Gmail address used to send |
| `SMTP_PASSWORD` | Gmail App Password (not your real password) |
| `EMAIL_TO` | Recipient address(es), comma-separated |
| `SMTP_HOST` | SMTP host (default: smtp.gmail.com) |
| `SMTP_PORT` | SMTP port (default: 587) |

### Automated delivery via GitHub Actions

The workflow file at `.github/workflows/briefing.yml` runs the script on a cron schedule. API keys are stored as GitHub repository secrets — never in code.

To change the delivery time, edit the cron line in `briefing.yml`. Times are in UTC:
- 10:30am EDT (summer): `30 14 * * *`
- 10:30am EST (winter): `30 15 * * *`

To trigger a manual run: **Actions** → **Daily News Briefing** → **Run workflow**.

## Cost

| Service | Cost |
|---|---|
| Anthropic API (~$0.20/run) | ~$6/month |
| GitHub Actions | Free |
| Gmail SMTP | Free |
| **Total** | **~$6/month** |
