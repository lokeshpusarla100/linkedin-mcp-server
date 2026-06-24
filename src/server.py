"""
DailyJobHunter MCP Server v3.0
- python-jobspy for real job scraping (Naukri + LinkedIn + Indeed + Google Jobs)
- Negative tier filter (FAANG, top-tier companies auto-excluded)
- Resume matching & scoring
- SSE + Streamable HTTP transport (Render-compatible)
- No API keys needed
"""

import os
import re
import json
import secrets
import uuid
import asyncio
from urllib.parse import urlencode
from typing import Optional

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Mount, Route

mcp = FastMCP("DailyJobHunter")
REGISTERED_CLIENTS: dict[str, dict] = {}
BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:8000").rstrip("/")

# ---------------------------------------------------------------------------
# Tier Filter — companies to exclude from results
# ---------------------------------------------------------------------------

TOP_TIER_COMPANIES = [
    "google", "amazon", "microsoft", "meta", "apple", "netflix",
    "uber", "airbnb", "stripe", "twitter", "x corp", "linkedin",
    "atlassian", "salesforce", "oracle", "sap", "adobe", "nvidia",
    "intel", "qualcomm", "cisco", "vmware", "servicenow",
    "workday", "splunk", "datadog", "snowflake", "palantir",
    "goldman sachs", "morgan stanley", "jpmorgan", "mckinsey",
    "bytedance", "tiktok",
]

def _is_top_tier(company_name: str) -> bool:
    name_lower = company_name.lower()
    return any(t in name_lower for t in TOP_TIER_COMPANIES)


# ---------------------------------------------------------------------------
# Resume Parser — extract skills + experience from plain text
# ---------------------------------------------------------------------------

COMMON_SKILLS = [
    "python", "java", "javascript", "typescript", "go", "rust", "c++", "c#",
    "react", "angular", "vue", "node", "django", "flask", "spring", "fastapi",
    "aws", "azure", "gcp", "docker", "kubernetes", "terraform", "ansible",
    "postgresql", "mysql", "mongodb", "redis", "kafka", "rabbitmq",
    "machine learning", "deep learning", "tensorflow", "pytorch", "llm",
    "rest", "graphql", "grpc", "microservices", "ci/cd", "jenkins", "github actions",
    "sql", "nosql", "elasticsearch", "spark", "hadoop",
    "html", "css", "tailwind", "next.js", "nuxt", "svelte",
    "linux", "bash", "git", "agile", "scrum",
    "full stack", "backend", "frontend", "devops", "data engineering",
]

def parse_resume(resume_text: str) -> dict:
    """Extract skills, experience years, and key info from resume text."""
    text_lower = resume_text.lower()
    found_skills = [s for s in COMMON_SKILLS if s in text_lower]
    
    exp_patterns = [
        r"(\d+)\+?\s*years?\s*(?:of\s*)?experience",
        r"experience\s*(?:of\s*)?(\d+)\+?\s*years?",
    ]
    years = 0
    for pat in exp_patterns:
        m = re.search(pat, text_lower)
        if m:
            years = int(m.group(1))
            break
    
    return {
        "skills": found_skills,
        "experience_years": years,
        "raw_length": len(resume_text),
    }


def score_job_match(job: dict, resume_data: dict) -> int:
    """
    Score a job 0-100 based on how well it matches the resume.
    Considers: skill overlap, experience fit, title relevance.
    """
    score = 0
    resume_skills = set(resume_data.get("skills", []))
    resume_years = resume_data.get("experience_years", 0)
    
    job_text = " ".join([
        job.get("title", ""),
        job.get("company", ""),
        job.get("description", ""),
        job.get("job_highlights", ""),
    ]).lower()
    
    matched_skills = [s for s in resume_skills if s in job_text]
    skill_score = min(60, len(matched_skills) * 8)
    score += skill_score
    
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
    elif exp_req > resume_years + 2:
        score += 5
    
    title_lower = job.get("title", "").lower()
    title_keywords = ["developer", "engineer", "architect", "analyst", "lead", "senior",
                      "backend", "frontend", "full stack", "fullstack", "devops", "platform"]
    for kw in title_keywords:
        if kw in title_lower:
            score += 3
    score = min(score, 100)
    
    return score


# ---------------------------------------------------------------------------
# Job Search via python-jobspy
# ---------------------------------------------------------------------------

