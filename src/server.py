import os
import re
import asyncio
import uvicorn
import httpx
from bs4 import BeautifulSoup

import mcp.types as types
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.requests import Request
from starlette.responses import JSONResponse

# Initialize Standard MCP Server
mcp = Server("DailyJobHunter")

# ---------------------------------------------------------------------------
# Filtering Configurations
# ---------------------------------------------------------------------------

TOP_TIER_COMPANIES = [
    "google", "amazon", "microsoft", "meta", "apple", "netflix",
    "uber", "airbnb", "stripe", "twitter", "x corp", "linkedin",
    "atlassian", "salesforce", "oracle", "sap", "adobe", "nvidia",
    "intel", "qualcomm", "cisco", "vmware", "servicenow",
    "workday", "splunk", "datadog", "snowflake", "palantir",
    "goldman sachs", "morgan stanley", "jpmorgan", "mckinsey",
]

AVOID_TITLES = [
    "senior", "lead", "principal", "staff", "architect",
    "manager", "director", "avp", "vp", "level iii",
    "level iv", "sde iii", "sde iv", "l5", "l6"
]

COMMON_SKILLS = [
    "python", "java", "javascript", "typescript", "go", "rust", "c++", "c#",
    "react", "angular", "vue", "node", "django", "flask", "spring", "fastapi",
    "aws", "azure", "gcp", "docker", "kubernetes", "terraform", "ansible",
    "postgresql", "mysql", "mongodb", "redis", "kafka", "rabbitmq",
    "machine learning", "llm", "rest", "graphql", "grpc", "microservices", 
    "ci/cd", "jenkins", "sql", "linux", "bash", "git", "backend", "full stack"
]

def _is_top_tier(company_name: str) -> bool:
    return any(t in company_name.lower() for t in TOP_TIER_COMPANIES)

def _should_avoid_title(title: str) -> bool:
    return any(t in title.lower() for t in AVOID_TITLES)

# ---------------------------------------------------------------------------
# Core Logic & Resume Parsing
# ---------------------------------------------------------------------------

def parse_resume(resume_text: str) -> dict:
    text_lower = resume_text.lower()
    found_skills = [s for s in COMMON_SKILLS if s in text_lower]
    
    years = 0
    for pat in [r"(\d+)\+?\s*years?\s*(?:of\s*)?experience", r"experience\s*(?:of\s*)?(\d+)\+?\s*years?"]:
        m = re.search(pat, text_lower)
        if m:
            years = int(m.group(1))
            break
            
    return {"skills": found_skills, "experience_years": years}

def score_job_match(job: dict, resume_data: dict) -> int:
    score = 0
    resume_skills = set(resume_data.get("skills", []))
    resume_years = resume_data.get("experience_years", 0)
    
    job_text = f"{job.get('title', '')} {job.get('company', '')} {job.get('description', '')}".lower()
    matched_skills = [s for s in resume_skills if s in job_text]
    score += min(60, len(matched_skills) * 8)
    
    exp_req = 0
    exp_match = re.search(r"(\d+)\+?\s*years?", job_text)
    if exp_match:
        exp_req = int(exp_match.group(1))
        
    if exp_req == 0:
        score += 15
    elif abs(exp_req - resume_years) <= 1:
        score += 25
    elif exp_req <= resume_years + 2:
        score += 15
    else:
        score += 5
        
    for kw in ["developer", "engineer", "backend", "full stack", "platform"]:
        if kw in job.get("title", "").lower():
            score += 3
            
    return min(score, 100)

def _search_jobs_sync(keyword: str, location: str, hours_old: int, results_wanted: int) -> list[dict]:
    try:
        from jobspy import scrape_jobs
        df = scrape_jobs(
            site_name=["naukri", "linkedin", "indeed"],
            search_term=keyword,
            location=location,
            results_wanted=results_wanted,
            hours_old=hours_old,
            country_linkedin="india"
        )
        if df is None or df.empty:
            return []
            
        jobs = []
        for _, row in df.iterrows():
            company = str(row.get("company", ""))
            title = str(row.get("title", ""))
            
            if _should_avoid_title(title) or _is_top_tier(company):
                continue
                
            jobs.append({
                "title": title,
                "company": company,
                "location": str(row.get("location", "")),
                "job_url": str(row.get("job_url", "")),
                "description": str(row.get("description", ""))[:500]
            })
        return jobs
    except Exception as e:
        return [{"error": str(e)}]

