import os
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
# CANDIDATE PROFILE (embedded for semantic matching)
# ---------------------------------------------------------------------------
MY_RESUME = """
PROFESSIONAL SUMMARY
Full Stack Engineer with 2.6 years of experience delivering production-grade microservices and RESTful
APIs in a high-compliance American insurance platform using Java, Spring Boot, Angular, and AWS.
Hands-on experience working within Kafka-based event-driven architectures and Redis-backed caching
systems at enterprise scale. AWS Certified Developer – Associate with experience building scalable
enterprise microservices, event-driven systems, and REST APIs in Agile environments with global teams.

TECHNICAL SKILLS
Languages: Java 17/21, TypeScript, JavaScript
Frontend: Angular, HTML5/CSS3
Backend: Spring Boot, Spring Cloud (Config, Gateway), REST APIs, Microservices, JPA/Hibernate
Backend Tech: Kafka, Redis, Resilience4j, JUnit 5, Mockito, Docker
Cloud & DevOps: AWS (EC2, S3, IAM, RDS, Lambda, API Gateway, CloudWatch, SQS), Jenkins, CI/CD, Maven, Git
Databases: Oracle, MySQL, SQL

PROFESSIONAL EXPERIENCE
Full Stack Engineer | Accenture — Hyderabad, India | May 2024 – Present
American Insurance Domain | Oracle Legacy Migration | Microservices Platform

- Contributed to delivery of 200+ REST APIs across Oracle-to-microservices migration
- Built Experience Layer APIs orchestrating 80+ platform service operations
- Developed Angular frontend with Reactive Forms, Angular Material, RxJS
- Worked within Kafka-based event-driven architecture for async processing
- Contributed to Redis-backed caching layer with Azure AD authentication
- Monitored service health via AWS CloudWatch and EC2 console
- Resolved 40+ production defects through systematic root cause analysis
- Authored Swagger/OpenAPI documentation for all delivered APIs
- Leveraged GitHub Copilot and AI-assisted tooling for delivery acceleration
"""

CANDIDATE_YEARS = 2.6

# Multi-query strategy for broader coverage
SEARCH_QUERIES = [
    "Java Spring Boot Developer",
    "Full Stack Java Angular Developer",
    "Backend Engineer Microservices",
]

# Only block pure recruitment/staffing agencies (not IT service companies)
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

# Gemini rate limiter — max 5 concurrent calls to stay under 30 RPM
GEMINI_SEM = asyncio.Semaphore(5)


# ---------------------------------------------------------------------------
# UTILITIES
# ---------------------------------------------------------------------------
def _hash_url(url: str) -> str:
    """Short hash for dedup."""
    return hashlib.sha256(url.strip().encode()).hexdigest()[:16]


def _extract_json(raw: str) -> dict:
    """Robustly extract JSON from Gemini response (handles markdown fences)."""
    try:
        text = raw.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
        return json.loads(text)
    except Exception as e:
        logger.error(f"JSON parse failed: {e}")
        return {}


async def _gemini(prompt: str) -> str:
    """Call Gemini 2.5 Flash with rate limiting."""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return '{"error": "GEMINI_API_KEY missing"}'

    url = (
        "https://generativelanguage.googleapis.com/v1beta/models/"
        f"gemini-2.5-flash:generateContent?key={api_key}"
    )
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"},
    }

    async with GEMINI_SEM:
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.post(
                    url, headers={"Content-Type": "application/json"}, json=payload
                )
                resp.raise_for_status()
                return resp.json()["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as e:
            logger.error(f"Gemini API failed: {e}")
            return "{}"


# ---------------------------------------------------------------------------
# SUPABASE DEDUP (optional — works without it)
# ---------------------------------------------------------------------------
def _supa_headers() -> dict | None:
    """Return Supabase auth headers, or None if not configured."""
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
    """Check which job URL hashes exist in Supabase. Returns set of seen hashes."""
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
    """Insert scored jobs into Supabase seen_jobs table."""
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
                "match_score": j.get("match_score", 0),
                "verdict": j.get("verdict", ""),
            }
            for j in jobs
        ]
        endpoint = f"{base}/rest/v1/seen_jobs"
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(endpoint, headers=headers, json=rows)
            resp.raise_for_status()
            logger.info(f"Marked {len(rows)} jobs as seen in Supabase")
    except Exception as e:
        logger.warning(f"Supabase write failed: {e}")