def _search_jobs_sync(
    keyword: str,
    location: str = "India",
    hours_old: int = 168,
    results_wanted: int = 20,
    sites: list = None,
    easy_apply: bool = False,
) -> list[dict]:
    """Run jobspy search synchronously. Returns raw job list."""
    try:
        from jobspy import scrape_jobs

        if sites is None:
            sites = ["naukri", "linkedin", "indeed", "google"]

        df = scrape_jobs(
            site_name=sites,
            search_term=keyword,
            location=location,
            results_wanted=results_wanted,
            hours_old=hours_old,
            easy_apply=easy_apply,
            linkedin_fetch_description=True,
        )

        if df is None or len(df) == 0:
            return []

        jobs = []
        for _, row in df.iterrows():
            company = str(row.get("company", "") or "")
            title = str(row.get("title", "") or "")
            description = str(row.get("description", "") or "")[:500]
            location_str = str(row.get("location", "") or "")
            salary = str(row.get("salary_source", "") or "")
            job_url = str(row.get("job_url", "") or "")
            site = str(row.get("site", "") or "")
            date_posted = str(row.get("date_posted", "") or "")
            job_type = str(row.get("job_type", "") or "")

            jobs.append({
                "title": title,
                "company": company,
                "location": location_str,
                "salary": salary,
                "description": description,
                "job_url": job_url,
                "site": site,
                "date_posted": date_posted,
                "job_type": job_type,
                "is_top_tier": _is_top_tier(company),
            })

        return jobs

    except ImportError:
        return [{"error": "python-jobspy not installed. Run: pip install python-jobspy"}]
    except Exception as e:
        return [{"error": str(e)}]


# ---------------------------------------------------------------------------
# MCP TOOLS
# ---------------------------------------------------------------------------

@mcp.tool()
async def search_target_jobs(
    query: str,
    location: str = "Hyderabad, India",
    experience_min: int = 0,
    experience_max: int = 10,
    exclude_top_tier: bool = True,
    sites: str = "naukri,linkedin,indeed",
    max_results: int = 20,
    hours_old: int = 168,
) -> str:
    """
    Search for jobs across Naukri, LinkedIn, and Indeed using python-jobspy.
    Returns REAL job listings with title, company, salary, location, apply link.
    Automatically filters out top-tier companies (Google, Amazon, Microsoft etc.)
    so you only see mid-tier and scaling companies.

    Args:
        query: Job search term e.g. "Java Full Stack Developer"
        location: City + country e.g. "Hyderabad, India"
        experience_min: Minimum years of experience
        experience_max: Maximum years of experience
        exclude_top_tier: If True, removes FAANG/top-tier company results
        sites: Comma-separated list: naukri,linkedin,indeed,google
        max_results: Number of results to fetch (max 30)
        hours_old: Only jobs posted in the last N hours (168 = 1 week)
    """
    site_list = [s.strip() for s in sites.split(",") if s.strip()]

    loop = asyncio.get_event_loop()
    jobs = await loop.run_in_executor(
        None,
        lambda: _search_jobs_sync(
            keyword=query,
            location=location,
            hours_old=hours_old,
            results_wanted=max_results,
            sites=site_list,
        )
    )

    if jobs and jobs[0].get("error"):
        return f"\u26a0\ufe0f Error: {jobs[0]['error']}"

    if exclude_top_tier:
        filtered = [j for j in jobs if not j.get("is_top_tier")]
        removed = len(jobs) - len(filtered)
        jobs = filtered
    else:
        removed = 0

    if not jobs:
        return f"No jobs found for: {query} in {location}"

    lines = [
        f"\U0001f50d **{query}** in {location}",
        f"\U0001f4ca {len(jobs)} mid-tier jobs found" + (f" (removed {removed} top-tier companies)" if removed > 0 else ""),
        "\u2501" * 50,
    ]

    for i, j in enumerate(jobs, 1):
        lines.append(f"\n**{i}. {j['title']}**")
        lines.append(f"   \U0001f3e2 {j['company']}  |  \U0001f4cd {j['location']}")
        if j.get("salary"):
            lines.append(f"   \U0001f4b0 {j['salary']}")
        if j.get("job_type"):
            lines.append(f"   \U0001f550 {j['job_type']}")
        if j.get("date_posted"):
            lines.append(f"   \U0001f4c5 Posted: {j['date_posted']}")
        if j.get("description"):
            lines.append(f"   \U0001f4dd {j['description'][:200]}...")
        if j.get("job_url"):
            lines.append(f"   \U0001f517 {j['job_url']}")
        lines.append(f"   \U0001f4e1 Source: {j.get('site', 'unknown').upper()}")

    return "\n".join(lines)


