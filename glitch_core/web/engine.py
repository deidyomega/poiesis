from __future__ import annotations

import importlib
import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

from pydantic import BaseModel

logger = logging.getLogger(__name__)


class PageMeta(BaseModel):
    """Metadata for a registered page module."""
    title: str
    icon: str = ""
    nav_section: str = "core"
    nav_order: int = 50
    show_in_nav: bool = True
    route_prefix: str = ""
    badge_count: int | None = None


class PageEntry(BaseModel):
    """A registered page in the engine."""
    meta: PageMeta
    module_name: str
    module_path: str
    is_custom: bool = False

    model_config = {"arbitrary_types_allowed": True}


class PageEngine:
    """Discovers and manages page modules from pages/ and pages_custom/ directories."""

    def __init__(self, pages_dir: Path, pages_custom_dir: Path) -> None:
        self.pages_dir = pages_dir
        self.pages_custom_dir = pages_custom_dir
        self.pages: dict[str, PageEntry] = {}
        self._routers: list[Any] = []

    def discover_pages(self) -> list[PageEntry]:
        """Scan both page directories and register all page modules."""
        self.pages.clear()
        self._routers.clear()

        # Core pages
        self._scan_directory(self.pages_dir, is_custom=False)

        # Custom pages (AI-generated)
        if self.pages_custom_dir.exists():
            self._scan_directory(self.pages_custom_dir, is_custom=True)

        return list(self.pages.values())

    def _scan_directory(self, directory: Path, is_custom: bool) -> None:
        """Import page modules from a directory."""
        if not directory.exists():
            return

        for py_file in sorted(directory.glob("*.py")):
            if py_file.name.startswith("_"):
                continue

            module_name = f"glitch_page_{'custom_' if is_custom else ''}{py_file.stem}"
            try:
                spec = importlib.util.spec_from_file_location(module_name, py_file)
                if spec is None or spec.loader is None:
                    continue

                module = importlib.util.module_from_spec(spec)
                sys.modules[module_name] = module
                spec.loader.exec_module(module)

                router = getattr(module, "router", None)
                page_meta = getattr(module, "PAGE_META", None)

                if router is None:
                    logger.warning("Page module %s has no router, skipping", py_file.name)
                    continue

                if page_meta is None:
                    page_meta = PageMeta(title=py_file.stem.replace("_", " ").title())

                entry = PageEntry(
                    meta=page_meta,
                    module_name=module_name,
                    module_path=str(py_file),
                    is_custom=is_custom,
                )
                self.pages[py_file.stem] = entry
                self._routers.append(router)
                logger.info("Registered page: %s (%s)", page_meta.title, py_file.stem)

            except Exception:
                logger.exception("Failed to load page module: %s", py_file)

    def get_nav_items(self) -> dict[str, list[PageEntry]]:
        """Return page entries grouped by nav_section, sorted by nav_order."""
        sections: dict[str, list[PageEntry]] = {}
        for entry in self.pages.values():
            if not entry.meta.show_in_nav:
                continue
            section = entry.meta.nav_section
            if section not in sections:
                sections[section] = []
            sections[section].append(entry)

        for entries in sections.values():
            entries.sort(key=lambda e: e.meta.nav_order)

        return sections

    def get_routers(self) -> list[Any]:
        """Return all discovered FastAPI routers."""
        return self._routers

    def reload_custom_pages(self) -> list[Any]:
        """Remove old custom pages from sys.modules and re-discover.

        Returns the list of NEW routers that need to be mounted to the app.
        The caller is responsible for mounting them via app.include_router().
        """
        to_remove = [
            name for name in sys.modules
            if name.startswith("glitch_page_custom_")
        ]
        for name in to_remove:
            del sys.modules[name]

        # Count how many custom routers we had before
        num_custom = sum(1 for v in self.pages.values() if v.is_custom)

        # Remove custom entries from pages dict
        self.pages = {
            k: v for k, v in self.pages.items() if not v.is_custom
        }

        # Remove old custom routers from the list (they were appended last)
        if num_custom > 0:
            self._routers = self._routers[:-num_custom]

        # Track position before re-scan
        routers_before = len(self._routers)

        # Re-scan custom directory
        if self.pages_custom_dir.exists():
            self._scan_directory(self.pages_custom_dir, is_custom=True)

        # Return only the newly added routers
        new_routers = self._routers[routers_before:]
        logger.info("Reloaded custom pages. Total pages: %d, new routers: %d",
                     len(self.pages), len(new_routers))
        return new_routers
