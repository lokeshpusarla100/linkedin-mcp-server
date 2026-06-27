import os
import re
import json
import logging
import asyncio
import hashlib
import uvicorn
import httpx
from datetime import datetime, timedelta

import mcp.types as types
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from starlette.applications import Starlette
from starlette.routing import Route, Mount
from starlette.requests import Request
from starlette.responses import JSONResponse

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("JobPipeline")

server = Server("IntelligentJobHunter")

# ---------------------------------------------------------------------------
# CANDIDATE SKILL KEYWORDS (for lightweight server-side ranking, no LLM)
# ---------------------------------------------------------------------------
CORE_SKILLS = [
    "java", "spring boot", "spring", "angular", "typescript", "javascript",
    "microservices", "rest api", "restful", "kafka", "redis", "docker",
    "aws", "jenkins", "ci/cd", "jpa", "hibernate", "junit", "maven", "git",
    "mysql", "oracle", "sql", "spring cloud", "resilience4j", "cloudwatch",
    "ec2", "s3", "lambda", "sqs", "api gateway", "html", "css", "rxjs",
]

# Multi-query strategy for broad coverage
SEARCH_QUERIES = [
    "Java Spring Boot Developer",
    "Full Stack Java Angular Developer",
    "Backend Engineer Microservices",
]

# Only block pure recruitment/staffing agencies
STAFFING_AGENCIES = [
    "randstad", "manpowergroup", "collabera", "teksystems",
    "adecco", "kelly services", "robert half", "hays",
]

# Skip clearly wrong seniority levels
SKIP_TITLES = [
    "principal", "staff", "architect", "director", "vp", "avp",
    "cto", "head of", "level iii", "level iv", "sde iii", "sde iv",
    "l5", "l6", "l7", "intern", "trainee", "fresher",
]

# In-memory cache (survives within a Render session)
SCRAPE_CACHE = {"timestamp": None, "data": []}
CACHE_HOURS = 6


# ---------------------------------------------------------------------------
# UTILITIES
# ---------------------------------------------------------------------------
def _hash_url(url: str) -> str:
    return hashlib.sha256(url.strip().encode()).hexdigest()[:16]


def _keyword_score(jd_text: str) -> tuple[int, list[str]]:
    """Count how many candidate skills appear in JD text. No LLM needed."""
    jd_lower = jd_text.lower()
    matched = [skill for skill in CORE_SKILLS if skill in jd_lower]
    return len(matched), matched


CANDIDATE_YEARS = 2.6
MIN_SKILL_HITS = 3  # Jobs with fewer than 3 matching skills are noise

# Senior threshold — only reject if minimum experience is this or higher
TOO_SENIOR_MIN = 5


def _extract_experience_numbers(jd_text: str) -> list[int]:
    """
    Extract ALL numbers that appear near experience-related words.
    Handles every format: "2-4 years", "2+ yrs", "minimum 2 years",
    "two plus years", "experience: 3-5", "at least 4 yrs", etc.
    Returns sorted list of all numbers found near experience context.
    """
    numbers = []

    # Split JD into chunks around experience-related keywords
    # Look for numbers within 60 chars of these words
    exp_keywords = r"(?:experience|exp|years?|yrs?)"
    # Find all regions containing experience keywords
    for match in re.finditer(exp_keywords, jd_text, re.IGNORECASE):
        start = max(0, match.start() - 60)
        end = min(len(jd_text), match.end() + 60)
        region = jd_text[start:end]

        # Extract all digits from this region
        for num_match in re.finditer(r"\b(\d{1,2})\b", region):
            val = int(num_match.group(1))
            # Only care about reasonable experience values (0-20)
            if 0 <= val <= 20:
                numbers.append(val)

    return sorted(set(numbers))


def _experience_qualifies(jd_text: str) -> tuple[bool, str]:
    """
    Check if candidate's 2.6 years fits the JD.
    Philosophy: DEFAULT IS INCLUDE. Only reject when we're very confident
    the role requires 5+ years minimum.
    """
    nums = _extract_experience_numbers(jd_text)

    if not nums:
        return True, "No experience numbers found — included"

    min_num = nums[0]  # smallest number found near experience context
    max_num = nums[-1]  # largest

    # If the smallest number mentioned is already >= 5, too senior
    if min_num >= TOO_SENIOR_MIN:
        return False, f"Requires {min_num}+ years — too senior"

    # Range like 0-3, 1-4, 2-5 etc — we qualify
    return True, f"Experience range {min_num}-{max_num} years — qualifies"


