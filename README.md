# DailyJobHunter MCP Server v3.0

A production-ready MCP server that **actually scrapes real jobs** using `python-jobspy` — no API keys, no rate limits, completely free.

## What's New in v3.0

| Feature | Details |
|---|---|
| **Real scraping** | `python-jobspy` hits Naukri + LinkedIn + Indeed + Google Jobs |
| **Tier filter** | Auto-removes FAANG, top-tier companies from results |
| **Resume matching** | Score jobs 0-100 against your resume |
| **JD fetcher** | Fetch full job description from any URL |
| **Daily 3 tool** | One command → 3 fresh mid-tier jobs in 30 seconds |

## Tools

### `search_target_jobs`
Search real jobs across platforms. Automatically filters out top-tier companies.
```
query="Java Full Stack Developer", location="Hyderabad, India", experience_min=2, experience_max=4
```

### `search_and_match_jobs` ⭐ (Main tool)
Search + score against your resume. Returns top N best-matching jobs with reasons.
```
query="Backend Developer", resume_text="<your resume>", top_n=3
```

### `get_daily_3_jobs`
Quick shortcut — 3 fresh jobs posted in last 48 hours. No resume needed.
```
role="Java Full Stack Developer", location="Hyderabad, India", experience_years=3
```

### `fetch_job_description`
Fetch full JD from any job URL (Naukri, LinkedIn, Indeed).
```
job_url="https://www.naukri.com/job-listings-xxx"
```

### `match_jd_to_resume`
Deep analysis: fetch JD + match against resume → score, matched skills, missing skills, tailoring tips.
```
job_url="...", resume_text="<your resume>"
```

## Deploy on Render

1. Fork this repo
2. Create a **New Web Service** on [Render](https://render.com)
3. Connect this repo
4. Set:
   - **Runtime**: Python 3.11+
   - **Build command**: `pip install -r requirements.txt`
   - **Start command**: `python src/server.py`
   - **Health check path**: `/health`
5. Deploy — Render gives you a URL like `https://daily-job-hunter.onrender.com`

## Connect to Perplexity

In Perplexity Desktop → Settings → Connectors → Add Connector → Advanced:
```json
{
  "command": "npx",
  "args": ["-y", "mcp-remote", "https://YOUR-APP.onrender.com/mcp"],
  "env": {}
}
```
> Requires Node.js installed on your machine.

## Connect to Claude Desktop

Edit `~/Library/Application Support/Claude/claude_desktop_config.json`:
```json
{
  "mcpServers": {
    "DailyJobHunter": {
      "command": "npx",
      "args": ["-y", "mcp-remote", "https://YOUR-APP.onrender.com/mcp"]
    }
  }
}
```

## Your Daily Prompt (save this!)

```
Run search_and_match_jobs:
- query: "Backend Developer OR Platform Engineer"
- location: "Hyderabad, India"
- experience_min: 2, experience_max: 4
- top_n: 3
- resume_text: [paste your resume here]

Strict rules:
1. Only mid-tier or scaling companies (top-tier already filtered)
2. Discard heavily front-end or 5+ year roles
3. For each match: show title, company, why it matches (specific skills), apply link
```

## How It Works

`python-jobspy` is an open-source library that scrapes job sites directly, bypassing basic anti-bot measures. It supports concurrent scraping of Naukri, LinkedIn, Indeed, and Google Jobs — **zero API keys, zero monthly limits**.

The negative filter (`TOP_TIER_COMPANIES` list) is hardcoded in `server.py`. Edit it to customize which companies to exclude.

## Render Free Tier Notes

- Server spins down after 15 min of inactivity
- First request of the day → ~30-60 second cold start
- Acceptable if you run once daily
- $7/mo to keep it always-on

## Stack

- `python-jobspy` — multi-site job scraping (Naukri, LinkedIn, Indeed, Google Jobs)
- `FastMCP` — MCP server framework
- `Starlette` + `uvicorn` — ASGI web server
- `httpx` + `beautifulsoup4` — JD fetching
