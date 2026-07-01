"""Settings: read-only overview + the per-channel soul editor.

Souls are plain markdown files under `souls/`, read fresh on every turn, so an
edit here takes effect on the channel's next reply — no restart. Each save is
committed to git so it survives a self-mod rollback later.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from poiesis import gitops, store

router = APIRouter()


def _resolve_soul(repo_root: str | Path, soul_path: str) -> Path | None:
    """Resolve a channel's soul_path to a real file inside souls/ (no traversal)."""
    souls_dir = (Path(repo_root) / "souls").resolve()
    target = (Path(repo_root) / soul_path).resolve()
    return target if souls_dir in target.parents else None


@router.get("/settings", response_class=HTMLResponse)
async def settings(request: Request) -> HTMLResponse:
    db = request.app.state.db
    return request.app.state.templates.TemplateResponse(
        request,
        "settings.html",
        {
            "channel_list": await store.list_channels(db),
            "schedules": await store.list_enabled_schedules(db),
            "deploys": await store.list_deploys(db, limit=10),
            "saved": request.query_params.get("saved"),
        },
    )


@router.get("/settings/soul/{channel_id}", response_class=HTMLResponse)
async def soul_edit(request: Request, channel_id: str):
    db = request.app.state.db
    env = request.app.state.env
    channel = await store.get_channel(db, channel_id)
    if not channel or not channel.get("soul_path"):
        return HTMLResponse("<h1>404</h1><p>No soul for this channel.</p>", status_code=404)
    target = _resolve_soul(env.repo_root, channel["soul_path"])
    content = target.read_text() if target and target.exists() else ""
    return request.app.state.templates.TemplateResponse(
        request,
        "soul_edit.html",
        {"channel": channel, "soul_path": channel["soul_path"], "content": content},
    )


@router.post("/settings/soul/{channel_id}")
async def soul_save(request: Request, channel_id: str, content: str = Form(...)):
    db = request.app.state.db
    env = request.app.state.env
    channel = await store.get_channel(db, channel_id)
    if not channel or not channel.get("soul_path"):
        return HTMLResponse("<h1>404</h1><p>No soul for this channel.</p>", status_code=404)
    target = _resolve_soul(env.repo_root, channel["soul_path"])
    if target is None:
        return HTMLResponse("<h1>400</h1><p>Invalid soul path.</p>", status_code=400)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content.replace("\r\n", "\n"))
    gitops.commit_file(env.repo_root, channel["soul_path"], f"edit soul: {channel_id}")
    return RedirectResponse(url=f"/settings?saved={channel_id}", status_code=303)
