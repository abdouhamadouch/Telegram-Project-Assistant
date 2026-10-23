# Telegram Project Assistant

A private Telegram assistant for project notes, GitHub repository metadata, Render service status, and confirmed deploy requests. It stores conversation history and project memories in SQLite.

## Important limitation

This bot does **not** connect to the exact ChatGPT conversation open in the ChatGPT app and cannot inherit that conversation's internal memory or tools. It uses the separately configured model API and its own stored history. Model/API access and ChatGPT subscriptions are separate.

## Commands

- `/start`, `/help`: help
- `/repo`: inspect configured GitHub repository metadata
- `/status`: inspect configured Render service and latest deployment
- `/deploy`: show confirmation instructions
- `/deploy confirm`: request a new Render deployment
- `/remember text`: save a project note
- `/memory`: list saved notes
- `/clearhistory`: clear saved chat history but keep project notes

Only the Telegram account matching `OWNER_ID` can use the bot.

## Environment variables

- `BOT_TOKEN`: token from BotFather
- `OWNER_ID`: your numeric Telegram user ID
- `AI_API_KEY`: API key from the chosen model provider
- `AI_BASE_URL`: OpenAI-compatible API base URL
- `AI_MODEL`: model name supported by that provider
- `GH_TOKEN`: GitHub token with the minimum repository read access needed
- `GH_REPO`: repository in `owner/name` format
- `RENDER_API_KEY`: Render API key
- `RENDER_SERVICE_ID`: target Render service ID
- `DB_PATH`: SQLite file path (default `/data/assistant.sqlite3`)

Never commit secrets or send them in Telegram messages.

## Deploy on Render

1. Create a **Background Worker** from this GitHub repository (not a web service; the bot polls Telegram).
2. Set the environment variables above in the Render dashboard.
3. Add persistent storage mounted at `/data` if your chosen Render plan supports disks. Without persistent storage, SQLite data may be lost on restart/redeploy.
4. Deploy and inspect logs. Do not run multiple replicas with the same Telegram bot token; polling conflicts can occur.

## Current scope and security

This initial version can read repository metadata, read Render service/deployment status, submit a deploy request only after an explicit confirmation command, and store notes/chat history. It does not yet edit or commit source files automatically. Add such write capabilities only with a strict repository allowlist, review/confirmation step, and tests.

## Local run

```bash
python -m venv .venv
pip install -r requirements.txt
cp .env.example .env
# Fill values in .env, then export them into your shell
python bot.py
```
