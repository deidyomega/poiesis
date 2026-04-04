from __future__ import annotations

import ast
import importlib
import importlib.util
import logging
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import jinja2

from glitch_core.schemas import PromotionResult, ValidationFailure, ValidationStage

logger = logging.getLogger(__name__)

# Dangerous patterns blocked by AST scan
DANGEROUS_CALLS = {
    "os.remove", "os.unlink", "os.rmdir", "os.system", "os.popen",
    "os.execl", "os.execle", "os.execlp", "os.execlpe",
    "os.execv", "os.execve", "os.execvp", "os.execvpe",
    "shutil.rmtree", "shutil.move",
    "subprocess.run", "subprocess.call", "subprocess.Popen",
}

DANGEROUS_IMPORTS = {"os", "subprocess", "shutil", "sys", "ctypes"}


class SafeFileWriter:
    """Enforcement layer for the System trust zone.

    The ONLY way tools, pages, and config get written to disk.
    Every write follows: temp -> validate -> git snapshot -> promote -> git commit -> reload.
    If anything fails, the live system is never modified.
    """

    def __init__(
        self,
        repo_root: Path | None = None,
        page_engine: Any | None = None,
    ) -> None:
        if repo_root is None:
            repo_root = Path(__file__).parent.parent.parent
        self.repo_root = repo_root.resolve()
        self.tools_dir = self.repo_root / "tools"
        self.pages_dir = self.repo_root / "glitch_core" / "web" / "pages_custom"
        self.templates_dir = self.repo_root / "glitch_core" / "web" / "templates_custom"
        self.page_engine = page_engine
        self.app: Any | None = None  # Set by daemon after create_app()

        # Ensure directories exist
        self.tools_dir.mkdir(exist_ok=True)
        self.pages_dir.mkdir(exist_ok=True)
        self.templates_dir.mkdir(exist_ok=True)

    def write_tool(self, filename: str, code: str) -> PromotionResult:
        """The ONLY way a tool module gets written to tools/.

        Validates, git snapshots, promotes, commits, and attempts hot-reload.
        """
        if not filename.endswith(".py"):
            filename = f"{filename}.py"

        if "/" in filename or "\\" in filename or ".." in filename:
            return PromotionResult(
                success=False,
                error="Invalid filename -- no paths, just a name like 'my_tool.py'",
            )

        target = self.tools_dir / filename

        # Validate
        failures = _validate_python(code, filename)
        if failures:
            non_fixable = [f for f in failures if not f.fixable]
            if non_fixable:
                return PromotionResult(
                    success=False,
                    error=non_fixable[0].error,
                    validation_failures=failures,
                )
            return PromotionResult(
                success=False,
                error=failures[0].error,
                validation_failures=failures,
            )

        # Git snapshot current state
        _git_snapshot(
            self.repo_root,
            [str(target)] if target.exists() else [],
            f"ouroboros: snapshot before tool '{filename}'",
        )

        # Promote
        try:
            target.write_text(code, encoding="utf-8")
        except Exception as e:
            return PromotionResult(success=False, error=f"Write failed: {e}")

        # Git commit
        commit_sha = _git_commit(
            self.repo_root,
            [str(target)],
            f"ouroboros: promote tool '{filename}'",
        )

        # Hot-reload
        reload_error = _try_reload_tool(filename)
        if reload_error:
            logger.warning("Tool reload failed, reverting: %s", reload_error)
            if commit_sha:
                _git_revert(self.repo_root, commit_sha)
            return PromotionResult(
                success=False,
                error=f"Reload failed (reverted): {reload_error}",
                rollback_id=commit_sha,
            )

        logger.info("Tool promoted: %s (sha=%s)", filename, commit_sha)
        return PromotionResult(
            success=True,
            artifact_path=str(target),
            rollback_id=commit_sha,
        )

    def read_tool(self, tool_name: str) -> str | None:
        """Read the source code of a tool. Returns code string or None."""
        filename = f"{tool_name}.py" if not tool_name.endswith(".py") else tool_name
        target = self.tools_dir / filename
        if not target.exists():
            return None
        return target.read_text(encoding="utf-8")

    def list_tools(self) -> list[dict[str, str]]:
        """List all tool files in tools/."""
        tools = []
        for py_file in sorted(self.tools_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            tools.append({
                "name": py_file.stem,
                "filename": py_file.name,
                "size": py_file.stat().st_size,
            })
        return tools

    def delete_tool(self, tool_name: str) -> PromotionResult:
        """Delete a tool file."""
        filename = f"{tool_name}.py" if not tool_name.endswith(".py") else tool_name
        target = self.tools_dir / filename
        if not target.exists():
            return PromotionResult(success=False, error=f"Tool '{tool_name}' not found")

        _git_snapshot(self.repo_root, [str(target)], f"ouroboros: snapshot before deleting tool '{tool_name}'")
        target.unlink()
        commit_sha = _git_commit(self.repo_root, [str(target)], f"ouroboros: delete tool '{tool_name}'")

        logger.info("Tool deleted: %s", tool_name)
        return PromotionResult(success=True, rollback_id=commit_sha)

    def write_page(
        self,
        page_filename: str,
        page_code: str,
        template_filename: str,
        template_code: str,
    ) -> PromotionResult:
        """The ONLY way a page gets written to pages_custom/ + templates_custom/.

        Both files validated together. Either both promote or neither does.
        """
        if not page_filename.endswith(".py"):
            page_filename = f"{page_filename}.py"
        if not template_filename.endswith(".html"):
            template_filename = f"{template_filename}.html"

        for fn in (page_filename, template_filename):
            if "/" in fn or "\\" in fn or ".." in fn:
                return PromotionResult(success=False, error=f"Invalid filename: {fn}")

        page_target = self.pages_dir / page_filename
        template_target = self.templates_dir / template_filename

        # Validate Python
        py_failures = _validate_python(page_code, page_filename)
        if py_failures:
            return PromotionResult(
                success=False,
                error=py_failures[0].error,
                validation_failures=py_failures,
            )

        # Page-specific validation: catch common mistakes
        page_failures = _validate_page_patterns(page_code, page_filename)
        if page_failures:
            return PromotionResult(
                success=False,
                error=page_failures[0].error,
                validation_failures=page_failures,
            )

        # Validate template
        tmpl_failures = _validate_template(template_code, template_filename)
        if tmpl_failures:
            return PromotionResult(
                success=False,
                error=tmpl_failures[0].error,
                validation_failures=tmpl_failures,
            )

        # Git snapshot
        existing = [str(f) for f in (page_target, template_target) if f.exists()]
        _git_snapshot(self.repo_root, existing, f"ouroboros: snapshot before page '{page_filename}'")

        # Promote both files
        try:
            page_target.write_text(page_code, encoding="utf-8")
            template_target.write_text(template_code, encoding="utf-8")
        except Exception as e:
            return PromotionResult(success=False, error=f"Write failed: {e}")

        # Git commit
        commit_sha = _git_commit(
            self.repo_root,
            [str(page_target), str(template_target)],
            f"ouroboros: promote page '{page_filename}'",
        )

        # Hot-reload pages
        if self.page_engine:
            try:
                new_routers = self.page_engine.reload_custom_pages()
                # Mount any newly discovered routers to the live app
                if new_routers and self.app:
                    for new_router in new_routers:
                        self.app.include_router(new_router)
                    logger.info("Mounted %d new router(s) to live app", len(new_routers))
            except Exception as e:
                logger.warning("Page reload failed, reverting: %s", e)
                if commit_sha:
                    _git_revert(self.repo_root, commit_sha)
                return PromotionResult(
                    success=False,
                    error=f"Page reload failed (reverted): {e}",
                    rollback_id=commit_sha,
                )

        logger.info("Page promoted: %s + %s (sha=%s)", page_filename, template_filename, commit_sha)
        return PromotionResult(
            success=True,
            artifact_path=str(page_target),
            rollback_id=commit_sha,
        )

    def read_page(self, page_name: str) -> dict[str, str] | None:
        """Read the current code for a custom page. Returns {page_code, template_code} or None."""
        page_file = self.pages_dir / f"{page_name}.py"
        template_file = self.templates_dir / f"{page_name}.html"

        if not page_file.exists():
            return None

        result = {"page_code": page_file.read_text(encoding="utf-8")}
        if template_file.exists():
            result["template_code"] = template_file.read_text(encoding="utf-8")
        else:
            result["template_code"] = ""

        return result

    def list_pages(self) -> list[dict[str, str]]:
        """List all custom pages."""
        pages = []
        for py_file in sorted(self.pages_dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            name = py_file.stem
            has_template = (self.templates_dir / f"{name}.html").exists()
            pages.append({
                "name": name,
                "page_file": str(py_file),
                "has_template": has_template,
            })
        return pages

    def delete_page(self, page_name: str) -> PromotionResult:
        """Delete a custom page and its template."""
        page_file = self.pages_dir / f"{page_name}.py"
        template_file = self.templates_dir / f"{page_name}.html"

        if not page_file.exists():
            return PromotionResult(success=False, error=f"Page '{page_name}' not found")

        # Git snapshot before deletion
        existing = [str(f) for f in (page_file, template_file) if f.exists()]
        _git_snapshot(self.repo_root, existing, f"ouroboros: snapshot before deleting page '{page_name}'")

        # Delete files
        if page_file.exists():
            page_file.unlink()
        if template_file.exists():
            template_file.unlink()

        # Git commit the deletion
        commit_sha = _git_commit(
            self.repo_root, existing,
            f"ouroboros: delete page '{page_name}'",
        )

        # Hot-reload
        if self.page_engine:
            try:
                self.page_engine.reload_custom_pages()
                # Note: deleted pages — FastAPI doesn't support removing routes,
                # but the page won't be in nav and will 404 on next restart.
            except Exception:
                logger.exception("Page reload after deletion failed (non-fatal)")

        logger.info("Page deleted: %s", page_name)
        return PromotionResult(success=True, rollback_id=commit_sha)

    def rollback(self, commit_sha: str) -> bool:
        """Manually rollback a specific promotion by git SHA."""
        return _git_revert(self.repo_root, commit_sha)


class RuntimeCircuitBreaker:
    """Watches for errors after Ouroboros promotions and auto-reverts if needed.

    If the error rate spikes within 5 minutes of a promotion, the promotion
    is assumed to be the cause and is automatically reverted.
    """

    def __init__(self, safe_writer: SafeFileWriter, threshold: int = 3) -> None:
        self.safe_writer = safe_writer
        self.threshold = threshold
        self._last_promotion_sha: str | None = None
        self._last_promotion_time: float = 0.0
        self._errors_since_promotion: int = 0
        self._stability_window: float = 300.0  # 5 minutes

    def record_promotion(self, sha: str | None) -> None:
        """Called after every successful Ouroboros promotion."""
        if sha:
            self._last_promotion_sha = sha
            self._last_promotion_time = time.time()
            self._errors_since_promotion = 0
            logger.info("Circuit breaker: tracking promotion %s", sha[:8])

    def record_error(self, error: Exception) -> None:
        """Called on every agent execution error. May trigger automatic rollback."""
        if not self._last_promotion_sha:
            return

        if time.time() - self._last_promotion_time > self._stability_window:
            self._last_promotion_sha = None
            self._errors_since_promotion = 0
            return

        self._errors_since_promotion += 1
        logger.warning(
            "Circuit breaker: error %d/%d since promotion %s",
            self._errors_since_promotion, self.threshold,
            self._last_promotion_sha[:8],
        )

        if self._errors_since_promotion >= self.threshold:
            logger.critical(
                "Circuit breaker TRIGGERED: reverting promotion %s after %d errors",
                self._last_promotion_sha[:8], self._errors_since_promotion,
            )
            self.safe_writer.rollback(self._last_promotion_sha)
            self._last_promotion_sha = None
            self._errors_since_promotion = 0


# ── Validation Functions ───────────────────────────────────────────────────

def _validate_page_patterns(code: str, filename: str) -> list[ValidationFailure]:
    """Catch common page code mistakes that would cause runtime errors."""
    failures: list[ValidationFailure] = []

    # Check for `await` on TemplateResponse (it returns a Response, not a coroutine)
    if "await" in code and "TemplateResponse" in code:
        if "return await" in code and "TemplateResponse" in code:
            failures.append(ValidationFailure(
                stage=ValidationStage.AST_SCAN,
                error=(
                    "Do NOT use `await` with TemplateResponse. It returns a Response object, "
                    "not a coroutine.\n"
                    "WRONG: return await templates.TemplateResponse(...)\n"
                    "RIGHT: return templates.TemplateResponse(request, \"name.html\")"
                ),
                fixable=True,
            ))

    # Check for creating own Jinja2Templates instance (causes cache/hash errors)
    if "Jinja2Templates(" in code:
        failures.append(ValidationFailure(
            stage=ValidationStage.AST_SCAN,
            error=(
                "Do NOT create your own Jinja2Templates instance. "
                "Use `templates = request.app.state.templates` inside route handlers instead."
            ),
            fixable=True,
        ))

    # Check for importing templates from nonexistent modules
    if "from glitch_core.web.dependencies import" in code:
        failures.append(ValidationFailure(
            stage=ValidationStage.AST_SCAN,
            error=(
                "glitch_core.web.dependencies does not exist. "
                "Use `templates = request.app.state.templates` inside route handlers instead."
            ),
            fixable=True,
        ))

    # Check for old-style TemplateResponse(name, context) — MUST be (request, name, context)
    # AST check: find calls to TemplateResponse where first arg is a string literal
    try:
        tree = ast.parse(code)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                # Match: *.TemplateResponse(...) or TemplateResponse(...)
                is_tr = (
                    (isinstance(func, ast.Attribute) and func.attr == "TemplateResponse")
                    or (isinstance(func, ast.Name) and func.id == "TemplateResponse")
                )
                if is_tr and node.args:
                    first_arg = node.args[0]
                    if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
                        failures.append(ValidationFailure(
                            stage=ValidationStage.AST_SCAN,
                            error=(
                                "Wrong TemplateResponse signature. The first argument MUST be "
                                "the request object, not the template name.\n"
                                "WRONG: templates.TemplateResponse(\"name.html\", {\"request\": request})\n"
                                "RIGHT: templates.TemplateResponse(request, \"name.html\", context={...})"
                            ),
                            fixable=True,
                        ))
                        break  # One error is enough
    except SyntaxError:
        pass  # Will be caught by _validate_python

    return failures


def _validate_python(code: str, filename: str) -> list[ValidationFailure]:
    """Validate Python code through multiple stages."""
    failures: list[ValidationFailure] = []

    # Stage 1: Syntax
    try:
        compile(code, filename, "exec")
    except SyntaxError as e:
        failures.append(ValidationFailure(
            stage=ValidationStage.SYNTAX,
            error=f"Syntax error at line {e.lineno}: {e.msg}",
            fixable=True,
        ))
        return failures

    # Stage 2: AST scan for dangerous patterns
    try:
        tree = ast.parse(code)
        dangerous = _scan_ast(tree)
        if dangerous:
            for pattern in dangerous:
                failures.append(ValidationFailure(
                    stage=ValidationStage.AST_SCAN,
                    error=f"Blocked dangerous pattern: {pattern}",
                    fixable=False,
                ))
            return failures
    except Exception as e:
        failures.append(ValidationFailure(
            stage=ValidationStage.AST_SCAN,
            error=f"AST parse failed: {e}",
            fixable=True,
        ))
        return failures

    # Stage 3: Import in isolated subprocess
    import_error = _validate_import(code, filename)
    if import_error:
        failures.append(ValidationFailure(
            stage=ValidationStage.IMPORT,
            error=import_error,
            fixable=True,
        ))

    return failures


def _scan_ast(tree: ast.AST) -> list[str]:
    """Walk the AST looking for dangerous patterns."""
    dangerous: list[str] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] in DANGEROUS_IMPORTS:
                    dangerous.append(f"import {alias.name}")

        elif isinstance(node, ast.ImportFrom):
            if node.module and node.module.split(".")[0] in DANGEROUS_IMPORTS:
                dangerous.append(f"from {node.module} import ...")

        elif isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                full_name = f"{func.value.id}.{func.attr}"
                if full_name in DANGEROUS_CALLS:
                    dangerous.append(f"call to {full_name}()")

    return dangerous


def _validate_import(code: str, filename: str) -> str | None:
    """Try to import the code in an isolated subprocess."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_file = Path(tmpdir) / filename
        tmp_file.write_text(code, encoding="utf-8")

        clean_env = {
            "PATH": "/usr/bin:/usr/local/bin",
            "HOME": tmpdir,
            "PYTHONPATH": str(Path(__file__).parent.parent.parent),
        }

        import_script = (
            f"import importlib.util; "
            f"spec = importlib.util.spec_from_file_location('test', '{tmp_file}'); "
            f"mod = importlib.util.module_from_spec(spec); "
            f"spec.loader.exec_module(mod)"
        )

        try:
            result = subprocess.run(
                [sys.executable, "-c", import_script],
                capture_output=True, text=True, timeout=10,
                env=clean_env, cwd=tmpdir,
            )
            if result.returncode != 0:
                return f"Import failed: {result.stderr.strip()[-500:]}"
        except subprocess.TimeoutExpired:
            return "Import timed out (10s limit)"
        except Exception as e:
            return f"Import check failed: {e}"

    return None


def _validate_template(code: str, filename: str) -> list[ValidationFailure]:
    """Validate a Jinja2 template."""
    failures: list[ValidationFailure] = []

    env = jinja2.Environment()
    try:
        env.parse(code)
    except jinja2.TemplateSyntaxError as e:
        failures.append(ValidationFailure(
            stage=ValidationStage.SYNTAX,
            error=f"Template syntax error at line {e.lineno}: {e.message}",
            fixable=True,
        ))

    return failures


# ── Git Helpers ────────────────────────────────────────────────────────────

def _has_git(repo_root: Path) -> bool:
    """Check if a git repo exists."""
    return (repo_root / ".git").exists()


def _git_snapshot(repo_root: Path, paths: list[str], message: str) -> str | None:
    """Commit current state of files for rollback safety."""
    if not paths or not _has_git(repo_root):
        return None
    try:
        subprocess.run(["git", "add", "--force"] + paths, cwd=str(repo_root), capture_output=True, check=True)
        result = subprocess.run(
            ["git", "commit", "-m", message, "--allow-empty"],
            cwd=str(repo_root), capture_output=True, text=True,
        )
        if result.returncode == 0:
            sha = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=str(repo_root), capture_output=True, text=True,
            ).stdout.strip()
            return sha
    except Exception:
        logger.exception("Git snapshot failed")
    return None


def _git_commit(repo_root: Path, paths: list[str], message: str) -> str | None:
    """Add and commit files. Returns SHA."""
    if not _has_git(repo_root):
        logger.debug("No git repo — skipping commit")
        return None
    try:
        subprocess.run(["git", "add", "--force"] + paths, cwd=str(repo_root), capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", message],
            cwd=str(repo_root), capture_output=True, check=True,
        )
        sha = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_root), capture_output=True, text=True,
        ).stdout.strip()
        return sha
    except Exception:
        logger.exception("Git commit failed")
        return None


def _git_revert(repo_root: Path, commit_sha: str) -> bool:
    """Revert a specific commit."""
    if not _has_git(repo_root):
        logger.warning("No git repo — cannot revert")
        return False
    try:
        result = subprocess.run(
            ["git", "revert", "--no-edit", commit_sha],
            cwd=str(repo_root), capture_output=True, text=True,
        )
        if result.returncode == 0:
            logger.info("Git reverted: %s", commit_sha[:8])
            return True
        logger.error("Git revert failed: %s", result.stderr)
    except Exception:
        logger.exception("Git revert failed")
    return False


def _try_reload_tool(filename: str) -> str | None:
    """Try to import a tool module. Returns error string or None."""
    module_name = f"glitch_tool_{filename.replace('.py', '')}"
    tool_path = Path(__file__).parent.parent.parent / "tools" / filename

    try:
        if module_name in sys.modules:
            del sys.modules[module_name]

        spec = importlib.util.spec_from_file_location(module_name, tool_path)
        if spec is None or spec.loader is None:
            return f"Could not create module spec for {filename}"

        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return None

    except Exception as e:
        if module_name in sys.modules:
            del sys.modules[module_name]
        return str(e)
