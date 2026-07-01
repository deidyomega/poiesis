"""Minimal git helpers (stdlib subprocess) for self-mod deploy + rollback.

Just what the supervisor and the request_deploy tool need: snapshot, commit,
hard-reset. No AST/import blocklist.
"""

from __future__ import annotations

import subprocess
from pathlib import Path


def _run(repo: str | Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(repo), capture_output=True, text=True
    )


def has_git(repo: str | Path) -> bool:
    return (Path(repo) / ".git").exists()


def current_sha(repo: str | Path) -> str | None:
    r = _run(repo, "rev-parse", "HEAD")
    return r.stdout.strip() if r.returncode == 0 else None


def is_dirty(repo: str | Path) -> bool:
    return bool(_run(repo, "status", "--porcelain").stdout.strip())


def commit_all(repo: str | Path, message: str) -> str | None:
    """Stage everything and commit. Returns the resulting HEAD sha.

    If there's nothing to commit, returns the current HEAD unchanged.
    """
    _run(repo, "add", "-A")
    _run(repo, "commit", "-m", message)  # no-op (nonzero) if nothing staged
    return current_sha(repo)


def reset_hard(repo: str | Path, ref: str) -> bool:
    """Hard-reset the working tree to a ref (the rollback primitive)."""
    return _run(repo, "reset", "--hard", ref).returncode == 0


def commit_file(repo: str | Path, relpath: str | Path, message: str) -> str | None:
    """Stage and commit a single path, leaving any other changes untouched.

    Used by the soul editor so a browser save is durable in git history without
    sweeping unrelated working-tree edits. No-op (returns current HEAD) if the
    file is unchanged.
    """
    _run(repo, "add", "--", str(relpath))
    _run(repo, "commit", "-m", message, "--", str(relpath))
    return current_sha(repo)
