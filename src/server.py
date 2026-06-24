import os
import re
import json
import logging
import asyncio
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
logger = logging.getLogger("JobPipeline")

mcp = Server("IntelligentJobHunter")

# ---------------------------------------------------------------------------
# GLOBAL STATE & CONSTANTS
# ---------------------------------------------------------------------------
MY_SKILLS_LIST = [
    "Java", "Java 17", "Java 21", "Spring Boot", "Spring Cloud", "Angular", 
    "TypeScript", "JavaScript", "HTML5", "CSS3", "REST APIs", "Microservices",
    "JPA", "Hibernate", "Kafka", "Redis", "Resilience4j", "JUnit 5", "Mockito",
    "Testcontainers", "k6", "Docker", "Jenkins", "CI/CD", "Maven", "Git",
    "AWS", "EC2", "S3", "IAM", "RDS", "Lambda", "API Gateway", "CloudWatch", "SQS",
    "MySQL", "SQL", "GitHub Copilot"
]

IGNORE_COMPANIES = [
    "tcs", "infosys", "wipro", "hcl", "cognizant", "capgemini", "accenture", 
    "quest global", "teksystems", "randstad", "manpowergroup", "collabera", 
    "ascendion", "mphasis", "hexaware", "ltimindtree", "tech mahindra",
    "ibm", "mindtree", "niit", "birlasoft", "zensar", "mastech"
]

AVOID_TITLES = [
    "senior", "snr", "sr.", "lead", "principal", "staff", "architect", 
    "manager", "director", "avp", "vp", "head of", "cto", "level iii", 
    "level iv", "sde iii", "sde iv", "l5", "l6", "l7"
]

GLOBAL_SCRAPE_CACHE = {"timestamp": None, "data": []}
CACHE_DURATION_HOURS = 6

# ---------------------------------------------------------------------------
# RECOVERY & API UTILITIES
# ---------------------------------------------------------------------------
def _extract_clean_json(raw_response: str) -> dict:
    try:
        clean_text = raw_response.strip()
        if "```json" in clean_text:
            clean_text = clean_text.split("```json")[1].split("```")[0].strip()
        elif "```" in clean_text:
            clean_text = clean_text.split("
```")[1].split("```")[0].strip()
        return json.loads(clean_text)
    except Exception as e:
        logger.error(f"JSON parsing recovery triggered. Error: {str(e)}")
        return {}

async def _call_gemini_flash(prompt: str) -> str:
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        return '{"error": "GEMINI_API_KEY missing"}'
        
    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-2.5-flash:generateContent?key={api_key}"
    headers = {"Content-Type": "application/json"}
    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseMimeType": "application/json"}
    }
    
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            return response.json()['candidates'][0]['content']['parts'][0]['text']
    except Exception as e:
        logger.error(f"Gemini API failure: {str(e)}")
        return "{}"

# ---------------------------------------------------------------------------
# COGNITIVE PIPELINE EXECUTION PIPES
# ---------------------------------------------------------------------------
def run_scrape_jobs_logic(query: str, location: str, days_posted: int) -> list[dict]:
    global GLOBAL_SCRAPE_CACHE
    now = datetime.now()
    
    if GLOBAL_SCRAPE_CACHE["timestamp"] and (now - GLOBAL_SCRAPE_CACHE["timestamp"]) < timedelta(hours=CACHE_DURATION_HOURS):
        logger.info("Serving compilation dataset from cache.")
        return GLOBAL_SCRAPE_CACHE["data"]

    try:
        from jobspy import scrape_jobs
        logger.info(f"Executing deep platform fetch via JobSpy for keyword: {query}")
        df = scrape_jobs(
            site_name=["linkedin", "naukri"],
            search_term=query,
            location=location,
            results_wanted=40,
            hours_old=int(days_posted * 24),
            country_linkedin="india"
        )
        
        if df is None or df.empty:
            return []
            
        jobs_list = []
        for _, row in df.iterrows():
            jobs_list.append({
                "title": str(row.get("title", "")).strip(),
                "company": str(row.get("company", "")).strip(),
                "jd_text": str(row.get("description", "")).strip(),
                "url": str(row.get("job_url", "")).strip(),
                "date_posted": str(row.get("date_posted", ""))
            })
            
        GLOBAL_SCRAPE_CACHE["timestamp"] = now
        GLOBAL_SCRAPE_CACHE["data"] = jobs_list
        return jobs_list
    except Exception as e:
        logger.error(f"Scraper critical failure: {str(e)}")
        return []

def run_pre_filter_logic(jobs: list[dict]) -> dict:
    filtered_jobs = []
    removed_jobs = []
    
    for job in jobs:
        company_lower = job["company"].lower()
        title_lower = job["title"].lower()
        
        if any(c in company_lower for c in IGNORE_COMPANIES):
            removed_jobs.append({"job": job, "reason": "Staffing/Outsourcing vendor match"})
            continue
            
        if any(t in title_lower for t in AVOID_TITLES):
            removed_jobs.append({"job": job, "reason": "Seniority threshold match"})
            continue
            
        filtered_jobs.append(job)
    return {"filtered_jobs": filtered_jobs, "removed_jobs": removed_jobs}

