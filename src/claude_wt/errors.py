from __future__ import annotations


class WtError(Exception):
    """Base for all claude-wt errors; the CLI prints these without a traceback."""


class GitError(WtError):
    def __init__(self, args: list[str], returncode: int, stderr: str):
        super().__init__(f"git {' '.join(args)} failed ({returncode}): {stderr.strip()}")
        self.args_ = args
        self.returncode = returncode
        self.stderr = stderr


class DirtyTreeError(WtError):
    def __init__(self, repo: str, entries: list[str]):
        shown = "\n".join(f"  {e}" for e in entries[:20])
        more = f"\n  ... and {len(entries) - 20} more" if len(entries) > 20 else ""
        super().__init__(
            f"{repo} has uncommitted changes:\n{shown}{more}\n\n"
            "The worktree starts from a commit, so these would be left behind. Choose one:\n"
            "  --carry-dirty   snapshot them into a WIP commit on the new branch\n"
            "                  (no hooks run; your tree is untouched)\n"
            "  --allow-dirty   start from the commit anyway and leave them where they are"
        )
        self.entries = entries


class WorktreeNotFoundError(WtError):
    def __init__(self, slug: str):
        super().__init__(f"no claude-wt worktree named {slug!r} for this repo (see `claude-wt ls`)")
        self.slug = slug
