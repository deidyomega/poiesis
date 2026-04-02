from __future__ import annotations

from pydantic import BaseModel, Field


class ThemeColors(BaseModel):
    """Color palette for the Glitch UI theme."""
    bg: str = "#0f0f0f"
    surface: str = "#1a1a1a"
    border: str = "#2a2a2a"
    accent: str = "#6366f1"
    text: str = "#e4e4e7"
    muted: str = "#71717a"
    success: str = "#22c55e"
    warning: str = "#eab308"
    error: str = "#ef4444"

    # Category tag colors
    tag_identity: str = "#8b5cf6"
    tag_relationship: str = "#ec4899"
    tag_preference: str = "#06b6d4"
    tag_fact: str = "#6366f1"
    tag_skill: str = "#22c55e"
    tag_medical: str = "#ef4444"
    tag_work: str = "#f59e0b"
    tag_hobby: str = "#14b8a6"
    tag_other: str = "#71717a"


class GlitchTheme(BaseModel):
    """Full UI theme configuration stored at /meta/theme."""
    name: str = "default"
    display_name: str = "Default"
    colors: ThemeColors = Field(default_factory=ThemeColors)

    # Typography
    font_family: str = "Inter, system-ui, sans-serif"
    font_cdn: str | None = "https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap"

    # Shape
    border_radius: str = "0.5rem"
    border_width: str = "1px"

    # Branding
    app_name: str = "Glitch Core"
    app_icon: str = "🧠"
    logo_url: str | None = None
    favicon_url: str | None = None

    # Layout
    sidebar_width: str = "16rem"
    compact_mode: bool = False


def _hex_to_rgb(hex_color: str) -> tuple[int, int, int]:
    """Convert hex color to RGB tuple."""
    hex_color = hex_color.lstrip("#")
    return (
        int(hex_color[0:2], 16),
        int(hex_color[2:4], 16),
        int(hex_color[4:6], 16),
    )


def _relative_luminance(r: int, g: int, b: int) -> float:
    """Calculate relative luminance per WCAG 2.0."""
    def linearize(c: int) -> float:
        s = c / 255.0
        return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4
    return 0.2126 * linearize(r) + 0.7152 * linearize(g) + 0.0722 * linearize(b)


def _passes_contrast_check(fg: str, bg: str, ratio: float = 4.5) -> bool:
    """Check if foreground/background colors pass WCAG AA contrast ratio."""
    fg_rgb = _hex_to_rgb(fg)
    bg_rgb = _hex_to_rgb(bg)
    lum_fg = _relative_luminance(*fg_rgb)
    lum_bg = _relative_luminance(*bg_rgb)
    lighter = max(lum_fg, lum_bg)
    darker = min(lum_fg, lum_bg)
    contrast = (lighter + 0.05) / (darker + 0.05)
    return contrast >= ratio


PRESET_THEMES: dict[str, GlitchTheme] = {
    "default": GlitchTheme(),
    "pink_gothic": GlitchTheme(
        name="pink_gothic",
        display_name="Pink Gothic",
        colors=ThemeColors(
            bg="#1a0a14",
            surface="#2d1225",
            border="#4a1e3d",
            accent="#ec4899",
            text="#fce7f3",
            muted="#9d7a8e",
            success="#22c55e",
            warning="#eab308",
            error="#ef4444",
            tag_identity="#d946ef",
            tag_relationship="#f472b6",
            tag_preference="#67e8f9",
            tag_fact="#a78bfa",
            tag_skill="#4ade80",
            tag_medical="#f87171",
            tag_work="#fbbf24",
            tag_hobby="#2dd4bf",
            tag_other="#9d7a8e",
        ),
        font_family="Crimson Text, serif",
        font_cdn="https://fonts.googleapis.com/css2?family=Crimson+Text:ital,wght@0,400;0,600;0,700;1,400&display=swap",
        app_name="Glitch Core",
        app_icon="🦇",
        border_radius="0.25rem",
    ),
    "corporate": GlitchTheme(
        name="corporate",
        display_name="Corporate",
        colors=ThemeColors(
            bg="#f8fafc",
            surface="#ffffff",
            border="#e2e8f0",
            accent="#2563eb",
            text="#1e293b",
            muted="#64748b",
            success="#16a34a",
            warning="#ca8a04",
            error="#dc2626",
            tag_identity="#7c3aed",
            tag_relationship="#db2777",
            tag_preference="#0891b2",
            tag_fact="#2563eb",
            tag_skill="#16a34a",
            tag_medical="#dc2626",
            tag_work="#d97706",
            tag_hobby="#0d9488",
            tag_other="#64748b",
        ),
        font_family="system-ui, -apple-system, sans-serif",
        font_cdn=None,
        app_name="Glitch Core",
        app_icon="📊",
        border_radius="0.375rem",
        border_width="1px",
    ),
}
