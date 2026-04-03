from fastapi import APIRouter, Request
from glitch_core.web.engine import PageMeta

router = APIRouter(prefix="/test")

PAGE_META = PageMeta(
    title="Test Page",
    icon="fas fa-vial",
    nav_section="custom",
    nav_order=50,
    route_prefix="/test"
)

@router.get("/")
async def test_page(request: Request):
    """Render the test page."""
    from glitch_core.web.dependencies import templates
    return templates.TemplateResponse(
        "test_page.html",
        {"request": request}
    )