async def run_parse_experience_logic(jd_text: str) -> dict:
    prompt = f"""You are an experience range extractor. Read the JD below.
Extract the required years of experience. Focus entirely on the primary backend engineering stack requirements.

Candidate has exactly 2.6 years experience.

Return ONLY this JSON configuration scheme, nothing else:
{{
  "min_exp": X,
  "max_exp": Y,
  "raw_text_found": "exact sentence from JD",
  "i_qualify": true/false
}}

JD: {jd_text}"""
    raw_response = await _call_gemini_flash(prompt)
    return _extract_clean_json(raw_response)

async def run_score_match_logic(jd_text: str) -> dict:
    prompt = f"""You are a ruthless technical auditor.
NEVER infer, assume, or hallucinate skills.
ONLY match skills that appear WORD-FOR-WORD in both the JD and the candidate resume skills list.

Candidate skills: {json.dumps(MY_SKILLS_LIST)}

JD: {jd_text}

Return ONLY this JSON structure payload:
{{
  "match_score": 0-100,
  "verified_matches": ["skill present in BOTH JD and resume - exact word"],
  "skill_gaps": ["skill in JD completely missing from resume"],
  "verdict": "APPLY NOW" | "APPLY AFTER PREP" | "SKIP",
  "verdict_reason": "one sentence explanation"
}}
Be harsh. If nothing matches, say so."""
    raw_response = await _call_gemini_flash(prompt)
    return _extract_clean_json(raw_response)

# ---------------------------------------------------------------------------
# MCP COMMAND INTERFACE DECORATORS
# ---------------------------------------------------------------------------
@mcp.list_tools()
async def handle_list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="daily_job_report",
            description="Compiles stages 1-4 into a deduplicated, audited daily short-list report.",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search target string"},
                    "location": {"type": "string", "default": "Hyderabad, India"},
                    "days_posted": {"type": "integer", "default": 3}
                },
                "required": ["query"]
            }
        )
    ]

@mcp.call_tool()
async def handle_call_tool(name: str, arguments: dict) -> list[types.TextContent]:
    if name != "daily_job_report":
        raise ValueError(f"Unknown tool target: {name}")
        
    query = arguments["query"]
    loc = arguments.get("location", "Hyderabad, India")
    days = arguments.get("days_posted", 3)
    
    # FIX: Offload sync scraping to a worker thread executor to keep Starlette alive
    loop = asyncio.get_event_loop()
    raw_list = await loop.run_in_executor(None, lambda: run_scrape_jobs_logic(query, loc, days))
    total_scraped = len(raw_list)
    
    pre_filtered_res = run_pre_filter_logic(raw_list)
    stage2_list = pre_filtered_res["filtered_jobs"]
    
    after_company_count = len([j for j in raw_list if not any(c in j["company"].lower() for c in IGNORE_COMPANIES)])
    after_title_count = len(stage2_list)
    
    # Slice to prevent API rate limiting issues 
    evaluation_candidates = stage2_list[:15]
    experience_qualified_jobs = []
    
    for item in evaluation_candidates:
        exp_audit = await run_parse_experience_logic(item["jd_text"])
        if exp_audit.get("i_qualify") is True:
            item["experience_metrics"] = exp_audit
            experience_qualified_jobs.append(item)
            
    after_exp_count = len(experience_qualified_jobs)
    
    apply_now_pool = []
    apply_after_prep_pool = []
    
    for item in experience_qualified_jobs:
        score_audit = await run_score_match_logic(item["jd_text"])
        verdict = score_audit.get("verdict", "SKIP")
        
        evaluated_job = {
            "title": item["title"],
            "company": item["company"],
            "url": item["url"],
            "match_score": score_audit.get("match_score", 0),
            "verified_matches": score_audit.get("verified_matches", []),
            "skill_gaps": score_audit.get("skill_gaps", []),
            "verdict": verdict,
            "verdict_reason": score_audit.get("verdict_reason", "Verification complete.")
        }
        
        if verdict == "APPLY NOW":
            apply_now_pool.append(evaluated_job)
        elif verdict == "APPLY AFTER PREP":
            apply_after_prep_pool.append(evaluated_job)
            
    apply_now_pool.sort(key=lambda x: x["match_score"], reverse=True)
    apply_after_prep_pool.sort(key=lambda x: x["match_score"], reverse=True)
    
    final_reporting_set = apply_now_pool[:3]
    if len(final_reporting_set) < 3:
        needed_slots = 3 - len(final_reporting_set)
        final_reporting_set.extend(apply_after_prep_pool[:needed_slots])
        
    for rank, item in enumerate(final_reporting_set, start=1):
        item["rank"] = rank
        
    report = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "total_scraped": total_scraped,
        "after_company_filter": after_company_count,
        "after_title_filter": after_title_count,
        "after_experience_filter": after_exp_count,
        "final_matches": final_reporting_set,
        "priority_order": "Prioritize applying to targets with active framework matches." if final_reporting_set else "Empty pipeline scope loop."
    }
    
    return [types.TextContent(type="text", text=json.dumps(report, indent=2))]

# ---------------------------------------------------------------------------
# ROUTER FRAMEWORK DEF
# ---------------------------------------------------------------------------
sse = SseServerTransport("/messages/")

async def handle_sse(request: Request):
    async with sse.connect_sse(request.scope, request.receive, request._send) as (read_stream, write_stream):
        await mcp.run(read_stream, write_stream, mcp.create_initialization_options())

async def health_check(request: Request):
    return JSONResponse({"status": "healthy", "pipeline_version": "6.1.0-AsyncFixed"})

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
