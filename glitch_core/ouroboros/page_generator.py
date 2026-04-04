from __future__ import annotations

import logging
from typing import Any

from glitch_core.ouroboros.sandbox import SafeFileWriter
from glitch_core.schemas import PromotionResult

logger = logging.getLogger(__name__)

MAX_RETRIES = 3

CODER_PAGE_PROMPT = """\
Generate a custom web page for Glitch Core.

Tech stack:
- Python: FastAPI APIRouter for routes
- Templates: Jinja2 extending base.html
- Interactivity: HTMX (hx-get, hx-post, hx-target, hx-swap) -- NOT custom JavaScript
- Styling: Tailwind CSS via CDN with the glitch- color palette
- Database: Firestore async client (available as request.app.state.db)
- Templates: available as request.app.state.templates

The Python module MUST define:
- `router = APIRouter(prefix="/your_prefix")`
- `PAGE_META = PageMeta(title="...", icon="🔧", nav_section="custom", nav_order=50, route_prefix="/your_prefix")`
  - The `icon` field MUST be a single emoji character (e.g. "🧪", "📊", "🎨"), NOT a Font Awesome class or icon name

Import PageMeta from: `from glitch_core.web.engine import PageMeta`

CRITICAL — route handlers MUST follow this exact pattern:
```python
@router.get("/")
async def my_page(request: Request):
    templates = request.app.state.templates
    return templates.TemplateResponse(request, "my_template.html", context={{"key": "value"}})
```

Rules:
- ALWAYS get templates from `request.app.state.templates` inside the route handler
- NEVER create your own Jinja2Templates instance
- NEVER import templates from anywhere — they are ONLY on request.app.state
- NEVER use `await` with TemplateResponse — it returns a Response, not a coroutine
- TemplateResponse signature is: `templates.TemplateResponse(request, template_name, context={{...}})`
  - First arg MUST be the Request object (NOT the template name!)
  - Second arg is the template name string
  - Third arg is optional context dict
  - WRONG: `templates.TemplateResponse("name.html", {{"request": request}})`
  - RIGHT: `templates.TemplateResponse(request, "name.html")`
- Only import from: fastapi, glitch_core.web.engine (PageMeta), standard library

The Jinja2 template MUST:
- Start with `{{% extends "base.html" %}}`
- Use `{{% block content %}}...{{% endblock %}}`
- Use Tailwind classes with glitch- prefix: bg-glitch-surface, text-glitch-text, border-glitch-border, bg-glitch-accent, text-glitch-muted, etc.
- Use rounded-glitch for border radius

Available Firestore collections:
- sessions/{{id}}/messages -- chat messages
- agents/{{id}} -- agent configs
- core_memories/{{id}} -- long-term memories
- journals/{{id}} -- journal entries
- meta/project, meta/theme, meta/compaction_config
- compaction_runs/{{id}} -- compaction audit logs
- workers/{{id}} -- worker registrations

All Firestore queries must be async: `async for doc in db.collection("x").stream()`
Filter placeholder docs: `if doc.id == "_placeholder": continue`
"""


async def generate_page(
    coder_agent: Any,
    safe_writer: SafeFileWriter,
    page_name: str,
    user_prompt: str,
) -> PromotionResult:
    """Generate a page (Python route + Jinja2 template) via the coder agent.

    The coder agent generates both files. SafeFileWriter validates and promotes
    them atomically. If validation fails (fixable), retries up to 3 times.
    """
    page_filename = f"{page_name}.py"
    template_filename = f"{page_name}.html"

    prompt = (
        f"{CODER_PAGE_PROMPT}\n\n"
        f"Page name: {page_name}\n"
        f"Page filename: {page_filename}\n"
        f"Template filename: {template_filename}\n"
        f"User request: {user_prompt}\n\n"
        f"Return TWO files separated by the marker '---TEMPLATE---' on its own line.\n"
        f"First the Python module, then the marker, then the Jinja2 template.\n"
        f"No markdown fences, no explanation."
    )

    last_result = PromotionResult(success=False, error="No attempts made")

    for attempt in range(MAX_RETRIES):
        try:
            result = await coder_agent.run(prompt)
            output = result.output

            if hasattr(output, "code"):
                output = output.code
            if not isinstance(output, str):
                output = str(output)

            # Split into page code and template code
            if "---TEMPLATE---" not in output:
                last_result = PromotionResult(
                    success=False,
                    error="Coder did not include ---TEMPLATE--- separator between Python and HTML.",
                )
                prompt = (
                    f"Your output was missing the ---TEMPLATE--- separator.\n"
                    f"Return TWO files: first the Python module, then '---TEMPLATE---' on its own line, "
                    f"then the Jinja2 template.\n\n"
                    f"Original request: {user_prompt}"
                )
                continue

            parts = output.split("---TEMPLATE---", 1)
            page_code = parts[0].strip()
            template_code = parts[1].strip()

            # Attempt promotion
            last_result = safe_writer.write_page(
                page_filename, page_code, template_filename, template_code
            )

            if last_result.success:
                return last_result

            # Retry with error context if fixable
            fixable = [f for f in last_result.validation_failures if f.fixable]
            if not fixable:
                return last_result

            error_context = "\n".join(f"- {f.error}" for f in fixable)
            prompt = (
                f"The previous page code failed validation:\n"
                f"{error_context}\n\n"
                f"Fix the code. Remember: Python module first, then ---TEMPLATE---, then HTML.\n"
                f"Original request: {user_prompt}"
            )
            logger.info("Page generation retry %d/%d for %s", attempt + 1, MAX_RETRIES, page_name)

        except Exception as e:
            logger.exception("Page generation failed on attempt %d", attempt + 1)
            last_result = PromotionResult(success=False, error=str(e))

    return last_result
