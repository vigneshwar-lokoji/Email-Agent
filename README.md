# Inbox Agent

An AI-powered email assistant that monitors your Gmail inbox in real-time, classifies emails, drafts context-aware replies, and manages your job search pipeline — all controlled through Telegram.

Built with LangGraph (multi-agent orchestration), Gemini 2.5 Flash (LLM), Google Sheets (tracker), and Telegram Bot API.

## What It Does

- **Real-time email monitoring** via Gmail Pub/Sub push notifications
- **Smart classification** into job / personal / business / bank / study / advertisement / spam
- **Multi-agent pipeline** for job emails: triage → thread resolver → data extractor → sheet operator → drafter → critic
- **Draft replies** with tone matching, calendar-aware scheduling, and profile injection
- **Google Sheets tracker** automatically logs companies, stages, rejections, and follow-ups
- **Telegram bot** for approving, editing, or writing your own replies
- **Reminder system** for actionable emails with priority levels
- **Follow-up detection** finds stale threads where you replied but got no response
- **Daily morning brief** with pending reminders, weekly analytics, calendar, and pipeline stats
- **Job search dashboard** with application pipeline, rejection analysis, and stage tracking
- **Response time analytics** tracking how fast you handle emails

## Architecture

```
Gmail Inbox
    │
    ▼ (Pub/Sub push)
┌─────────────┐
│   main.py   │ ← Entry point: Pub/Sub listener + Telegram poller + background loops
└──────┬──────┘
       │
       ▼
┌──────────────────┐     ┌─────────────────┐
│  orchestrator.py │────▶│   ai_nodes.py   │  Gemini 2.5 Flash (Vertex AI / API)
│  (Phase A + B)   │     │  6 LLM agents   │
└──────┬───────────┘     └─────────────────┘
       │
       ├──▶ Google Sheets (job tracker + profile)
       ├──▶ Gmail API (send replies, save drafts, apply labels)
       ├──▶ SQLite (pending decisions, reminders, sent replies)
       └──▶ Telegram Bot (notifications + approval buttons)
```

**Phase A** (non-blocking): New email arrives → classify → extract data → update sheet → send Telegram notification. All emails processed in batch without waiting for user input.

**Phase B** (async): User taps a button on Telegram → agent drafts reply / sends / saves draft / ignores. Each email is handled independently.

## LLM Agents

| Agent | Purpose |
|-------|---------|
| Super Triage | Classify email category (job/personal/bank/advertisement/spam) |
| Job Triage | Determine if job-related email needs action |
| Thread Resolver | Match to existing tracker row or create new entry |
| Data Extractor | Pull structured fields (company, stage, action type, priority) |
| Drafter | Write context-aware reply using profile + calendar data |
| Critic | Review draft against tone/content rules, request rewrites |
| General Triage | For non-job emails: needs_reply / needs_attention / spam |
| General Drafter | Draft replies for personal/business/study emails |

## Telegram Commands

| Command | What it does |
|---------|-------------|
| `reminders` | Show pending action items with priority |
| `clear reminders` | Dismiss all reminders at once |
| `followup` | Check for stale threads (sent but no reply) |
| `dashboard` / `stats` | Job search pipeline and rejection analysis |
| `response time` / `speed` | Your email response speed stats |
| `brief` / `morning` | Daily summary (reminders + weekly analytics) |
| `profile` | View your profile data from Google Sheet |
| `help` | List all commands |

## Email Notification Buttons

When a new email arrives that needs your attention:

```
[Show Email]           ← View the full email
[Draft a Reply for me] ← AI writes a reply
[Write My Own]         ← Type your own reply (with optional refine loop)
[Ignore]               ← Dismiss
```

After AI drafts a reply:

```
[Approve & Send] [Edit] [Write My Own] [Hold as Draft] [Reject]
```

## Prerequisites

- Python 3.11+
- Google Cloud project with these APIs enabled:
  - Gmail API
  - Google Sheets API
  - Google Drive API
  - Vertex AI API (if using Vertex AI backend)
  - Cloud Pub/Sub API
- Google Cloud service account with roles:
  - Vertex AI User (if using Vertex AI)
  - Pub/Sub Subscriber
  - Pub/Sub Publisher
