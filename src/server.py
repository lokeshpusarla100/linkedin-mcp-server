"""
LinkedIn MCP Server — HTTP transport (Streamable HTTP / SSE)
Deploy to Render for free. Paste the public URL into Perplexity custom connector.
"""

import json
import os
from urllib.parse import urlencode

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("linkedin-mcp")

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
# Tools
# ---------------------------------------------------------------------------


@mcp.tool()
def linkedin_search_jobs(
    keyword: str,
    location: str = "",
    remote: bool = False,
) -> str:
    """
    Build a public LinkedIn job search URL for any keyword + location.
    Returns a ready-to-open browser URL. No LinkedIn auth required.
    """
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
    job_type: F (full-time) | P (part-time) | C (contract) | T (temporary) | I (internship) | V (volunteer)
    days: posted within last N days
    """
    url = _build_linkedin_search_url(
        keyword=keyword,
        location=location,
        remote=remote,
        easy_apply=easy_apply,
        experience_level=experience_level,
        job_type=job_type,
        days=days,
    )
    return f"LinkedIn search URL with filters:\n{url}"


@mcp.tool()
def linkedin_get_job_details(job_id: str) -> str:
    """
    Return the canonical public LinkedIn job details page URL for a given job ID.
    """
    return (
        f"LinkedIn job details URL:\n"
        f"https://www.linkedin.com/jobs/view/{job_id}/\n\n"
        f"Open the URL above in your browser to see full job details."
    )


@mcp.tool()
def linkedin_easy_apply_search(
    keyword: str,
    location: str = "",
    experience_level: str = "",
    days: int = 7,
) -> str:
    """
    Shortcut: build a LinkedIn Easy Apply job search URL.
    Perfect for quickly finding jobs you can apply to with one click.
    experience_level: internship | entry | associate | mid-senior | director | executive
    """
    url = _build_linkedin_search_url(
        keyword=keyword,
        location=location,
        easy_apply=True,
        experience_level=experience_level,
        days=days,
    )
    return (
        f"Easy Apply jobs for '{keyword}'"
        + (f" in {location}" if location else "")
        + f" (last {days} days):\n\n{url}"
    )


@mcp.tool()
def linkedin_remote_jobs(
    keyword: str,
    experience_level: str = "",
    easy_apply: bool = True,
    days: int = 7,
) -> str:
    """
    Find fully remote LinkedIn jobs for any role/skill.
    Returns a ready-to-open search URL filtered to remote positions.
    """
    url = _build_linkedin_search_url(
        keyword=keyword,
        remote=True,
        easy_apply=easy_apply,
        experience_level=experience_level,
        days=days,
    )
    return f"Remote jobs for '{keyword}' (last {days} days):\n\n{url}"


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    mcp.run(transport="streamable-http", host="0.0.0.0", port=port, path="/mcp")
