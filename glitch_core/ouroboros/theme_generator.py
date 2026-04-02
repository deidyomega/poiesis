from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from glitch_core.web.theming import GlitchTheme, _passes_contrast_check

logger = logging.getLogger(__name__)

MAX_RETRIES = 2


async def generate_theme(
    coder_agent: Any,
    db: Any,
    prompt: str,
) -> GlitchTheme | None:
    """Generate a theme from a natural language description.

    The coder agent generates a GlitchTheme JSON object. Contrast validation
    ensures readability. Retries if contrast fails.
    """
    schema = GlitchTheme.model_json_schema()
    agent_prompt = (
        f"Generate a UI color theme for a web application.\n\n"
        f"User request: {prompt}\n\n"
        f"Return a JSON object matching this exact schema:\n"
        f"{schema}\n\n"
        f"Requirements:\n"
        f"- All color values must be hex strings (e.g. '#1a1a2e')\n"
        f"- Text colors must have good contrast against background colors (WCAG AA)\n"
        f"- The 'name' field should be a slug (lowercase, underscores)\n"
        f"- The 'display_name' should be human-readable\n"
        f"- Choose a Google Font that matches the theme mood, or use system fonts\n"
        f"- If using a Google Font, set font_cdn to the Google Fonts CSS URL\n\n"
        f"Return ONLY the JSON object, no markdown fences."
    )

    for attempt in range(MAX_RETRIES + 1):
        try:
            result = await coder_agent.run(agent_prompt)
            output = result.output

            if hasattr(output, "code"):
                output = output.code
            if not isinstance(output, str):
                output = str(output)

            # Strip markdown fences if the model added them
            output = output.strip()
            if output.startswith("```"):
                output = output.split("\n", 1)[1] if "\n" in output else output
            if output.endswith("```"):
                output = output.rsplit("```", 1)[0]
            output = output.strip()

            # Parse as GlitchTheme
            import json
            theme_data = json.loads(output)
            theme = GlitchTheme.model_validate(theme_data)

            # Contrast check
            contrast_issues = _check_contrast(theme)
            if contrast_issues:
                if attempt < MAX_RETRIES:
                    issues_str = "\n".join(f"- {i}" for i in contrast_issues)
                    agent_prompt = (
                        f"The generated theme has contrast issues:\n{issues_str}\n\n"
                        f"Fix the colors to pass WCAG AA contrast ratio (4.5:1 minimum).\n"
                        f"Original request: {prompt}\n\n"
                        f"Return the corrected JSON object only."
                    )
                    logger.info("Theme contrast retry %d/%d", attempt + 1, MAX_RETRIES)
                    continue
                else:
                    logger.warning("Theme has contrast issues but max retries reached")

            # Write to Firestore
            if db is not None:
                # Save current theme to history
                current_doc = await db.collection("meta").document("theme").get()
                if current_doc.exists:
                    hist_id = f"theme_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
                    current = current_doc.to_dict()
                    current["archived_at"] = datetime.utcnow()
                    await db.collection("theme_history").document(hist_id).set(current)

                # Apply new theme
                await db.collection("meta").document("theme").set(theme.model_dump())

            logger.info("Theme generated: %s", theme.display_name)
            return theme

        except Exception as e:
            logger.exception("Theme generation failed on attempt %d", attempt + 1)
            if attempt >= MAX_RETRIES:
                return None

    return None


def _check_contrast(theme: GlitchTheme) -> list[str]:
    """Check critical color pairs for WCAG AA contrast."""
    issues: list[str] = []

    pairs = [
        ("text", "bg", theme.colors.text, theme.colors.bg),
        ("text", "surface", theme.colors.text, theme.colors.surface),
        ("muted", "bg", theme.colors.muted, theme.colors.bg),
        ("muted", "surface", theme.colors.muted, theme.colors.surface),
    ]

    for fg_name, bg_name, fg, bg in pairs:
        try:
            if not _passes_contrast_check(fg, bg):
                issues.append(
                    f"{fg_name} ({fg}) on {bg_name} ({bg}) fails WCAG AA contrast"
                )
        except Exception:
            pass  # Invalid hex — will fail elsewhere

    return issues