@mcp.tool()
async def search_and_match_jobs(
    query: str,
    resume_text: str,
    location: str = "Hyderabad, India",
    experience_min: int = 0,
    experience_max: int = 10,
    top_n: int = 3,
    exclude_top_tier: bool = True,
    hours_old: int = 168,
) -> str:
    """
    Search for jobs AND score them against your resume.
    Returns the top N best-matching jobs with match scores and reasons.
    This is your daily job hunt tool — paste your resume, get 3 perfect matches.

    Args:
        query: Job role e.g. "Backend Developer" or "Platform Engineer"
        resume_text: Your full resume text (paste it)
        location: City e.g. "Hyderabad, India"
        experience_min: Your minimum experience
        experience_max: Your max experience
        top_n: How many top matches to return (default 3)
        exclude_top_tier: Filter out FAANG/top-tier companies
        hours_old: Jobs posted in last N hours (168 = 1 week)
    """
    resume_data = parse_resume(resume_text)

    loop = asyncio.get_event_loop()
    jobs = await loop.run_in_executor(
        None,
        lambda: _search_jobs_sync(
            keyword=query,
            location=location,
            hours_old=hours_old,
            results_wanted=25,
            sites=["naukri", "linkedin", "indeed"],
        )
    )

    if jobs and jobs[0].get("error"):
        return f"\u26a0\ufe0f Error: {jobs[0]['error']}"

    if exclude_top_tier:
        jobs = [j for j in jobs if not j.get("is_top_tier")]

    if not jobs:
        return "No matching jobs found. Try broadening your search."

    scored = [(j, score_job_match(j, resume_data)) for j in jobs]
    scored.sort(key=lambda x: x[1], reverse=True)
    top_jobs = scored[:top_n]

    lines = [
        f"\U0001f3af **Daily Job Match: {query}**",
        f"\U0001f4c4 Resume skills detected: {', '.join(resume_data['skills'][:8])}",
        f"\U0001f4ca Scored {len(scored)} jobs \u2192 showing top {top_n} matches",
        "\u2501" * 50,
    ]

    for rank, (j, score) in enumerate(top_jobs, 1):
        job_text = (j.get("title", "") + " " + j.get("description", "")).lower()
        matched = [s for s in resume_data["skills"] if s in job_text]

        lines.append(f"\n## #{rank} Match \u2014 {score}/100 \u2705")
        lines.append(f"**{j['title']}** at **{j['company']}**")
        lines.append(f"\U0001f4cd {j['location']}  |  \U0001f4c5 {j.get('date_posted', 'N/A')}")
        if j.get("salary"):
            lines.append(f"\U0001f4b0 {j['salary']}")
        lines.append(f"\n**Why it matches your resume:**")
        if matched:
            lines.append(f"\u2713 Skill overlap: {', '.join(matched[:5])}")
        exp_req_match = re.search(r"(\d+)\+?\s*years?", j.get("description", "").lower())
        if exp_req_match:
            req = int(exp_req_match.group(1))
            diff = abs(req - resume_data.get("experience_years", 0))
            if diff <= 1:
                lines.append(f"\u2713 Experience fit: JD asks {req} yrs, resume shows {resume_data.get('experience_years', '?')} yrs")
        if j.get("description"):
            lines.append(f"\n\U0001f4dd JD Preview: {j['description'][:300]}...")
        if j.get("job_url"):
            lines.append(f"\n\U0001f517 Apply: {j['job_url']}")
        lines.append("")

    return "\n".join(lines)


