from __future__ import annotations

import os
import subprocess

from glitch_core import gitops

GIT_ENV = {
    **os.environ,
    "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
    "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
}


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, env=GIT_ENV, check=True, capture_output=True)


def test_commit_and_reset(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    assert not gitops.has_git(repo)
    _git(repo, "init", "-q")
    (repo / "a.txt").write_text("one")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    assert gitops.has_git(repo)

    green = gitops.current_sha(repo)
    assert green

    # Make + commit a change.
    (repo / "a.txt").write_text("two")
    assert gitops.is_dirty(repo)
    new = gitops.commit_all(repo, "change")
    assert new and new != green
    assert not gitops.is_dirty(repo)

    # Roll back to the original commit.
    assert gitops.reset_hard(repo, green)
    assert (repo / "a.txt").read_text() == "one"
    assert gitops.current_sha(repo) == green
