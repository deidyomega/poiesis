from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from glitch_core.web.engine import PageMeta

router = APIRouter(prefix="/test_page")

PAGE_META = PageMeta(
    title="Test Page",
    icon="file",
    nav_section="custom",
    nav_order=50,
    route_prefix="/test_page"
)

@router.get("/", response_class=HTMLResponse)
async def test_page(request: Request):
    return request.app.state.templates.TemplateResponse(
        request,
        "test_page.html"
    )