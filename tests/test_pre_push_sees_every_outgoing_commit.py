"""#123: the pre-push torch gate looks at every commit a push sends, not only the tip of a new
branch. Reproduced in a throwaway repository with the real hook and a stand-in torch runner
that always fails (so "the gate ran" is exit 1); no real push, network or production access.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HOOK = REPO / "scripts" / "hooks" / "pre-push"
ZERO = "0" * 40
WATCHED = "tagger/heads/train_heads.py"


def git(cwd: Path, *args: str) -> str:
    env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.org",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.org"}
    return subprocess.run(["git", *args], cwd=cwd, env=env, check=True, capture_output=True,
                          text=True).stdout.strip()


def commit(cwd: Path, path: str, text: str) -> str:
    f = cwd / path
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(text)
    git(cwd, "add", path)
    git(cwd, "commit", "-q", "-m", f"change {path}")
    return git(cwd, "rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    if shutil.which("git") is None:
        pytest.skip("needs git")
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "-q", "--bare", str(remote))
    work = tmp_path / "work"
    git(tmp_path, "init", "-q", "-b", "main", str(work))
    (work / "scripts").mkdir()
    (work / "scripts" / "run_torch_tests.sh").write_text("#!/bin/sh\necho ran > \"$(dirname \"$0\")/../ran\"\nexit 1\n")
    commit(work, "README.md", "base\n")
    git(work, "add", "scripts/run_torch_tests.sh")
    git(work, "commit", "-q", "-m", "runner")
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "-q", "origin", "main")             # origin/main = the baseline
    return work


def hook(work: Path, local_sha: str, remote_sha: str) -> int:
    (work / "ran").unlink(missing_ok=True)
    line = f"refs/heads/x {local_sha} refs/heads/x {remote_sha}\n"
    return subprocess.run(["bash", str(HOOK), "origin", "unused"], cwd=work, input=line, text=True,
                          capture_output=True).returncode


def test_a_new_branch_with_a_training_change_below_a_docs_tip_runs_the_gate(repo):
    commit(repo, WATCHED, "loss = weighted\n")
    tip = commit(repo, "README.md", "docs only\n")
    assert hook(repo, tip, ZERO) == 1 and (repo / "ran").exists()


def test_an_existing_branch_with_the_same_history_runs_the_gate(repo):
    base = git(repo, "rev-parse", "origin/main")
    commit(repo, WATCHED, "loss = weighted\n")
    tip = commit(repo, "README.md", "docs only\n")
    assert hook(repo, tip, base) == 1


def test_a_new_branch_with_the_training_change_at_its_tip_runs_the_gate(repo):
    tip = commit(repo, WATCHED, "loss = weighted\n")
    assert hook(repo, tip, ZERO) == 1


def test_unrelated_changes_do_not_run_the_gate(repo):
    base = git(repo, "rev-parse", "origin/main")
    tip = commit(repo, "docs/notes.md", "notes\n")
    assert hook(repo, tip, base) == 0 and not (repo / "ran").exists()
    assert hook(repo, tip, ZERO) == 0                       # a new branch of unrelated work too


def test_history_already_on_the_remote_is_not_inspected_again(repo):
    """A training change already pushed (on origin) is not this push's change."""
    commit(repo, WATCHED, "loss = weighted\n")
    git(repo, "push", "-q", "origin", "main")
    tip = commit(repo, "README.md", "docs only\n")
    assert hook(repo, tip, ZERO) == 0


def test_a_training_change_made_inside_a_merge_runs_the_gate(repo):
    """The change exists only in the merge commit (a conflict resolution, an "evil merge"),
    then a docs commit goes on top: git log hides merge diffs unless asked."""
    git(repo, "checkout", "-q", "-b", "side")
    commit(repo, "docs/side.md", "side\n")
    git(repo, "checkout", "-q", "main")
    commit(repo, "docs/main.md", "main\n")
    git(repo, "merge", "-q", "--no-commit", "--no-ff", "side")
    (repo / "tagger" / "heads").mkdir(parents=True, exist_ok=True)
    (repo / WATCHED).write_text("loss = changed in the merge\n")
    git(repo, "add", WATCHED)
    git(repo, "commit", "-q", "-m", "merge side")
    tip = commit(repo, "README.md", "docs only\n")
    assert hook(repo, tip, ZERO) == 1 and (repo / "ran").exists()
