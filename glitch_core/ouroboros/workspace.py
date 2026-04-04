from __future__ import annotations

import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from glitch_core.schemas import ScriptResult, WorkspaceEntry, WorkspaceFile, WorkspaceTree

logger = logging.getLogger(__name__)

# Size limits
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50MB per file
MAX_WORKSPACE_SIZE = 500 * 1024 * 1024  # 500MB total

# Directories the workspace must never resolve into
FORBIDDEN_PREFIXES = [
    "glitch_core",
    "tools",
    "soul",
    ".git",
    ".claude",
]


class Workspace:
    """Free-form user zone. The daemon never imports from here.

    The AI builds arbitrary projects for the user — scripts, websites, data files.
    No validation beyond path safety. No hot-reload. No system impact.
    """

    def __init__(self, root: Path | None = None) -> None:
        if root is None:
            root = Path(__file__).parent.parent.parent / "workspace"
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _resolve_safe(self, path: str) -> Path:
        """Resolve a path safely within the workspace. Raises on traversal."""
        resolved = (self.root / path).resolve()

        # Must be inside workspace root
        if not str(resolved).startswith(str(self.root)):
            raise PermissionError(f"Path traversal blocked: {path}")

        # Must not resolve into forbidden directories
        # Check relative to the repo root (workspace's parent)
        repo_root = self.root.parent
        try:
            rel = resolved.relative_to(repo_root)
            first_part = str(rel).split(os.sep)[0]
            if first_part in FORBIDDEN_PREFIXES:
                raise PermissionError(f"Forbidden directory: {first_part}")
        except ValueError:
            pass  # Not relative to repo root — that's fine, it's inside workspace

        # Also block ~/.glitch
        glitch_home = Path.home() / ".glitch"
        if str(resolved).startswith(str(glitch_home.resolve())):
            raise PermissionError("Cannot write to ~/.glitch from workspace")

        return resolved

    def _check_total_size(self, additional_bytes: int = 0) -> None:
        """Check total workspace size doesn't exceed limit."""
        total = sum(f.stat().st_size for f in self.root.rglob("*") if f.is_file())
        if total + additional_bytes > MAX_WORKSPACE_SIZE:
            raise PermissionError(
                f"Workspace size limit exceeded: {total + additional_bytes} bytes "
                f"(max {MAX_WORKSPACE_SIZE})"
            )

    def write(self, path: str, content: str | bytes) -> WorkspaceFile:
        """Write a file to the workspace."""
        resolved = self._resolve_safe(path)

        if isinstance(content, str):
            data = content.encode("utf-8")
        else:
            data = content

        if len(data) > MAX_FILE_SIZE:
            raise PermissionError(
                f"File too large: {len(data)} bytes (max {MAX_FILE_SIZE})"
            )

        self._check_total_size(len(data))

        created = not resolved.exists()
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_bytes(data)

        rel_path = str(resolved.relative_to(self.root))
        logger.info("Workspace write: %s (%d bytes)", rel_path, len(data))

        return WorkspaceFile(
            path=str(resolved),
            workspace_relative=rel_path,
            size_bytes=len(data),
            created=created,
        )

    def read(self, path: str) -> str:
        """Read a text file from the workspace."""
        resolved = self._resolve_safe(path)
        if not resolved.exists():
            raise FileNotFoundError(f"Not found: {path}")
        return resolved.read_text(encoding="utf-8")

    def read_bytes(self, path: str) -> bytes:
        """Read a binary file from the workspace."""
        resolved = self._resolve_safe(path)
        if not resolved.exists():
            raise FileNotFoundError(f"Not found: {path}")
        return resolved.read_bytes()

    def list(self, path: str = ".") -> WorkspaceTree:
        """List files in a workspace directory."""
        resolved = self._resolve_safe(path)
        if not resolved.exists():
            return WorkspaceTree()
        if not resolved.is_dir():
            raise ValueError(f"Not a directory: {path}")

        entries: list[WorkspaceEntry] = []
        total = 0

        for item in sorted(resolved.iterdir()):
            if item.name.startswith("."):
                continue
            stat = item.stat()
            size = stat.st_size if item.is_file() else 0
            total += size
            entries.append(WorkspaceEntry(
                name=item.name,
                path=str(item.relative_to(self.root)),
                is_dir=item.is_dir(),
                size_bytes=size,
                modified_at=datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
            ))

        return WorkspaceTree(files=entries, total_size_bytes=total)

    def delete(self, path: str) -> bool:
        """Delete a file or directory from the workspace."""
        resolved = self._resolve_safe(path)
        if not resolved.exists():
            return False

        import shutil
        if resolved.is_dir():
            shutil.rmtree(resolved)
        else:
            resolved.unlink()

        logger.info("Workspace delete: %s", path)
        return True

    def mkdir(self, path: str) -> WorkspaceFile:
        """Create a directory in the workspace."""
        resolved = self._resolve_safe(path)
        created = not resolved.exists()
        resolved.mkdir(parents=True, exist_ok=True)

        return WorkspaceFile(
            path=str(resolved),
            workspace_relative=str(resolved.relative_to(self.root)),
            size_bytes=0,
            created=created,
        )

    def run_script(
        self,
        script_path: str,
        args: list[str] | None = None,
        timeout: int = 300,
        interpreter: str | None = None,
    ) -> ScriptResult:
        """Execute a script from the workspace in the workspace.

        The interpreter is auto-detected from file extension if not specified.
        The script gets the user's normal environment (including API keys),
        since these are the user's own scripts — not Ouroboros system code.
        """
        resolved = self._resolve_safe(script_path)
        if not resolved.exists():
            return ScriptResult(
                exit_code=1,
                stdout="",
                stderr=f"Script not found: {script_path}",
                timed_out=False,
            )

        # Auto-detect interpreter from extension
        if interpreter is None:
            ext = resolved.suffix.lower()
            interpreter_map = {
                ".py": "python3",
                ".js": "node",
                ".ts": "npx ts-node",
                ".sh": "bash",
                ".rb": "ruby",
                ".pl": "perl",
                ".php": "php",
            }
            interpreter = interpreter_map.get(ext, "python3")

        cmd = interpreter.split() + [str(resolved)]
        if args:
            cmd.extend(args)

        try:
            result = subprocess.run(
                cmd,
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            return ScriptResult(
                exit_code=result.returncode,
                stdout=result.stdout[-5000:] if len(result.stdout) > 5000 else result.stdout,
                stderr=result.stderr[-2000:] if len(result.stderr) > 2000 else result.stderr,
                timed_out=False,
            )
        except subprocess.TimeoutExpired:
            return ScriptResult(
                exit_code=-1,
                stdout="",
                stderr=f"Script timed out after {timeout}s",
                timed_out=True,
            )
        except Exception as e:
            return ScriptResult(
                exit_code=-1,
                stdout="",
                stderr=str(e),
                timed_out=False,
            )