@mcp.tool()
async def get_daily_3_jobs(
    role: str,
    location: str = "Hyderabad, India",
    experience_years: int = 3,
) -> str:
    """
    Quick daily shortcut — get exactly 3 fresh mid-tier jobs for your role.
    No resume needed. Just the role, location, and years of experience.
    Perfect for a daily 30-second check.

    Args:
        role: Your target role e.g. "Java Full Stack Developer"
        location: City e.g. "Hyderabad, India"
        experience_years: Your years of experience
    """
    loop = asyncio.get_event_loop()
    jobs = await loop.run_in_executor(
        None,
        lambda: _search_jobs_sync(
            keyword=role,
            location=location,
            hours_old=48,
            results_wanted=30,
            sites=["naukri", "linkedin", "indeed"],
        )
    )

    if jobs and jobs[0].get("error"):
        return f"\u26a0\ufe0f Error: {jobs[0]['error']}"

    filtered = [j for j in jobs if not j.get("is_top_tier")]
    top3 = filtered[:3]

    if not top3:
        return f"No fresh jobs found for {role} in {location} (last 48 hours). Try expanding time window."

    lines = [
        f"\U0001f305 **Daily 3 Jobs \u2014 {role}**",
        f"\U0001f4cd {location}  |  \U0001f550 Fresh listings (last 48 hours)",
        f"\U0001f6ab Top-tier filtered out ({len(jobs) - len(filtered)} removed)",
        "\u2501" * 50,
    ]

    for i, j in enumerate(top3, 1):
        lines.append(f"\n**Job {i}:**")
        lines.append(f"\U0001f3f7\ufe0f {j['title']}")
        lines.append(f"\U0001f3e2 {j['company']}")
        lines.append(f"\U0001f4cd {j['location']}")
        if j.get("salary"):
            lines.append(f"\U0001f4b0 {j['salary']}")
        if j.get("date_posted"):
            lines.append(f"\U0001f4c5 {j['date_posted']}")
        lines.append(f"\U0001f517 {j.get('job_url', 'N/A')}")

    return "\n".join(lines)


