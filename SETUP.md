# Daily News Briefing — Windows Setup Guide

## What you'll end up with

Every day at noon, your PC will automatically:
1. Pull headlines from BBC, Al Jazeera, NYT, Dawn, Reuters, Guardian, and Fox News
2. Send them to Claude, which writes a sharp, Economist-style digest
3. Email it to your inbox as a beautifully formatted HTML email

Total cost: ~$0.10–0.30/month (just the Claude API — email is free via Gmail)

---

## Step 1 — Install Python

1. Go to https://www.python.org/downloads/
2. Download the latest Python 3.x installer
3. **Important:** On the first screen of the installer, check **"Add Python to PATH"**
4. Click "Install Now"
5. Verify it worked: open Command Prompt and type `python --version`

---

## Step 2 — Get your Anthropic API key

1. Go to https://console.anthropic.com
2. Sign in (or create a free account)
3. Click **API Keys** in the left sidebar → **Create Key**
4. Copy the key — it starts with `sk-ant-`
5. Add $5 of credit under **Billing** (this will last months at daily usage)

---

## Step 3 — Set up a Gmail App Password

You need this so the script can send email on your behalf without using your real password.

1. Make sure your Gmail account has **2-Step Verification** enabled:
   https://myaccount.google.com/security

2. Go to: https://myaccount.google.com/apppasswords

3. Under "App name", type `News Briefing` → click **Create**

4. Google will show you a 16-character password like `abcd efgh ijkl mnop`
   **Copy it now** — you won't see it again. Remove the spaces when you paste it into `.env`.

---

## Step 4 — Set up the project folder

Open **Command Prompt** (press `Win + R`, type `cmd`, press Enter) and run:

```
mkdir C:\NewsAgent
cd C:\NewsAgent
```

Copy `briefing.py`, `requirements.txt`, and `.env.example` into `C:\NewsAgent\`.

Then run:
```
python -m pip install -r requirements.txt
```

---

## Step 5 — Configure your .env file

In `C:\NewsAgent\`, copy `.env.example` to `.env`:
```
copy .env.example .env
```

Open `.env` in Notepad:
```
notepad .env
```

Fill in:
- `ANTHROPIC_API_KEY` — your key from Step 2
- `SMTP_USER` — your Gmail address
- `SMTP_PASSWORD` — the App Password from Step 3 (no spaces)
- `EMAIL_TO` — where to send it (can be any email address)

Save and close.

---

## Step 6 — Test it manually

In Command Prompt:
```
cd C:\NewsAgent
python briefing.py
```

You should see logs printing for ~30 seconds, ending with `✅ Done!`
Check your inbox — the email should arrive within a minute.

If you see an error, check the **Troubleshooting** section below.

---

## Step 7 — Schedule it to run at noon every day

Windows has a built-in task scheduler. Here's the easiest way to set it up:

1. Press `Win + S` and search for **Task Scheduler** → open it
2. In the right panel, click **Create Basic Task...**
3. Fill in:
   - **Name:** `Daily News Briefing`
   - **Trigger:** Daily
   - **Start time:** 12:00 PM
   - **Action:** Start a program
   - **Program/script:** `C:\Users\YOUR_USERNAME\AppData\Local\Programs\Python\Python3xx\python.exe`
     (replace `Python3xx` with your actual version, e.g. `Python312`)
   - **Add arguments:** `briefing.py`
   - **Start in:** `C:\NewsAgent`
4. Click **Finish**

**Tip — find your Python path:** In Command Prompt, type `where python` and use that path.

**Alternative (easier):** Create a `.bat` file in `C:\NewsAgent\run.bat`:
```bat
@echo off
cd /d C:\NewsAgent
python briefing.py >> C:\NewsAgent\briefing.log 2>&1
```
Then schedule `run.bat` instead of python directly — it also logs output for debugging.

---

## Reader (Instapaper queue)

`reader.py` runs alongside the briefing and saves the day's five best essays to
Instapaper. It needs two more lines in `.env`:

- `INSTAPAPER_USER` — your Instapaper email or username
- `INSTAPAPER_PASSWORD` — your Instapaper password (leave empty if the account has none)

Test it without sending anything: `python reader.py --dry-run --limit 15`.
In GitHub Actions, add the same two values as repository secrets.

---

## Troubleshooting

| Error | Fix |
|---|---|
| `python is not recognized` | Python wasn't added to PATH. Reinstall and check "Add to PATH" |
| `ModuleNotFoundError: feedparser` | Run `pip install -r requirements.txt` again |
| `SMTPAuthenticationError` | Your App Password is wrong. Re-generate it in Google settings |
| `SMTPException: STARTTLS` | Your network may block port 587 — try port 465 and `SMTP_SSL` |
| `JSONDecodeError` | Claude returned unexpected output — run again, it's usually a one-off |
| Email goes to spam | Add your own Gmail to your contacts, or use a service like Mailgun |

---

## Customizing

Open `briefing.py` in any text editor (Notepad, VS Code, etc.):

- **Change number of stories:** edit `MAX_ITEMS_PER_FEED = 8`
- **Make it shorter/longer:** edit `TARGET_WORD_COUNT = 750`
- **Add/remove sources:** edit the `RSS_FEEDS` dictionary at the top
- **Change editorial focus:** edit the `SYSTEM_PROMPT` string — e.g. add "Always include at least one story about technology"

---

## Adding audio later (ElevenLabs)

When you're ready to add audio, you'll just need to:
1. Sign up at https://elevenlabs.io ($5/month Starter plan)
2. Add `ELEVENLABS_API_KEY` and `ELEVENLABS_VOICE_ID` to your `.env`
3. Add the audio generation step to `briefing.py` (a ~20-line addition)

The transcript text is already generated — audio is just one more step piped in at the end.