# ---------------------------------------------------------------------------
# PIPELINE STAGES
# ---------------------------------------------------------------------------
def stage_scrape() -> list[dict]:
    """Stage 1: Multi-query scrape across platforms, deduplicate by URL."""
    global SCRAPE_CACHE
    now = datetime.now()

    if SCRAPE_CACHE["timestamp"] and (now - SCRAPE_CACHE["timestamp"]) < timedelta(hours=CACHE_HOURS):
        logger.info("Serving from in-memory cache")
        return SCRAPE_CACHE["data"]

    try:
        from jobspy import scrape_jobs
    except ImportError:
        logger.error("python-jobspy not installed")
        return []

    all_jobs: dict[str, dict] = {}  # url -> job (natural dedup across queries)

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
    """Stage 2: Remove staffing agencies, wrong seniority, empty JDs."""
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


async def stage_check_exp(jd_text: str) -> dict:
    """Stage 3: Parse experience requirement from JD via Gemini."""
    prompt = f"""Extract years of experience required from this job description.
Candidate has {CANDIDATE_YEARS} years experience.

Return ONLY this JSON:
{{
  "min_exp": <number or null if not stated>,
  "max_exp": <number or null if not stated>,
  "i_qualify": true/false,
  "reason": "one sentence"
}}

Rules:
- If experience is not clearly mentioned, i_qualify = true
- Ranges like 0-3, 1-4, 2-5, 2-4 years -> i_qualify = true
- If minimum required is 5+ years -> i_qualify = false
- Focus on primary role requirements, ignore nice-to-have

JD:
{jd_text[:2500]}"""

    raw = await _gemini(prompt)
    result = _extract_json(raw)
    if not result or "i_qualify" not in result:
        return {"i_qualify": True, "reason": "Could not parse, assuming qualified"}
    return result


async def stage_score(jd_text: str) -> dict:
    """Stage 4: Semantic resume match via Gemini."""
    prompt = f"""You are a technical recruiter evaluating job fit for a candidate.

## CANDIDATE RESUME
{MY_RESUME}

## JOB DESCRIPTION
{jd_text[:3500]}

## SCORING RUBRIC (score 0-100)
1. Tech Stack Overlap (50 points): How many candidate skills appear in the JD?
   Use SEMANTIC matching: "Spring" ~ "Spring Boot", "AWS services" ~ specific AWS tools,
   "REST" ~ "RESTful APIs", "microservice" ~ "microservices architecture".
2. Experience Level Fit (25 points): Is this appropriate for {CANDIDATE_YEARS} years?
   Ideal: 1-4 year roles. Too junior: 0-1. Too senior: 5+.
3. Role Type Fit (25 points): Backend-heavy or full-stack roles score highest.
   Pure frontend, data engineering, or DevOps-only roles score lower.

## VERDICT
- score >= 65 -> "APPLY NOW"
- score 45-64 -> "APPLY AFTER PREP"
- score < 45 -> "SKIP"

Return ONLY this JSON:
{{
  "match_score": <0-100>,
  "matched_skills": ["skills in BOTH resume and JD"],
  "skill_gaps": ["important JD skills missing from resume"],
  "verdict": "APPLY NOW" | "APPLY AFTER PREP" | "SKIP",
  "fit_reason": "2 sentence explanation"
}}"""

    raw = await _gemini(prompt)
    result = _extract_json(raw)
    if not result or "match_score" not in result:
        return {"match_score": 0, "verdict": "SKIP", "fit_reason": "Scoring failed"}
    return result