# ---------------------------------------------------------------------------
# Tool Implementations
# ---------------------------------------------------------------------------

async def search_and_match_jobs_logic(query: str, resume_text: str, location: str, top_n: int) -> str:
    resume_data = parse_resume(resume_text)
    loop = asyncio.get_event_loop()
    jobs = await loop.run_in_executor(
        None, 
        lambda: _search_jobs_sync(keyword=query, location=location, hours_old=168, results_wanted=35)
    )
    
    if jobs and jobs[0].get("error"):
        return f"Error: {jobs[0]['error']}"
    if not jobs:
        return "No matching mid-tier/appropriate experience jobs found right now."

    scored = [(j, score_job_match(j, resume_data)) for j in jobs]
    scored.sort(key=lambda x: x[1], reverse=True)
    top_jobs = scored[:top_n]

    lines = [f"🎯 Target Match Results: {query}\n"]
    for rank, (j, score) in enumerate(top_jobs, 1):
        lines.append(f"#{rank} Match — {score}/100 ✅")
        lines.append(f"Role: {j['title']} at {j['company']}")
        lines.append(f"Location: {j['location']} | Link: {j.get('job_url', 'N/A')}\n")
        
    return "\n".join(lines)

async def fetch_job_description_logic(job_url: str) -> str:
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(job_url, headers=headers)
            soup = BeautifulSoup(resp.text, "lxml")
            for tag in soup(["script", "style", "nav", "footer", "header"]):
                tag.decompose()
            return soup.get_text(separator="\n", strip=True)[:3000]
    except Exception as e:
        return f"Could not fetch JD: {str(e)}"

# ---------------------------------------------------------------------------
# MCP Tool Registration (Official v1.0.0 Syntax)
# ---------------------------------------------------------------------------

@mcp.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="search_and_match_jobs",
            description="Searches live jobs, strips out high-level/top-tier roles, and matches against a resume.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Job title to search"},
                    "resume_text": {"type": "string", "description": "Your full resume text"},
                    "location": {"type": "string", "default": "Hyderabad, India"},
                    "top_n": {"type": "integer", "default": 3}
                },
                "required": ["query", "resume_text"]
            }
        ),
        types.Tool(
            name="fetch_job_description",
            description="Fetches the full text of a job description from a URL.",
            inputSchema={
                "type": "object",
                "properties": {
                    "job_url": {"type": "string", "description": "URL of the job posting"}
                },
                "required": ["job_url"]
            }
        )
    ]

@mcp.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name == "search_and_match_jobs":
        result = await search_and_match_jobs_logic(
            arguments.get("query"),
            arguments.get("resume_text"),
            arguments.get("location", "Hyderabad, India"),
            arguments.get("top_n", 3)
        )
        return [types.TextContent(type="text", text=result)]
        
    elif name == "fetch_job_description":
        result = await fetch_job_description_logic(arguments.get("job_url"))
        return [types.TextContent(type="text", text=result)]
        
    raise ValueError(f"Unknown tool: {name}")

# ---------------------------------------------------------------------------
# Server Initialization (SSE Transport for Render)
# ---------------------------------------------------------------------------

sse = SseServerTransport("/messages/")

async def handle_sse(request: Request):
    async with sse.connect_sse(request.scope, request.receive, request._send) as (read_stream, write_stream):
        await mcp.run(
            read_stream,
            write_stream,
            mcp.create_initialization_options()
        )

async def health_check(request: Request):
    return JSONResponse({"status": "ok", "version": "4.3 (Final)"})

app = Starlette(
    routes=[
        Route("/sse", endpoint=handle_sse),
        Mount("/messages/", app=sse.handle_post_message),
        Route("/health", endpoint=health_check),
    ]
)

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
