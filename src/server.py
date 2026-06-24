"""
LinkedIn MCP Server — Streamable HTTP with full OAuth discovery + DCR
Perplexity requires:
  1. /.well-known/oauth-protected-resource
  2. /.well-known/oauth-authorization-server  (with registration_endpoint)
  3. /oauth/register  (Dynamic Client Registration - RFC 7591)
  4. /mcp  (the actual MCP endpoint)
"""

import json
import os
import secrets
import uuid
from urllib.parse import urlencode

import uvicorn
from mcp.server.fastmcp import FastMCP
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

mcp = FastMCP("linkedin-mcp")

# In-memory client store (resets on restart — fine for open server)
REGISTERED_CLIENTS: dict[str, dict] = {}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

EXP_CODES = {
    "internship": "1",
    "entry": "2",
    "associate": "3",
    "mid-senior": "4",
    "director": "5",
    "executive": "6",
}


def _build_linkedin_search_url(
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
    if experience_level and experience_level in EXP_CODES:
        params["f_E"] = EXP_CODES[experience_level]
    if job_type:
        params["f_JT"] = job_type
    if days:
        params["f_TPR"] = f"r{days * 86400}"
    return "https://www.linkedin.com/jobs/search/?" + urlencode(params)


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def linkedin_search_jobs(keyword: str, location: str = "", remote: bool = False) -> str:
    """Build a public LinkedIn job search URL for any keyword + location."""
    url = _build_linkedin_search_url(keyword=keyword, location=location, remote=remote)
    parts = [f"LinkedIn job search for '{keyword}'"]
    if location:
        parts.append(f" in {location}")
    if remote:
        parts.append(" (Remote only)")
    return "\n".join(parts) + f"\n\nOpen in browser:\n{url}"


@mcp.tool()
def linkedin_build_search_url(
    keyword: str,
    location: str = "",
    remote: bool = False,
    easy_apply: bool = False,
    experience_level: str = "",
    job_type: str = "",
    days: int | None = None,
) -> str:
    """
    Build a LinkedIn job search URL with full filter support.
    experience_level: internship | entry | associate | mid-senior | director | executive
    job_type: F (full-time) | P (part-time) | C (contract) | T (temporary) | I (internship)
    days: posted within last N days
    """
    url = _build_linkedin_search_url(
        keyword=keyword, location=location, remote=remote,
        easy_apply=easy_apply, experience_level=experience_level,
        job_type=job_type, days=days,
    )
    return f"LinkedIn search URL with filters:\n{url}"


@mcp.tool()
def linkedin_get_job_details(job_id: str) -> str:
    """Return the canonical public LinkedIn job details page URL for a given job ID."""
    return (
        f"LinkedIn job details URL:\n"
        f"https://www.linkedin.com/jobs/view/{job_id}/\n\n"
        f"Open the URL above in your browser to see full job details."
    )


@mcp.tool()
def linkedin_easy_apply_search(
    keyword: str, location: str = "", experience_level: str = "", days: int = 7
) -> str:
    """Shortcut: build a LinkedIn Easy Apply job search URL."""
    url = _build_linkedin_search_url(
        keyword=keyword, location=location, easy_apply=True,
        experience_level=experience_level, days=days,
    )
    return (
        f"Easy Apply jobs for '{keyword}'"
        + (f" in {location}" if location else "")
        + f" (last {days} days):\n\n{url}"
    )


@mcp.tool()
def linkedin_remote_jobs(
    keyword: str, experience_level: str = "", easy_apply: bool = True, days: int = 7
) -> str:
    """Find fully remote LinkedIn jobs for any role/skill."""
    url = _build_linkedin_search_url(
        keyword=keyword, remote=True, easy_apply=easy_apply,
        experience_level=experience_level, days=days,
    )
    return f"Remote jobs for '{keyword}' (last {days} days):\n\n{url}"


# ---------------------------------------------------------------------------
# OAuth / Discovery endpoints
# ---------------------------------------------------------------------------

BASE_URL = os.environ.get("RENDER_EXTERNAL_URL", "http://localhost:8000").rstrip("/")


async def oauth_protected_resource(request: Request) -> JSONResponse:
    """RFC 9470 — resource server metadata."""
    return JSONResponse({
        "resource": BASE_URL,
        "authorization_servers": [BASE_URL],
        "bearer_methods_supported": ["header"],
        "resource_documentation": f"{BASE_URL}/mcp",
    })


async def oauth_authorization_server(request: Request) -> JSONResponse:
    """RFC 8414 — authorization server metadata WITH registration_endpoint."""
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
    """OpenID Connect discovery stub."""
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
    """
    RFC 7591 Dynamic Client Registration.
    Perplexity calls this to get a client_id + client_secret automatically.
    We auto-approve every registration — no real auth needed.
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    client_id = str(uuid.uuid4())
    client_secret = secrets.token_urlsafe(32)

    client_data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "client_name": body.get("client_name", "Perplexity MCP Client"),
        "redirect_uris": body.get("redirect_uris", []),
        "grant_types": body.get("grant_types", ["authorization_code"]),
        "response_types": body.get("response_types", ["code"]),
        "token_endpoint_auth_method": body.get("token_endpoint_auth_method", "none"),
    }
    REGISTERED_CLIENTS[client_id] = client_data

    return JSONResponse(client_data, status_code=201)


async def oauth_token(request: Request) -> JSONResponse:
    """Token endpoint — issues a dummy bearer token (server is open/public)."""
    return JSONResponse({
        "access_token": secrets.token_urlsafe(32),
        "token_type": "bearer",
        "expires_in": 86400,
        "scope": "mcp",
    })


async def oauth_authorize(request: Request) -> JSONResponse:
    """Authorization endpoint stub."""
    redirect_uri = request.query_params.get("redirect_uri", "")
    code = secrets.token_urlsafe(16)
    state = request.query_params.get("state", "")
    if redirect_uri:
        from starlette.responses import RedirectResponse
        sep = "&" if "?" in redirect_uri else "?"
        return RedirectResponse(url=f"{redirect_uri}{sep}code={code}&state={state}")
    return JSONResponse({"code": code, "state": state})


async def health(request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok", "service": "linkedin-mcp-server"})


# ---------------------------------------------------------------------------
# Build combined ASGI app
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
    app = build_app()
    uvicorn.run(app, host="0.0.0.0", port=port)