# ---------------------------------------------------------------------------
# MCP TOOL DEFINITION
# ---------------------------------------------------------------------------
@server.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="daily_job_report",
            description=(
                "Scrapes fresh jobs from LinkedIn and Naukri using multiple search queries, "
                "filters by experience and seniority, scores each job against the candidate's "
                "full resume using AI semantic matching, deduplicates against previously seen "
                "jobs, and returns the top 3 best matches with scores and reasoning."
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Optional extra search query to add alongside defaults.",
                        "default": "",
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

    extra_query = arguments.get("query", "").strip()

    # Stage 1 — Scrape (sync, run in executor)
    loop = asyncio.get_running_loop()
    raw_jobs = await loop.run_in_executor(None, stage_scrape)
    logger.info(f"[Stage 1] Scraped {len(raw_jobs)} raw jobs")

    # Stage 2 — Pre-filter
    filtered = stage_pre_filter(raw_jobs)
    logger.info(f"[Stage 2] {len(filtered)} after pre-filter")

    # Stage 3 — Supabase dedup (skip if not configured)
    for job in filtered:
        job["url_hash"] = _hash_url(job["url"])

    all_hashes = [j["url_hash"] for j in filtered]
    seen_hashes = await _check_seen(all_hashes)
    unseen = [j for j in filtered if j["url_hash"] not in seen_hashes]
    logger.info(f"[Stage 3] {len(unseen)} unseen jobs (deduped {len(seen_hashes)} seen)")

    # Stage 4 — Experience filter (parallel with rate limiting)
    exp_tasks = [stage_check_exp(j["jd_text"]) for j in unseen]
    exp_results = await asyncio.gather(*exp_tasks, return_exceptions=True)

    qualified = []
    for job, exp in zip(unseen, exp_results):
        if isinstance(exp, Exception):
            qualified.append(job)  # assume qualified on error
        elif exp.get("i_qualify", True):
            job["exp_info"] = exp
            qualified.append(job)

    logger.info(f"[Stage 4] {len(qualified)} experience-qualified")

    # Stage 5 — Semantic scoring (parallel with rate limiting)
    score_tasks = [stage_score(j["jd_text"]) for j in qualified]
    score_results = await asyncio.gather(*score_tasks, return_exceptions=True)

    scored = []
    for job, sc in zip(qualified, score_results):
        if isinstance(sc, Exception):
            continue
        scored.append({
            "title": job["title"],
            "company": job["company"],
            "location": job.get("location", ""),
            "url": job["url"],
            "url_hash": job["url_hash"],
            "match_score": sc.get("match_score", 0),
            "matched_skills": sc.get("matched_skills", []),
            "skill_gaps": sc.get("skill_gaps", []),
            "verdict": sc.get("verdict", "SKIP"),
            "fit_reason": sc.get("fit_reason", ""),
        })

    logger.info(f"[Stage 5] {len(scored)} jobs scored")

    # Stage 6 — Rank and pick top 3
    apply_now = sorted(
        [j for j in scored if j["verdict"] == "APPLY NOW"],
        key=lambda x: x["match_score"],
        reverse=True,
    )
    apply_prep = sorted(
        [j for j in scored if j["verdict"] == "APPLY AFTER PREP"],
        key=lambda x: x["match_score"],
        reverse=True,
    )

    top_3 = apply_now[:3]
    if len(top_3) < 3:
        top_3.extend(apply_prep[: 3 - len(top_3)])

    for rank, job in enumerate(top_3, 1):
        job["rank"] = rank

    # Stage 7 — Mark all scored jobs as seen
    await _mark_seen(scored)

    report = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "pipeline": {
            "scraped": len(raw_jobs),
            "after_filter": len(filtered),
            "new_unseen": len(unseen),
            "exp_qualified": len(qualified),
            "scored": len(scored),
            "apply_now_count": len(apply_now),
            "apply_prep_count": len(apply_prep),
        },
        "top_matches": top_3,
        "summary": (
            f"Found {len(top_3)} matches from {len(raw_jobs)} scraped jobs."
            if top_3
            else "No strong matches today. New jobs will appear tomorrow."
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
