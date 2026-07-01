"""Read-only settings overview: channels, schedules, recent deploys."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from poiesis import store

router = APIRouter()


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
        },
    )