# ---------------------------------------------------------------------------
# SUPABASE DEDUP (optional — works without it)
# ---------------------------------------------------------------------------
def _supa_headers() -> dict | None:
    url = os.environ.get("SUPABASE_URL")
    key = os.environ.get("SUPABASE_ANON_KEY")
    if not url or not key:
        return None
    return {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }


async def _check_seen(url_hashes: list[str]) -> set[str]:
    headers = _supa_headers()
    base = os.environ.get("SUPABASE_URL", "")
    if not headers or not url_hashes:
        return set()
    try:
        hash_csv = ",".join(url_hashes)
        endpoint = f"{base}/rest/v1/seen_jobs?url_hash=in.({hash_csv})&select=url_hash"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(endpoint, headers=headers)
            resp.raise_for_status()
            return {row["url_hash"] for row in resp.json()}
    except Exception as e:
        logger.warning(f"Supabase read failed (dedup skipped): {e}")
        return set()


async def _mark_seen(jobs: list[dict]) -> None:
    headers = _supa_headers()
    base = os.environ.get("SUPABASE_URL", "")
    if not headers or not jobs:
        return
    try:
        headers["Prefer"] = "resolution=ignore-duplicates"
        rows = [
            {
                "url_hash": j["url_hash"],
                "title": j.get("title", "")[:200],
                "company": j.get("company", "")[:100],
                "skill_hits": j.get("skill_hits", 0),
            }
            for j in jobs
        ]
        endpoint = f"{base}/rest/v1/seen_jobs"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(endpoint, headers=headers, json=rows)
            resp.raise_for_status()
            logger.info(f"Marked {len(rows)} jobs as seen")
    except Exception as e:
        logger.warning(f"Supabase write failed: {e}")


# ---------------------------------------------------------------------------
# PIPELINE STAGES
# ---------------------------------------------------------------------------
def stage_scrape() -> list[dict]:
    """Multi-query scrape across LinkedIn + Naukri, dedup by URL."""
    global SCRAPE_CACHE
    now = datetime.now()

    if (
        SCRAPE_CACHE["timestamp"]
        and (now - SCRAPE_CACHE["timestamp"]) < timedelta(hours=CACHE_HOURS)
    ):
        logger.info("Serving from in-memory cache")
        return SCRAPE_CACHE["data"]

    try:
        from jobspy import scrape_jobs
    except ImportError:
        logger.error("python-jobspy not installed")
        return []

    all_jobs: dict[str, dict] = {}

    for query in SEARCH_QUERIES:
        logger.info(f"Scraping: '{query}'")
        try:
            df = scrape_jobs(
                site_name=["linkedin", "naukri"],
                search_term=query,
                location="India",
                results_wanted=25,
                hours_old=72,
                country_linkedin="india",
                linkedin_fetch_description=True,
            )
            if df is None or df.empty:
                continue
            for _, row in df.iterrows():
                url = str(row.get("job_url", "")).strip()
                if url and url not in all_jobs:
                    all_jobs[url] = {
                        "title": str(row.get("title", "")).strip(),
                        "company": str(row.get("company", "")).strip(),
                        "jd_text": str(row.get("description", "")).strip(),
                        "url": url,
                        "location": str(row.get("location", "")).strip(),
                        "date_posted": str(row.get("date_posted", "")),
                    }
        except Exception as e:
            logger.error(f"Query '{query}' failed: {e}")
            continue

    jobs = list(all_jobs.values())
    SCRAPE_CACHE = {"timestamp": now, "data": jobs}
    logger.info(f"Total unique jobs scraped: {len(jobs)}")
    return jobs


def stage_pre_filter(jobs: list[dict]) -> list[dict]:
    """Remove staffing agencies, wrong seniority, empty JDs."""
    filtered = []
    for job in jobs:
        title_lower = job["title"].lower()
        company_lower = job["company"].lower()

        if any(s in company_lower for s in STAFFING_AGENCIES):
            continue
        if any(t in title_lower for t in SKIP_TITLES):
            continue
        if len(job.get("jd_text", "")) < 100:
            continue

        filtered.append(job)

    logger.info(f"Pre-filter: {len(jobs)} -> {len(filtered)}")
    return filtered


def stage_experience_filter(jobs: list[dict]) -> list[dict]:
    """Filter jobs by experience requirement using regex. No LLM."""
    qualified = []
    for job in jobs:
        qualifies, reason = _experience_qualifies(job["jd_text"])
        job["exp_check"] = reason
        if qualifies:
            qualified.append(job)

    logger.info(f"Experience filter: {len(jobs)} -> {len(qualified)}")
    return qualified