@mcp.tool()
async def fetch_job_description(
    job_url: str,
) -> str:
    """
    Fetch the full job description from any job URL (Naukri, LinkedIn, Indeed).
    Use this to deep-read a JD before applying or for resume tailoring.

    Args:
        job_url: Full URL of the job posting
    """
    import httpx
    from bs4 import BeautifulSoup

    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,*/*",
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=True) as client:
            resp = await client.get(job_url, headers=headers)
            soup = BeautifulSoup(resp.text, "lxml")

            for tag in soup(["script", "style", "nav", "footer", "header", "aside"]):
                tag.decompose()

            jd_selectors = [
                "div.job-description", "div#job-description",
                "div.description", "div.jd-container",
                "section.description", "div[data-automation='jobDescription']",
                "div.show-more-less-html", "article",
            ]
            content = None
            for sel in jd_selectors:
                el = soup.select_one(sel)
                if el:
                    content = el.get_text(separator="\n", strip=True)
                    break

            if not content:
                content = soup.get_text(separator="\n", strip=True)

            lines = [l.strip() for l in content.splitlines() if l.strip()]
            clean = "\n".join(lines[:100])

            return f"\U0001f4c4 **Job Description**\n\U0001f517 {job_url}\n\n{clean}"

    except Exception as e:
        return f"\u26a0\ufe0f Could not fetch JD: {str(e)}\n\nTry opening manually: {job_url}"


@mcp.tool()
async def match_jd_to_resume(
    job_url: str,
    resume_text: str,
) -> str:
    """
    Fetch a job's full JD and do a deep match against your resume.
    Returns a score, matched skills, missing skills, and tailoring suggestions.

    Args:
        job_url: URL of the job posting
        resume_text: Your full resume text
    """
    jd_text = await fetch_job_description(job_url)

    resume_data = parse_resume(resume_text)
    resume_skills = set(resume_data["skills"])

    jd_lower = jd_text.lower()

    matched = [s for s in resume_skills if s in jd_lower]
    all_jd_skills = [s for s in COMMON_SKILLS if s in jd_lower]
    missing = [s for s in all_jd_skills if s not in resume_skills]

    score = min(100, len(matched) * 10 + (20 if len(missing) <= 3 else 0))

    lines = [
        f"\U0001f3af **Resume \u2194 JD Match Analysis**",
        f"\n\U0001f4ca **Match Score: {score}/100**",
        f"\n\u2705 **Skills You Have ({len(matched)}):**",
        ", ".join(matched) if matched else "None detected from common skill set",
        f"\n\u26a0\ufe0f **Skills to Add/Highlight ({len(missing)}):**",
        ", ".join(missing[:10]) if missing else "All required skills covered!",
        f"\n\U0001f4a1 **Tailoring Suggestions:**",
    ]

    if missing:
        lines.append(f"\u2022 Add these to your resume if you have experience: {', '.join(missing[:5])}")
    if matched:
        lines.append(f"\u2022 Bold these in your resume as they match exactly: {', '.join(matched[:5])}")

    exp_req = re.search(r"(\d+)\+?\s*years?", jd_lower)
    if exp_req:
        req = int(exp_req.group(1))
        resume_years = resume_data.get("experience_years", 0)
        if req > resume_years + 2:
            lines.append(f"\u2022 \u26a0\ufe0f JD asks {req} yrs \u2014 your resume shows {resume_years} yrs. Address gap in cover letter.")
        elif req <= resume_years:
            lines.append(f"\u2022 \u2713 Your {resume_years} yrs meets the {req} yr requirement.")

    lines.append(f"\n---\n\U0001f4c4 JD Snippet:\n{jd_text[200:600]}...")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# OAuth discovery + Dynamic Client Registration
# ---------------------------------------------------------------------------

async def oauth_protected_resource(request: Request) -> JSONResponse:
    return JSONResponse({
        "resource": BASE_URL,
        "authorization_servers": [BASE_URL],
        "bearer_methods_supported": ["header"],
        "resource_documentation": f"{BASE_URL}/mcp",
    })


async def oauth_authorization_server(request: Request) -> JSONResponse:
    return JSONResponse({
        "issuer": BASE_URL,
        "authorization_endpoint": f"{BASE_URL}/oauth/authorize",
        "token_endpoint": f"{BASE_URL}/oauth/token",
        "registration_endpoint": f"{BASE_URL}/oauth/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "client_credentials"],
        "token_endpoint_auth_methods_supported": ["none", "client_secret_post"],
        "scopes_supported": ["mcp"],
    })


async def openid_configuration(request: Request) -> JSONResponse:
    return JSONResponse({
        "issuer": BASE_URL,
        "authorization_endpoint": f"{BASE_URL}/oauth/authorize",
        "token_endpoint": f"{BASE_URL}/oauth/token",
        "registration_endpoint": f"{BASE_URL}/oauth/register",
        "jwks_uri": f"{BASE_URL}/.well-known/jwks.json",
        "response_types_supported": ["code"],
        "subject_types_supported": ["public"],
        "id_token_signing_alg_values_supported": ["RS256"],
    })


async def oauth_register(request: Request) -> JSONResponse:
    try:
        body = await request.json()
    except Exception:
        body = {}
    client_id = str(uuid.uuid4())
    client_secret = secrets.token_urlsafe(32)
    client_data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "client_name": body.get("client_name", "MCP Client"),
        "redirect_uris": body.get("redirect_uris", []),
        "grant_types": body.get("grant_types", ["authorization_code"]),
        "response_types": body.get("response_types", ["code"]),
        "token_endpoint_auth_method": body.get("token_endpoint_auth_method", "none"),
    }
    REGISTERED_CLIENTS[client_id] = client_data
    return JSONResponse(client_data, status_code=201)


async def oauth_token(request: Request) -> JSONResponse:
    return JSONResponse({
        "access_token": secrets.token_urlsafe(32),
        "token_type": "bearer",
        "expires_in": 86400,
        "scope": "mcp",
    })


async def oauth_authorize(request: Request) -> JSONResponse:
    redirect_uri = request.query_params.get("redirect_uri", "")
    code = secrets.token_urlsafe(16)
    state = request.query_params.get("state", "")
    if redirect_uri:
        sep = "&" if "?" in redirect_uri else "?"
        return RedirectResponse(url=f"{redirect_uri}{sep}code={code}&state={state}")
    return JSONResponse({"code": code, "state": state})


async def health(request: Request) -> JSONResponse:
    return JSONResponse({
        "status": "ok",
        "service": "DailyJobHunter MCP",
        "version": "3.0",
        "scraper": "python-jobspy",
        "tools": [
            "search_target_jobs — real scraping, top-tier filter",
            "search_and_match_jobs — scraping + resume scoring",
            "get_daily_3_jobs — quick 3 fresh jobs daily",
            "fetch_job_description — full JD from any URL",
            "match_jd_to_resume — deep JD vs resume analysis",
        ],
    })


# ---------------------------------------------------------------------------
# Build ASGI app
# ---------------------------------------------------------------------------

def build_app():
    mcp_asgi = mcp.streamable_http_app()
    routes = [
        Route("/.well-known/oauth-protected-resource", oauth_protected_resource),
        Route("/.well-known/oauth-protected-resource/mcp", oauth_protected_resource),
        Route("/.well-known/oauth-authorization-server", oauth_authorization_server),
        Route("/.well-known/openid-configuration", openid_configuration),
        Route("/oauth/register", oauth_register, methods=["POST"]),
        Route("/oauth/token", oauth_token, methods=["POST"]),
        Route("/oauth/authorize", oauth_authorize, methods=["GET"]),
        Route("/health", health),
        Mount("/mcp", app=mcp_asgi),
    ]
    return Starlette(routes=routes)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(build_app(), host="0.0.0.0", port=port)