- Gmail OAuth 2.0 credentials (for user's inbox access)
- Telegram Bot (created via [@BotFather](https://t.me/BotFather))
- Google Sheet named "AI Job Tracker"

## Setup

### 1. Clone and install

```bash
git clone https://github.com/yourusername/inbox-agent.git
cd inbox-agent
python -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

### 2. Google Cloud setup

#### Service Account
1. Go to [Google Cloud Console](https://console.cloud.google.com) → IAM & Admin → Service Accounts
2. Create a service account
3. Grant roles: `Vertex AI User`, `Pub/Sub Subscriber`, `Pub/Sub Publisher`
4. Create a JSON key → save as `service_account.json` in the project root

#### Gmail OAuth
1. Go to APIs & Services → Credentials → Create OAuth 2.0 Client ID
2. Application type: Desktop app
3. Download the JSON → save as `credentials.json`
4. Run the auth flow:
```bash
python auth.py
```
This opens a browser for Gmail authorization and creates `token.json`.

> **Important:** Publish your OAuth app to Production (APIs & Services → OAuth consent screen → Publish) to prevent token expiration every 7 days.

#### Pub/Sub
1. Create a topic: `gmail-push`
2. Create a subscription: `gmail-push-sub` (Pull type)
3. Grant `gmail-api-push@system.gserviceaccount.com` the Publisher role on the topic

### 3. Google Sheet

1. Create a Google Sheet named **"AI Job Tracker"**
2. Share it with your service account email (Editor access)
3. The agent auto-creates headers on first run
4. Run the profile setup:
```bash
python setup_profile.py
```
5. Fill in row 2 of the "Profile" tab with your data

### 4. Telegram Bot

1. Message [@BotFather](https://t.me/BotFather) → `/newbot` → follow prompts
2. Copy the bot token
3. Send any message to your bot, then get your chat ID:
```bash
curl https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
```
Look for `"chat":{"id": YOUR_CHAT_ID}` in the response.

### 5. Environment variables

```bash
cp .env.example .env
```

Edit `.env` with your values:
```
TELEGRAM_BOT_TOKEN=123456:ABC-DEF...
TELEGRAM_CHAT_ID=123456789
GOOGLE_CLOUD_PROJECT_ID=my-project-123
USER_TIMEZONE=America/New_York
USER_FIRST_NAME=John
```

### 6. LLM Backend

**Option A: Vertex AI** (recommended)
- Enable Vertex AI API in your GCP project
- Service account needs `Vertex AI User` role
- Set `USE_VERTEX_AI = True` in `ai_nodes.py`

**Option B: Gemini API Key** (simpler, no GCP needed for LLM)
- Get an API key from [Google AI Studio](https://aistudio.google.com/apikey)
- Add `GEMINI_API_KEY=your-key` to `.env`
- Set `USE_VERTEX_AI = False` in `ai_nodes.py`

### 7. Run

```bash
python main.py
```

This starts:
- Telegram long-polling listener
- Gmail Pub/Sub pull listener (in a background thread)
- Follow-up checker (every 6 hours)
- Response nudge (every 3 hours)
- Morning brief (daily at 8 AM in your timezone)

## File Structure

```
inbox-agent/
├── main.py              # Entry point — Telegram + Pub/Sub + background loops
├── orchestrator.py      # Phase A (classify) + Phase B (execute) logic
├── ai_nodes.py          # LLM agent functions (triage, extract, draft, critic)
├── prompts.py           # All LLM system prompts
├── graph.py             # LangGraph workflow definition
├── state.py             # Agent state schema
├── db.py                # SQLite layer (pending decisions, reminders, follow-ups)
├── gmail_service.py     # Gmail API helpers (fetch, send, label, draft)
├── gmail_watch.py       # Pub/Sub watch registration
├── calendar_service.py  # Google Calendar integration
├── action_nodes.py      # Google Sheets operations + dashboard stats
├── bot_listener.py      # Telegram button/command handler
├── auth.py              # OAuth flow for Gmail
├── setup_profile.py     # One-time Profile tab creator
├── server.py            # FastAPI server (alternative to polling)
├── test_bot.py          # Quick Telegram connectivity test
├── visualize.py         # LangGraph ASCII visualization
├── requirements.txt     # Python dependencies
├── .env.example         # Environment variable template
└── .gitignore
```

## Customization

### Adding email categories
Edit `SUPER_TRIAGE_PROMPT` in `prompts.py` to add new categories. Then add routing logic in `orchestrator.py` (`run_phase_a`) and a Gmail label in `_gmail_label_for()`.

### Changing the LLM model
In `ai_nodes.py`, change the `model` parameter in `ChatGoogleGenerativeAI`. The agent uses two instances: `llm_json` (structured output) and `llm_text` (free text).

### Customizing draft style
Edit `DRAFTER_PROMPT` in `prompts.py`. The drafter receives the user's profile data and calendar availability as context.

### Adding tracker columns
The extractor agent populates columns based on `EXTRACTOR_PROMPT` in `prompts.py`. Add new fields there, and update `sheet_operator_node` in `action_nodes.py` to write them.

### Changing the morning brief schedule
In `main.py`, modify `brief_hour` in `_morning_brief_loop()`. The timezone is set via `USER_TIMEZONE` env var.

### Running on a VM
For 24/7 operation, deploy to a cloud VM (e2-micro is sufficient):
```bash
# Start with nohup
nohup python3 -u main.py >> logs/agent.log 2>&1 &

# Or use systemd for auto-restart
```

## How the Job Pipeline Works

```
New job email arrives
    │
    ▼
Super Triage → "job" category
    │
    ▼
Job Triage → is it about YOUR application? (not a digest)
    │
    ▼
Thread Resolver → match to existing sheet row or create new
    │
    ▼
Data Extractor → company, stage, action type, priority
    │
    ▼
Sheet Operator → update Google Sheets tracker
    │
    ├── Action Type: "Reply" → Notify on Telegram
    ├── Action Type: "Task" → Add to reminders
    └── Action Type: "None" / Rejected → Log silently
```