def stage_rank(jobs: list[dict]) -> list[dict]:
    """Rank by keyword overlap. Drop jobs below minimum skill threshold."""
    for job in jobs:
        hits, matched = _keyword_score(job["jd_text"])
        job["skill_hits"] = hits
        job["matched_keywords"] = matched

    # Drop jobs with too few matching skills
    relevant = [j for j in jobs if j["skill_hits"] >= MIN_SKILL_HITS]
    logger.info(f"Skill threshold ({MIN_SKILL_HITS}+): {len(jobs)} -> {len(relevant)}")

    ranked = sorted(relevant, key=lambda x: x["skill_hits"], reverse=True)
    if ranked:
        logger.info(f"Top: {ranked[0]['skill_hits']} hits, Bottom: {ranked[-1]['skill_hits']} hits")
    return ranked


# ---------------------------------------------------------------------------
# MCP TOOL
# ---------------------------------------------------------------------------
@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="daily_job_report",
            description=(
                "Scrapes fresh jobs from LinkedIn and Naukri across India using "
                "multiple search queries optimized for Java/Spring Boot/Angular "
                "full stack roles. Pre-filters by seniority, removes staffing "
                "agencies, deduplicates against previously seen jobs, ranks by "
                "skill keyword overlap, and returns the top matches with full "
                "JD text for your analysis. Best used with: 'Run daily_job_report "
                "and match results against my resume to find the top 3 I should apply to.'"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "top_n": {
                        "type": "integer",
                        "description": "Number of top-ranked jobs to return (default 15).",
                        "default": 15,
                    }
                },
                "required": [],
            },
        )
    ]


@server.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name != "daily_job_report":
        raise ValueError(f"Unknown tool: {name}")

    top_n = arguments.get("top_n", 15)

    # Stage 1 — Scrape (blocking I/O, run in executor)
    loop = asyncio.get_running_loop()
    raw_jobs = await loop.run_in_executor(None, stage_scrape)
    logger.info(f"[S1 Scrape] {len(raw_jobs)} raw jobs")

    # Stage 2 — Pre-filter
    filtered = stage_pre_filter(raw_jobs)
    logger.info(f"[S2 Filter] {len(filtered)} after pre-filter")

    # Stage 3 — Supabase dedup
    for job in filtered:
        job["url_hash"] = _hash_url(job["url"])

    all_hashes = [j["url_hash"] for j in filtered]
    seen = await _check_seen(all_hashes)
    unseen = [j for j in filtered if j["url_hash"] not in seen]
    logger.info(f"[S3 Dedup] {len(unseen)} new ({len(seen)} already seen)")

    # Stage 4 — Experience filter (regex, no LLM)
    exp_qualified = stage_experience_filter(unseen)
    logger.info(f"[S4 Exp] {len(exp_qualified)} experience-qualified")

    # Stage 5 — Keyword rank + minimum skill threshold
    ranked = stage_rank(exp_qualified)
    logger.info(f"[S5 Rank] {len(ranked)} after skill threshold")

    # Take top N
    top_jobs = ranked[:top_n]

    # Build results with JD preview for Perplexity
    results = []
    for rank, job in enumerate(top_jobs, 1):
        results.append({
            "rank": rank,
            "title": job["title"],
            "company": job["company"],
            "location": job["location"],
            "url": job["url"],
            "date_posted": job["date_posted"],
            "skill_hits": job["skill_hits"],
            "matched_keywords": job["matched_keywords"],
            "exp_check": job.get("exp_check", ""),
            "jd_preview": job["jd_text"][:2000],
        })

    # Mark all processed jobs as seen (not just top N)
    await _mark_seen(ranked)

    report = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "pipeline": {
            "scraped": len(raw_jobs),
            "after_pre_filter": len(filtered),
            "new_unseen": len(unseen),
            "exp_qualified": len(exp_qualified),
            "after_skill_threshold": len(ranked),
            "returned": len(results),
        },
        "jobs": results,
        "instructions": (
            "These jobs passed experience check (2.6 years) and have 3+ matching "
            "skills from the candidate's stack (Java, Spring Boot, Angular, AWS, "
            "Kafka, Redis, Docker, Microservices). Analyze each JD against the "
            "candidate's resume to pick the top 3 best matches."
        ),
    }

    return [types.TextContent(type="text", text=json.dumps(report, indent=2))]


# ---------------------------------------------------------------------------
# STARLETTE APP
# ---------------------------------------------------------------------------
sse = SseServerTransport("/messages/")


async def handle_sse(request: Request):
    async with sse.connect_sse(
        request.scope, request.receive, request._send
    ) as (read_stream, write_stream):
        await server.run(
            read_stream, write_stream, server.create_initialization_options()
        )


async def health_check(request: Request):
    return JSONResponse({"status": "healthy", "version": "7.0.0"})


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
