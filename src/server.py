"""
Job Search MCP Server — LinkedIn + Naukri
Streamable HTTP with OAuth discovery + Dynamic Client Registration
Deploy on Render. Connect via Perplexity custom connector.
"""

import os
import secrets
import uuid
from urllib.parse import urlencode, quote_plus

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, RedirectResponse
from starlette.routing import Mount, Route

mcp = FastMCP("job-search-mcp")

# In-memory client store
REGISTERED_CLIENTS: dict[str, dict] = {}
BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:8000").rstrip("/")

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

LINKEDIN_EXP = {
    "internship": "1",
    "entry": "2",
    "associate": "3",
    "mid-senior": "4",
    "director": "5",
    "executive": "6",
}

NAUKRI_EXP = {
    "0": "0to1",
    "1": "1to3",
    "2": "2to4",
    "3": "3to5",
    "4": "4to6",
    "5": "5to8",
    "7": "7to10",
    "10": "10to15",
}

NAUKRI_LOCATIONS = {
    "hyderabad": "Hyderabad",
    "bangalore": "Bangalore",
    "mumbai": "Mumbai",
    "chennai": "Chennai",
    "pune": "Pune",
    "delhi": "Delhi",
    "noida": "Noida",
    "gurgaon": "Gurgaon",
    "remote": "Work+from+Home",
}


# ---------------------------------------------------------------------------
# LINKEDIN TOOLS
# ---------------------------------------------------------------------------

def _linkedin_url(
    keyword: str,
    location: str = "",
    remote: bool = False,
    easy_apply: bool = False,
    experience_level: str = "",
    job_type: str = "",
    days: int | None = None,
) -> str:
    params: dict[str, str] = {"keywords": keyword}
    if location:
        params["location"] = location
    if remote:
        params["f_WT"] = "2"
    if easy_apply:
        params["f_AL"] = "true"
    if experience_level and experience_level in LINKEDIN_EXP:
        params["f_E"] = LINKEDIN_EXP[experience_level]
    if job_type:
        params["f_JT"] = job_type
    if days:
        params["f_TPR"] = f"r{days * 86400}"
    return "https://www.linkedin.com/jobs/search/?" + urlencode(params)


@mcp.tool()
def linkedin_search_jobs(
    keyword: str,
    location: str = "",
    remote: bool = False,
    easy_apply: bool = False,
    experience_level: str = "",
    days: int = 7,
) -> str:
    """
    Search LinkedIn jobs by keyword and location.
    experience_level: internship | entry | associate | mid-senior | director | executive
    Returns a direct LinkedIn job search URL with filters applied.
    """
    url = _linkedin_url(
        keyword=keyword,
        location=location,
        remote=remote,
        easy_apply=easy_apply,
        experience_level=experience_level,
        days=days,
    )
    label = f"LinkedIn: '{keyword}'"
    if location:
        label += f" in {location}"
    if easy_apply:
        label += " | Easy Apply"
    if remote:
        label += " | Remote"
    label += f" | Last {days} days"
    return f"{label}\n\n🔗 {url}"


@mcp.tool()
def linkedin_remote_jobs(
    keyword: str,
    experience_level: str = "",
    easy_apply: bool = True,
    days: int = 7,
) -> str:
    """
    Find fully remote LinkedIn jobs for any role.
    experience_level: internship | entry | associate | mid-senior | director | executive
    """
    url = _linkedin_url(
        keyword=keyword,
        remote=True,
        easy_apply=easy_apply,
        experience_level=experience_level,
        days=days,
    )
    return f"LinkedIn Remote: '{keyword}' | Last {days} days\n\n🔗 {url}"


# ---------------------------------------------------------------------------
# NAUKRI TOOLS
# ---------------------------------------------------------------------------

def _naukri_url(
    keyword: str,
    location: str = "",
    experience_min: int = 0,
    experience_max: int = 0,
    salary_min: int = 0,
    job_type: str = "",
    days: int = 0,
) -> str:
    """
    Build a Naukri.com job search URL with filters.
    """
    slug = keyword.lower().replace(" ", "-")
    loc_slug = location.lower().replace(" ", "-") if location else ""

    # Naukri URL format: /keyword-jobs[/in-location][?filters]
    if loc_slug:
        base = f"https://www.naukri.com/{slug}-jobs-in-{loc_slug}"
    else:
        base = f"https://www.naukri.com/{slug}-jobs"

    params: dict[str, str] = {}
    if experience_min or experience_max:
        params["experience"] = f"{experience_min}to{experience_max}"
    if salary_min:
        params["salary"] = str(salary_min)
    if job_type:
        params["jobType"] = job_type
    if days:
        params["jobAge"] = str(days)

    if params:
        return base + "?" + urlencode(params)
    return base


