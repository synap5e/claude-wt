"""Thin git wrappers. Every call goes through `git()` so failures surface as GitError."""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from .errors import GitError


def git(*args: str, cwd: Path, env: dict[str, str] | None = None, check: bool = True) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env={**os.environ, **(env or {})},
        capture_output=True,
        text=True,
    )
    if check and proc.returncode != 0:
        raise GitError(list(args), proc.returncode, proc.stderr)
    return proc.stdout.strip()


@dataclass(frozen=True)
class Repo:
    toplevel: Path  # checkout the user launched from (may itself be a linked worktree)
    common_dir: Path  # shared .git dir: objects, refs, worktree metadata
    head: str  # commit sha
    branch: str | None  # None when detached

    @property
    def name(self) -> str:
        return self.common_dir.parent.name if self.common_dir.name == ".git" else self.toplevel.name


def discover(cwd: Path) -> Repo:
    toplevel = Path(git("rev-parse", "--show-toplevel", cwd=cwd))
    common = Path(git("rev-parse", "--path-format=absolute", "--git-common-dir", cwd=cwd))
    head = git("rev-parse", "HEAD", cwd=cwd)
    branch = git("symbolic-ref", "--quiet", "--short", "HEAD", cwd=cwd, check=False) or None
    return Repo(toplevel=toplevel, common_dir=common, head=head, branch=branch)


def git_dir(cwd: Path) -> Path:
    return Path(git("rev-parse", "--absolute-git-dir", cwd=cwd))


def ref_format(cwd: Path) -> str:
    """'files' or 'reftable'. Older gits don't know the flag and only support 'files'."""
    return git("rev-parse", "--show-ref-format", cwd=cwd, check=False) or "files"


def remotes(cwd: Path) -> list[str]:
    return git("remote", cwd=cwd).split()


def dirty_entries(cwd: Path) -> list[str]:
    """Porcelain lines for modified, staged and untracked (non-ignored) files."""
    out = git("status", "--porcelain=v1", "--untracked-files=all", cwd=cwd)
    return [line for line in out.splitlines() if line]


def snapshot_tree(cwd: Path, parent: str) -> str:
    """Tree of the working tree as it stands (including untracked, non-ignored files), built in a throwaway index
    so the user's real index and staged/unstaged split are untouched."""
    with tempfile.TemporaryDirectory(prefix="claude-wt-index-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        git("read-tree", parent, cwd=cwd, env=env)
        git("add", "--all", "--", ".", cwd=cwd, env=env)
        return git("write-tree", cwd=cwd, env=env)


def snapshot_commit(cwd: Path, parent: str, message: str) -> str:
    """Commit the working tree on top of `parent` without touching the real index or running hooks."""
    return git("commit-tree", snapshot_tree(cwd, parent), "-p", parent, "-m", message, cwd=cwd)


def resolve_commit(cwd: Path, ref: str) -> str:
    return git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", cwd=cwd)


def branch_exists(cwd: Path, branch: str) -> bool:
    return bool(git("rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", cwd=cwd, check=False))


def add_worktree(cwd: Path, path: Path, branch: str, start: str) -> None:
    git("worktree", "add", "--quiet", "-b", branch, str(path), start, cwd=cwd)


def remove_worktree(cwd: Path, path: Path, force: bool) -> None:
    git("worktree", "remove", *(["--force"] if force else []), str(path), cwd=cwd)


def delete_branch(cwd: Path, branch: str, force: bool) -> None:
    git("branch", "-D" if force else "-d", branch, cwd=cwd)


def commits_ahead(cwd: Path, base: str, head: str = "HEAD") -> int:
    return int(git("rev-list", "--count", f"{base}..{head}", cwd=cwd))


def is_merged_into(cwd: Path, commit: str, into: str) -> bool:
    return subprocess.run(["git", "merge-base", "--is-ancestor", commit, into], cwd=cwd, check=False).returncode == 0


def tracked_files_named(cwd: Path, name: str) -> list[Path]:
    """Directories (relative to cwd) that contain a tracked file called `name`."""
    out = git("ls-files", "-z", "--", f"{name}", f"**/{name}", cwd=cwd)
    return sorted({Path(p).parent for p in out.split("\0") if p})
