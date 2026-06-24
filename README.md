# LinkedIn MCP Server

A free, Render-deployable **LinkedIn MCP Server** built with [FastMCP](https://github.com/jlowin/fastmcp).
Connect it to **Perplexity** (or any MCP-compatible client) as a custom connector.

## Tools

| Tool | What it does |
|---|---|
| `linkedin_search_jobs` | Search by keyword + location |
| `linkedin_build_search_url` | Full filter: remote, easy apply, exp level, job type, date |
| `linkedin_get_job_details` | Get public job details URL by job ID |
| `linkedin_easy_apply_search` | Shortcut for Easy Apply jobs |
| `linkedin_remote_jobs` | Remote-only job search |

## Deploy to Render (Free)

1. Fork / push this repo to GitHub
2. Go to [render.com](https://render.com) → New → Web Service
3. Connect your GitHub repo
4. Render auto-detects `render.yaml` — click **Deploy**
5. Your MCP URL will be: `https://linkedin-mcp-server.onrender.com/mcp`

## Connect to Perplexity

1. Open Perplexity → Settings → Connectors
2. Click **Add custom connector**
3. Paste your Render URL: `https://linkedin-mcp-server.onrender.com/mcp`
4. Save and enable

## Local dev

```bash
pip install -r requirements.txt
python src/server.py
# Server runs at http://localhost:8000/mcp
```