@mcp.tool()
def naukri_search_jobs(
    keyword: str,
    location: str = "",
    experience_min: int = 0,
    experience_max: int = 0,
    days: int = 7,
) -> str:
    """
    Search Naukri.com for jobs by keyword, location and experience range.
    Returns a direct Naukri search URL.
    Example: keyword='Java Full Stack Developer', location='Hyderabad', experience_min=2, experience_max=4
    """
    url = _naukri_url(
        keyword=keyword,
        location=location,
        experience_min=experience_min,
        experience_max=experience_max,
        days=days,
    )
    label = f"Naukri: '{keyword}'"
    if location:
        label += f" in {location}"
    if experience_min or experience_max:
        label += f" | {experience_min}-{experience_max} yrs exp"
    label += f" | Last {days} days"
    return f"{label}\n\n🔗 {url}"


@mcp.tool()
def naukri_easy_apply_jobs(
    keyword: str,
    location: str = "",
    experience_min: int = 0,
    experience_max: int = 0,
    days: int = 7,
) -> str:
    """
    Search Naukri.com for Easy Apply jobs (1-click apply).
    Returns direct Naukri Easy Apply filtered URL.
    """
    url = _naukri_url(
        keyword=keyword,
        location=location,
        experience_min=experience_min,
        experience_max=experience_max,
        days=days,
    )
    # Append Naukri easy apply filter
    sep = "&" if "?" in url else "?"
    url = url + sep + "jobType=easyApply"
    label = f"Naukri Easy Apply: '{keyword}'"
    if location:
        label += f" in {location}"
    if experience_min or experience_max:
        label += f" | {experience_min}-{experience_max} yrs exp"
    return f"{label}\n\n🔗 {url}"


@mcp.tool()
def naukri_remote_jobs(
    keyword: str,
    experience_min: int = 0,
    experience_max: int = 0,
    days: int = 7,
) -> str:
    """
    Search Naukri.com for remote / work from home jobs.
    """
    url = _naukri_url(
        keyword=keyword,
        location="work-from-home",
        experience_min=experience_min,
        experience_max=experience_max,
        days=days,
    )
    label = f"Naukri Remote: '{keyword}'"
    if experience_min or experience_max:
        label += f" | {experience_min}-{experience_max} yrs exp"
    return f"{label}\n\n🔗 {url}"


@mcp.tool()
def naukri_company_jobs(
    company: str,
    keyword: str = "",
    location: str = "",
) -> str:
    """
    Find jobs at a specific company on Naukri.
    Example: company='TCS', keyword='Java Developer', location='Hyderabad'
    """
    slug = company.lower().replace(" ", "-")
    base = f"https://www.naukri.com/{slug}-jobs"
    params: dict[str, str] = {}
    if keyword:
        params["title"] = keyword
    if location:
        params["location"] = location
    url = base + ("?" + urlencode(params) if params else "")
    return f"Naukri: Jobs at {company}" + (f" | {keyword}" if keyword else "") + f"\n\n🔗 {url}"


@mcp.tool()
def job_search_both(
    keyword: str,
    location: str = "",
    experience_min: int = 0,
    experience_max: int = 0,
    easy_apply: bool = True,
    days: int = 7,
) -> str:
    """
    Search BOTH LinkedIn and Naukri simultaneously for any role.
    Returns search URLs for both platforms in one response.
    Perfect for maximum job discovery.
    Example: keyword='Java Full Stack Developer', location='Hyderabad', experience_min=2, experience_max=4
    """
    # LinkedIn
    exp_level = "associate" if experience_min >= 2 else "entry"
    li_url = _linkedin_url(
        keyword=keyword,
        location=location,
        easy_apply=easy_apply,
        experience_level=exp_level,
        days=days,
    )

    # Naukri
    nk_url = _naukri_url(
        keyword=keyword,
        location=location,
        experience_min=experience_min,
        experience_max=experience_max,
        days=days,
    )
    if easy_apply:
        sep = "&" if "?" in nk_url else "?"
        nk_url = nk_url + sep + "jobType=easyApply"

    label = f"'{keyword}'"
    if location:
        label += f" in {location}"
    if experience_min or experience_max:
        label += f" | {experience_min}-{experience_max} yrs"

    return (
        f"🔍 Job Search: {label}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━\n"
        f"💼 LinkedIn:\n🔗 {li_url}\n\n"
        f"📋 Naukri:\n🔗 {nk_url}"
    )


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
    """RFC 7591 Dynamic Client Registration — auto-approve every client."""
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
        "service": "job-search-mcp",
        "tools": [
            "linkedin_search_jobs",
            "linkedin_remote_jobs",
            "naukri_search_jobs",
            "naukri_easy_apply_jobs",
            "naukri_remote_jobs",
            "naukri_company_jobs",
            "job_search_both",
        ]
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
