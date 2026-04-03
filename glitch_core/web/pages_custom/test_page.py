from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from glitch_core.web.engine import PageMeta

router = APIRouter(prefix="/test_page")

PAGE_META = PageMeta(
    title="Test Page",
    icon="📄",
    nav_section="custom",
    nav_order=50,
    route_prefix="/test_page"
)

@router.get("/", response_class=HTMLResponse)
async def test_page(request: Request):
    """Test page with Hello World."""
    return await request.app.state.templates.TemplateResponse(
        "test_page.html",
        {"request": request}
    )