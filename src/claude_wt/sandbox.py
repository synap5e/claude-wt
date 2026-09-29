"""bubblewrap wrapping: the host filesystem as-is, except the main checkout is read-only.

The agent can still commit (the shared .git dir stays writable) but can't edit the main checkout's files,
so "stay in your worktree" is enforced, not just requested. It is a guardrail, not a security boundary:
nothing else is unshared.
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


def build_argv(
    main_checkout: Path, common_dir: Path, worktree: Path, overlays: list[Overlay], cmd: list[str]
) -> list[str]:
    """Mount order matters: later binds sit on top of earlier ones."""
    argv = ["bwrap", "--dev-bind", "/", "/", "--ro-bind", str(main_checkout), str(main_checkout)]
    argv += ["--bind", str(common_dir), str(common_dir)]
    argv += ["--bind", str(worktree), str(worktree)]  # in case the worktree root sits inside the main checkout
    for ov in overlays:
        argv += ["--overlay-src", str(ov.lower), "--overlay", str(ov.upper), str(ov.work), str(ov.dest)]
    argv += ["--die-with-parent", "--chdir", str(worktree), "--", *cmd]
    return argv


def prepare_overlay(ov: Overlay) -> None:
    ov.upper.mkdir(parents=True, exist_ok=True)
    ov.work.mkdir(parents=True, exist_ok=True)
    ov.dest.mkdir(parents=True, exist_ok=True)
