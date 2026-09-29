from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .deps import Step, Strategy


@dataclass(frozen=True)
class PromptContext:
    worktree: Path
    branch: str
    base_ref: str
    base_sha: str
    main_checkout: Path
    carried_dirty: bool
    sandboxed: bool
    steps: list[Step]


def _deps_lines(steps: list[Step]) -> list[str]:
    lines: list[str] = []
    for s in steps:
        what = {
            Strategy.OVERLAY: "copy-on-write overlay of the main checkout's copy; installs are yours alone and "
            "are discarded on reboot",
            Strategy.REFLINK: "copy-on-write copy of the main checkout's",
            Strategy.INSTALL: f"freshly installed (`{' '.join(s.command or [])}`)",
            Strategy.SKIP: f"not set up: {s.note}",
        }[s.strategy]
        lines.append(f"  - {s.dep.rel}: {what}")
    return lines


def render(ctx: PromptContext) -> str:
    base = ctx.base_ref if ctx.base_ref == ctx.base_sha else f"{ctx.base_ref} ({ctx.base_sha[:12]})"
    lines = [
        "# You are running in a git worktree (claude-wt)",
        "",
        f"- Worktree (your working directory): {ctx.worktree}",
        f"- Branch: {ctx.branch}, created from {base}",
        f"- Main checkout, for reference only: {ctx.main_checkout}",
        "",
        "Rules:",
        f"- Do all work and commits here, on {ctx.branch}. Don't switch branches, detach HEAD, or create/remove "
        "worktrees; the `git` on your PATH refuses those commands.",
        "- Don't edit files in the main checkout. "
        + (
            "It is mounted read-only for this session, and so is the repo's .git apart from your own branch: "
            "other branches, git config and `git stash` (shared with the main checkout) are off limits. "
            "Commit work in progress instead of stashing it. `git fetch` works but skips tags, and `--prune` "
            "can't remove packed remote-tracking refs."
            if ctx.sandboxed
            else "Nothing enforces this, so take care with absolute paths."
        ),
        "- Commit as you go. Uncommitted changes are only in this worktree and are easy to lose.",
    ]
    if ctx.carried_dirty:
        lines.append(
            "- The first commit on this branch is a WIP snapshot of the user's uncommitted changes, made without "
            "running hooks. Treat it as the user's in-progress work, not yours."
        )
    if ctx.steps:
        lines += ["", "Dependency directories:", *_deps_lines(ctx.steps)]
    return "\n".join(lines) + "\n"
