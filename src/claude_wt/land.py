"""Bringing the agent's work back, driven by the human after the agent exits.

The branch already lives in the main repo's shared .git; "landing" means merging or squashing it into whatever
branch the main checkout has checked out, with the human choosing at each step.
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

from . import gitops
from .context import Context, remove
from .errors import WtError
from .state import Meta

Ask = Callable[[str], str]


class Outcome(StrEnum):
    RESUME = "resume"
    DONE = "done"


MENU = {
    "m": "merge into the main checkout's branch (fast-forward when possible)",
    "s": "squash into the main checkout as staged changes, for you to commit",
    "l": "log of the branch's commits",
    "d": "diff against where it started",
    "r": "resume the agent",
    "k": "keep it for later (default)",
    "x": "discard: remove the worktree and delete the branch",
}
NO_COMMITS = {"r", "k", "x"}


def _confirm(ask: Ask, question: str, default: bool) -> bool:
    answer = ask(f"{question} [{'Y/n' if default else 'y/N'}] ").strip().lower()
    return default if not answer else answer.startswith("y")


def _git_tty(cwd: Path, *args: str) -> int:
    """Run git attached to the terminal (pager, conflict output, hooks)."""
    return subprocess.run(["git", *args], cwd=cwd, check=False).returncode


def clear_carried_changes(main: Path, meta: Meta, ask: Ask) -> None:
    """Main still holds the changes that were carried into the WIP commit. If they're unchanged since, clearing them
    from main loses nothing: the merge brings the same content back. If they've diverged, refuse."""
    head = gitops.git("rev-parse", "HEAD", cwd=main)
    if meta.wip_sha is None or head != meta.base_sha:
        raise WtError(f"{main} has uncommitted changes; commit or stash them first")
    if gitops.snapshot_tree(main, head) != gitops.git("rev-parse", f"{meta.wip_sha}^{{tree}}", cwd=main):
        raise WtError(
            f"{main} has uncommitted changes that differ from the carried WIP commit {meta.wip_sha[:12]}; "
            "commit or stash them first"
        )
    if not _confirm(
        ask,
        "claude-wt: main's uncommitted changes are identical to the carried WIP commit. Clear them from main "
        "so the merge can bring them back?",
        default=True,
    ):
        raise WtError("left main's uncommitted changes alone; nothing merged")
    untracked = [e[3:] for e in gitops.dirty_entries(main) if e.startswith("?? ")]
    gitops.git("reset", "--quiet", "--hard", cwd=main)
    if untracked:
        gitops.git("clean", "--quiet", "--force", "--", *untracked, cwd=main)


def _target_branch(meta: Meta, ask: Ask) -> tuple[Path, str]:
    main = Path(meta.main_checkout)
    branch = gitops.git("symbolic-ref", "--quiet", "--short", "HEAD", cwd=main, check=False)
    if not branch:
        raise WtError(f"{main} has a detached HEAD; check out the branch to merge into first")
    if gitops.dirty_entries(main):
        clear_carried_changes(main, meta, ask)
    return main, branch


def merge(meta: Meta, ask: Ask) -> bool:
    main, target = _target_branch(meta, ask)
    note = (
        f" This includes the carried WIP commit {meta.wip_sha[:12]}, so your in-progress changes become a commit"
        " on the branch; use squash to get them back uncommitted."
        if meta.wip_sha
        else ""
    )
    if not _confirm(ask, f"claude-wt: merge {meta.branch} into {target} in {main}?{note}", default=True):
        return False
    if gitops.is_merged_into(main, "HEAD", meta.branch):
        code = _git_tty(main, "merge", "--ff-only", meta.branch)
    else:
        code = _git_tty(main, "merge", "--no-edit", meta.branch)
    if code != 0:
        print(f"claude-wt: merge stopped (exit {code}). Resolve it in {main}; the worktree is kept.")
        return False
    print(f"claude-wt: merged {meta.branch} into {target}")
    return True


def squash(meta: Meta, ask: Ask) -> bool:
    main, target = _target_branch(meta, ask)
    if not _confirm(ask, f"claude-wt: squash {meta.branch} into {target}'s staging area in {main}?", default=True):
        return False
    code = _git_tty(main, "merge", "--squash", meta.branch)
    if code != 0:
        print(f"claude-wt: squash stopped (exit {code}). Resolve it in {main}; the worktree is kept.")
        return False
    print(f"claude-wt: staged in {main}. Review with `git diff --cached` and commit when ready.")
    return True


def _offer_cleanup(ctx: Context, meta: Meta, ask: Ask, squashed: bool) -> None:
    # A squash leaves the branch looking unmerged, so cleanup has to force-delete it.
    if _confirm(ask, "claude-wt: remove the worktree and branch now?", default=not squashed):
        remove(ctx, meta, force=squashed)


def _prompt(ahead: int, dirty: int, meta: Meta) -> str:
    lines = [f"\nclaude-wt: {meta.branch}: {ahead} commit(s) ahead of {meta.base_ref}"]
    if dirty:
        lines.append(f"  warning: {dirty} uncommitted path(s) in the worktree won't be included; resume to commit them")
    keys = [k for k in MENU if ahead or k in NO_COMMITS]
    lines += [f"  [{k}] {MENU[k]}" for k in keys]
    return "\n".join(lines) + "\nchoice: "


def land(ctx: Context, meta: Meta, ask: Ask = input) -> Outcome:
    wt = ctx.state.worktree(meta.slug)
    while True:
        ahead = gitops.commits_ahead(wt, meta.base_sha)
        choice = (ask(_prompt(ahead, len(gitops.dirty_entries(wt)), meta)).strip().lower() or "k")[:1]
        if choice not in MENU or (not ahead and choice not in NO_COMMITS):
            print(f"claude-wt: unknown choice {choice!r}")
            continue
        try:
            outcome = _dispatch(ctx, meta, ask, choice)
        except WtError as e:
            print(f"claude-wt: {e}")
            continue
        if outcome is not None:
            return outcome


def _dispatch(ctx: Context, meta: Meta, ask: Ask, choice: str) -> Outcome | None:
    wt = ctx.state.worktree(meta.slug)
    if choice == "l":
        _git_tty(wt, "log", "--stat", f"{meta.base_sha}..HEAD")
    elif choice == "d":
        _git_tty(wt, "diff", f"{meta.base_sha}..HEAD")
    elif choice == "r":
        return Outcome.RESUME
    elif choice == "k":
        print(f"claude-wt: kept. Later: `claude-wt land {meta.slug}` or `claude-wt resume {meta.slug}`")
        return Outcome.DONE
    elif choice == "x":
        if _confirm(ask, f"claude-wt: discard {meta.branch} and all its work?", default=False):
            remove(ctx, meta, force=True)
            return Outcome.DONE
    elif choice in {"m", "s"}:
        landed = merge(meta, ask) if choice == "m" else squash(meta, ask)
        if landed:
            _offer_cleanup(ctx, meta, ask, squashed=choice == "s")
            return Outcome.DONE
    return None
