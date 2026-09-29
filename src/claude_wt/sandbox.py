"""bubblewrap wrapping: the host filesystem as-is, except the main checkout and the shared .git.

The shared .git is layered so the kernel enforces "only your own branch":
  1. a throwaway overlay over all of .git. Git must create and remove lock files at its top level even for routine
     operations (deleting a pseudo-ref locks packed-refs), so a plain read-only .git breaks rebase. Stray writes
     (lock files, a planted hook, a fake MERGE_HEAD for the main checkout) land in the overlay and vanish.
  2. read-only binds over what matters (refs, logs, HEAD, config, packed-refs, hooks, info, other worktrees), so
     forbidden writes fail loudly instead of silently landing in the overlay.
  3. real, writable binds for what committing to the agent's own branch needs: the object store, the worktree's own
     git dir, its branch's ref and reflog directories, and remote-tracking refs so fetch works.

It is a guardrail, not a security boundary: nothing else is unshared.
"""

from __future__ import annotations

import functools
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Capabilities:
    bwrap: bool
    overlay: bool
    detail: str = ""


@dataclass(frozen=True)
class Overlay:
    lower: Path  # main checkout's dir, read-only layer
    dest: Path  # mount point inside the worktree
    upper: Path  # where writes land
    work: Path  # overlayfs scratch, same fs as upper


@functools.cache
def probe() -> Capabilities:
    exe = shutil.which("bwrap")
    if exe is None:
        return Capabilities(False, False, "bwrap not installed")
    base = [exe, "--dev-bind", "/", "/"]
    proc = subprocess.run([*base, "true"], capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        return Capabilities(False, False, f"bwrap unusable: {proc.stderr.strip()}")
    with tempfile.TemporaryDirectory(prefix="claude-wt-probe-") as tmp:
        lower, dest = Path(tmp, "lower"), Path(tmp, "dest")
        lower.mkdir()
        dest.mkdir()
        proc = subprocess.run(
            [*base, "--overlay-src", str(lower), "--tmp-overlay", str(dest), "true"],
            capture_output=True,
            text=True,
            check=False,
        )
    if proc.returncode != 0:
        return Capabilities(True, False, f"overlay unsupported: {proc.stderr.strip()}")
    return Capabilities(True, True)


@dataclass(frozen=True)
class GitLayout:
    common_dir: Path  # the shared .git
    worktree_git_dir: Path  # <common>/worktrees/<name>: index, HEAD, rebase state
    branch: str  # wt/<slug>/work

    def protected(self) -> list[Path]:
        names = ("refs", "logs", "HEAD", "config", "config.worktree", "packed-refs", "hooks", "info", "worktrees")
        return [p for p in (self.common_dir / n for n in names) if p.exists()]

    def writable(self) -> list[Path]:
        ref_dir = Path(*self.branch.split("/")[:-1])
        c = self.common_dir
        return [
            c / "objects",
            self.worktree_git_dir,
            c / "refs" / "heads" / ref_dir,
            c / "logs" / "refs" / "heads" / ref_dir,
            c / "refs" / "remotes",
            c / "logs" / "refs" / "remotes",
        ]


def prepare_git(layout: GitLayout) -> None:
    """Bind mount points must exist. Empty ref directories are harmless to git."""
    for path in layout.writable():
        path.mkdir(parents=True, exist_ok=True)


def build_argv(
    main_checkout: Path, git: GitLayout, worktree: Path, overlays: list[Overlay], cmd: list[str]
) -> list[str]:
    """Mount order matters: later binds sit on top of earlier ones."""
    argv = ["bwrap", "--dev-bind", "/", "/", "--ro-bind", str(main_checkout), str(main_checkout)]
    argv += ["--overlay-src", str(git.common_dir), "--tmp-overlay", str(git.common_dir)]
    for path in git.protected():
        argv += ["--ro-bind", str(path), str(path)]
    for path in git.writable():
        argv += ["--bind", str(path), str(path)]
    argv += ["--bind", str(worktree), str(worktree)]  # in case the worktree root sits inside the main checkout
    for ov in overlays:
        argv += ["--overlay-src", str(ov.lower), "--overlay", str(ov.upper), str(ov.work), str(ov.dest)]
    argv += ["--die-with-parent", "--chdir", str(worktree), "--", *cmd]
    return argv


def prepare_overlay(ov: Overlay) -> None:
    ov.upper.mkdir(parents=True, exist_ok=True)
    ov.work.mkdir(parents=True, exist_ok=True)
    ov.dest.mkdir(parents=True, exist_ok=True)
