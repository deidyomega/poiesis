"""The single tool #spice gets: fetch a URL and hand it back as readable markdown.

Featherless (and most OpenAI-compatible providers) don't ship a built-in web/JSON
tool, so this is it — a plain GET that parses JSON and flattens it into markdown so
the model can actually read the payload instead of a wall of braces.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx

from poiesis.config import PoiesisEnv

logger = logging.getLogger(__name__)

# OpenAI function-tool schema advertised to the model.
FETCH_TOOL_SCHEMA: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "fetch",
        "description": (
            "Fetch a URL over HTTP GET and return its body as readable markdown. "
            "Omit `url` to get the user's challenges (the default endpoint). A JSON list "
            "of challenges renders as the challenge list; other JSON is flattened into a "
            "markdown outline; non-JSON is returned as truncated text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "url": {"type": "string",
                        "description": "The URL to GET. Omit to fetch the user's challenges."},
            },
        },
    },
}

_MAX_BODY = 20_000  # cap what we feed back to the model (chars)


def challenges_to_markdown(items: list[dict[str, Any]]) -> str:
    """Render a Challenge[] array as reference markdown for ideation.

    Adapted from the prior project's `challengesAsMarkdown`; the opaque `id` slug is
    dropped — Prompta only needs style/category/points/description to riff on."""
    if not items:
        return "_No challenges defined._"
    lines = []
    for c in items:
        meta = [f"category: {c.get('category')}", f"{c.get('point_value')} pts"]
        if c.get("min_req"):
            meta.append(f"min {c['min_req']}")
        if c.get("important"):
            meta.append("IMPORTANT")
        desc = re.sub(r"\s+", " ", str(c.get("description", ""))).strip()
        lines.append(f"- ({', '.join(meta)}): {desc}")
    return "\n".join(lines)


def _looks_like_challenges(data: Any) -> bool:
    return (
        isinstance(data, list) and bool(data)
        and all(isinstance(x, dict) for x in data)
        and any(("point_value" in x or "category" in x) for x in data)
    )


def json_to_markdown(value: Any, depth: int = 0, max_depth: int = 6) -> str:
    """Flatten parsed JSON into a compact, readable markdown outline."""
    indent = "  " * depth
    if depth > max_depth:
        return f"{indent}…"
    if isinstance(value, dict):
        if not value:
            return f"{indent}_(empty object)_"
        lines = []
        for k, v in value.items():
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{indent}- **{k}**:")
                lines.append(json_to_markdown(v, depth + 1, max_depth))
            else:
                lines.append(f"{indent}- **{k}**: {_scalar(v)}")
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return f"{indent}_(empty list)_"
        lines = []
        for item in value:
            if isinstance(item, (dict, list)) and item:
                lines.append(f"{indent}-")
                lines.append(json_to_markdown(item, depth + 1, max_depth))
            else:
                lines.append(f"{indent}- {_scalar(item)}")
        return "\n".join(lines)
    return f"{indent}{_scalar(value)}"


def _scalar(v: Any) -> str:
    if v is None:
        return "_null_"
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


CHALLENGES_SETTING = "spice_challenges_md"  # DB settings key for the cached markdown


def _cf_headers(env: PoiesisEnv) -> dict[str, str]:
    """Cloudflare Access service-token headers, if configured (else empty)."""
    cid = env.spice_challenges_cf_client_id
    secret = env.spice_challenges_cf_client_secret
    if cid and secret:
        return {"CF-Access-Client-Id": cid, "CF-Access-Client-Secret": secret}
    return {}


async def fetch_challenges_markdown(env: PoiesisEnv) -> str:
    """GET the challenges JSON and render it as markdown. Returns '' on any problem."""
    url = env.spice_challenges_url
    if not url:
        return ""
    headers = {"Accept": "application/json", **_cf_headers(env)}
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
            resp = await client.get(url, headers=headers)
    except httpx.HTTPError as e:
        logger.warning("challenges fetch failed: %s: %s", type(e).__name__, e)
        return ""
    if resp.status_code != 200:
        # A 302 to cloudflareaccess.com means the endpoint is still Access-gated.
        logger.warning("challenges fetch got %s (Access-gated?) for %s", resp.status_code, url)
        return ""
    try:
        data = resp.json()
    except ValueError:
        logger.warning("challenges endpoint did not return JSON")
        return ""
    items = data if isinstance(data, list) else data.get("challenges", [])
    return challenges_to_markdown(items) if isinstance(items, list) else ""


async def refresh_challenges(db, env: PoiesisEnv) -> int:
    """Fetch challenges and cache the markdown in settings. Returns line count (0 if none)."""
    md = await fetch_challenges_markdown(env)
    from poiesis import store
    await store.set_setting(db, CHALLENGES_SETTING, md)
    return md.count("\n") + 1 if md else 0


async def run_fetch(arguments: str | dict[str, Any], env: PoiesisEnv | None = None) -> str:
    """Execute the `fetch` tool. Returns markdown (or a readable error string).

    With no `url`, falls back to the configured challenges endpoint
    (POIESIS_SPICE_CHALLENGES_URL) so the model can just "get the challenges".
    """
    try:
        args = json.loads(arguments) if isinstance(arguments, str) else (arguments or {})
    except json.JSONDecodeError:
        return "⚠️ fetch: could not parse tool arguments as JSON."
    url = (args or {}).get("url") or getattr(env, "spice_challenges_url", "")
    if not url or not isinstance(url, str):
        return ("⚠️ fetch: no `url` given and no default challenges endpoint configured "
                "(set POIESIS_SPICE_CHALLENGES_URL).")
    if not url.startswith(("http://", "https://")):
        return f"⚠️ fetch: `{url}` is not an http(s) URL."
    try:
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            resp = await client.get(url, headers={"Accept": "application/json, text/*"})
    except httpx.HTTPError as e:
        return f"⚠️ fetch: request failed — {type(e).__name__}: {e}"
    status = f"`GET {url}` → {resp.status_code}"
    body = resp.text
    ctype = resp.headers.get("content-type", "")
    if "json" in ctype or body.strip()[:1] in ("{", "["):
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            data = None
        if data is not None:
            if _looks_like_challenges(data):
                md = challenges_to_markdown(data)
                return f"The user's current challenges (point-earning tasks):\n\n{md[:_MAX_BODY]}"
            return f"{status}\n\n{json_to_markdown(data)[:_MAX_BODY]}"
    return f"{status}\n\n{body[:_MAX_BODY]}"
