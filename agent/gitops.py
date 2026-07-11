"""Git / GitHub operations for the loop: branch, commit, push, open PR.

Thin wrappers over `git` and `gh`. All functions take the repo working dir.
Nothing here is destructive to the base branch — work happens on a per-run
`<prefix>/<run_id>` branch and lands via PR.
"""

from __future__ import annotations

import subprocess


class GitError(Exception):
    pass


def _run(args: list[str], cwd: str, check: bool = True) -> str:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="ignore")
    if check and proc.returncode != 0:
        raise GitError(f"{' '.join(args)} failed:\n{proc.stdout}\n{proc.stderr}")
    return (proc.stdout or "").strip()


def current_branch(cwd: str) -> str:
    return _run(["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd)


def ensure_branch(cwd: str, name: str) -> None:
    _run(["git", "checkout", "-B", name], cwd)


def checkout_file(cwd: str, rel_path: str) -> None:
    """Revert a single file to HEAD — used to roll back a fix that didn't help."""
    _run(["git", "checkout", "--", rel_path], cwd, check=False)


def has_changes(cwd: str) -> bool:
    return bool(_run(["git", "status", "--porcelain"], cwd, check=False))


def stage_all(cwd: str) -> None:
    _run(["git", "add", "-A"], cwd)


def commit(cwd: str, message: str, author_name: str, author_email: str) -> str:
    env_args = [
        "-c", f"user.name={author_name}",
        "-c", f"user.email={author_email}",
    ]
    _run(["git", *env_args, "commit", "-m", message], cwd)
    return _run(["git", "rev-parse", "HEAD"], cwd)


def push(cwd: str, branch: str) -> None:
    _run(["git", "push", "-u", "origin", branch, "--force-with-lease"], cwd)


def open_pr(cwd: str, base: str, head: str, title: str, body: str) -> tuple[int | None, str | None]:
    """Open a PR via gh. Returns (number, url) or (None, None) if gh unavailable."""
    try:
        url = _run(
            ["gh", "pr", "create", "--base", base, "--head", head, "--title", title, "--body", body],
            cwd,
        )
    except GitError:
        return None, None
    num = None
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    if tail.isdigit():
        num = int(tail)
    return num, url
