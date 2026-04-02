from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse

from glitch_core.web.engine import PageMeta

PAGE_META = PageMeta(
    title="Workspace",
    icon="📁",
    nav_section="core",
    nav_order=5,
    route_prefix="/workspace",
)

router = APIRouter(prefix="/workspace")


@router.get("", response_class=HTMLResponse)
@router.get("/{path:path}", response_model=None)
async def workspace_browser(request: Request, path: str = ""):
    """Workspace file browser. Directories render as HTML, files download."""
    templates = request.app.state.templates
    workspace = getattr(request.app.state, "workspace", None)

    if workspace is None:
        return templates.TemplateResponse(request, "workspace.html", context={
            "entries": [],
            "current_path": "",
            "parent_path": None,
            "breadcrumbs": [],
            "total_size": 0,
            "error": "Workspace not initialized.",
        })

    resolved = workspace._resolve_safe(path) if path else workspace.root

    # If it's a file, serve it as download
    if resolved.is_file():
        return FileResponse(
            path=str(resolved),
            filename=resolved.name,
            media_type="application/octet-stream",
        )

    # Directory listing
    try:
        tree = workspace.list(path or ".")
    except Exception as e:
        return templates.TemplateResponse(request, "workspace.html", context={
            "entries": [],
            "current_path": path,
            "parent_path": None,
            "breadcrumbs": [],
            "total_size": 0,
            "error": str(e),
        })

    # Build breadcrumbs
    breadcrumbs = []
    if path:
        parts = path.strip("/").split("/")
        for i, part in enumerate(parts):
            crumb_path = "/".join(parts[: i + 1])
            breadcrumbs.append({"name": part, "path": crumb_path})

    parent_path = "/".join(path.strip("/").split("/")[:-1]) if path else None

    return templates.TemplateResponse(request, "workspace.html", context={
        "entries": tree.files,
        "current_path": path,
        "parent_path": parent_path,
        "breadcrumbs": breadcrumbs,
        "total_size": tree.total_size_bytes,
        "error": None,
    })


@router.post("/delete/{path:path}")
async def workspace_delete(request: Request, path: str) -> RedirectResponse:
    """Delete a file or directory from the workspace."""
    workspace = getattr(request.app.state, "workspace", None)
    if workspace:
        workspace.delete(path)

    # Redirect to parent directory
    parent = "/".join(path.strip("/").split("/")[:-1])
    return RedirectResponse(url=f"/workspace/{parent}" if parent else "/workspace", status_code=303)
