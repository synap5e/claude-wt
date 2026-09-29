from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from . import gitops
from .errors import GitError, WtError
from .state import Meta, RepoState

BRANCH_PREFIX = "wt/"


@dataclass(frozen=True)
class Context:
    repo: gitops.Repo
    state: RepoState

    @classmethod
    def here(cls) -> Context:
        repo = gitops.discover(Path.cwd())
        return cls(repo, RepoState.for_repo(repo.common_dir, repo.name))


def remove(ctx: Context, meta: Meta, force: bool) -> None:
    """Remove the worktree and its state; delete the branch if it's empty, merged, or `force`."""
    wt, top = ctx.state.worktree(meta.slug), ctx.repo.toplevel
    if wt.exists():
        entries = gitops.dirty_entries(wt)
        if entries and not force:
            raise WtError(f"{wt} has {len(entries)} uncommitted path(s); commit them or pass --force")
        gitops.remove_worktree(top, wt, force=True)
    ctx.state.forget(meta.slug)
    print(f"claude-wt: removed {wt}")
    if gitops.branch_exists(top, meta.branch):
        _drop_branch(top, meta, force)


def _drop_branch(top: Path, meta: Meta, force: bool) -> None:
    empty = gitops.commits_ahead(top, meta.base_sha, meta.branch) == 0
    try:
        gitops.delete_branch(top, meta.branch, force=force or empty)
        print(f"claude-wt: deleted branch {meta.branch}")
    except GitError:
        print(
            f"claude-wt: kept branch {meta.branch}: it has commits git doesn't see as merged "
            f"(squash merges look unmerged). Delete with `git branch -D {meta.branch}` once you're sure."
        )
