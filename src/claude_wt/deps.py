"""Give the worktree usable dependency dirs without sharing the main checkout's.

Symlinking or copying them is wrong for Python: a uv venv has the main checkout's absolute path baked
into its editable-install `.pth`, so imports would silently load main's code instead of the worktree's.
Venvs are always rebuilt with `uv sync` (hardlinked from uv's cache, so it's fast and costs almost no disk).

node_modules uses relative links, so it can be shared copy-on-write:
  overlay  bwrap overlayfs: main's dir read-only below, writes to a per-boot upper dir. Instant, any fs.
  reflink  `cp --reflink=always`: a real COW copy. Needs btrfs/xfs, same fs as main.
  install  fresh install from the lockfile.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from .gitops import tracked_files_named


class Strategy(StrEnum):
    OVERLAY = "overlay"
    REFLINK = "reflink"
    INSTALL = "install"
    SKIP = "skip"


class Mode(StrEnum):
    AUTO = "auto"
    INSTALL = "install"
    NONE = "none"


VENV_NAMES = (".venv", "venv")

NODE_INSTALLERS: tuple[tuple[str, list[str]], ...] = (
    ("pnpm-lock.yaml", ["pnpm", "install", "--frozen-lockfile"]),
    ("bun.lock", ["bun", "install", "--frozen-lockfile"]),
    ("bun.lockb", ["bun", "install", "--frozen-lockfile"]),
    ("yarn.lock", ["yarn", "install", "--frozen-lockfile"]),
    ("package-lock.json", ["npm", "ci"]),
)


@dataclass(frozen=True)
class DepDir:
    rel: Path  # relative to the checkout root
    kind: str  # "venv" | "node_modules"


@dataclass(frozen=True)
class Step:
    dep: DepDir
    strategy: Strategy
    command: list[str] | None = None  # for INSTALL, run in the worktree at dep.rel.parent
    note: str = ""


def detect(main: Path) -> list[DepDir]:
    found: list[DepDir] = []
    for d in tracked_files_named(main, "pyproject.toml"):
        found += [DepDir(d / n, "venv") for n in VENV_NAMES if (main / d / n / "pyvenv.cfg").exists()]
    for d in tracked_files_named(main, "package.json"):
        if (main / d / "node_modules").is_dir():
            found.append(DepDir(d / "node_modules", "node_modules"))
    return found


def _node_installer(project_dir: Path) -> list[str] | None:
    for lockfile, cmd in NODE_INSTALLERS:
        if (project_dir / lockfile).exists():
            return cmd
    return None


def _plan_venv(dep: DepDir, main: Path) -> Step:
    project = main / dep.rel.parent
    if not (project / "uv.lock").exists():
        return Step(dep, Strategy.SKIP, note="no uv.lock; create this environment yourself if you need it")
    if dep.rel.name != ".venv":
        return Step(dep, Strategy.SKIP, note="non-default venv name; run `uv sync` with UV_PROJECT_ENVIRONMENT set")
    return Step(dep, Strategy.INSTALL, ["uv", "sync", "--frozen"])


def _plan_node(dep: DepDir, main: Path, mode: Mode, sandboxed: bool, covered: set[Path]) -> Step:
    if mode is Mode.AUTO and sandboxed:
        return Step(dep, Strategy.OVERLAY)
    if mode is Mode.AUTO:
        return Step(dep, Strategy.REFLINK, note="falls back to install if the filesystem can't reflink")
    if any(dep.rel.is_relative_to(c) for c in covered):
        return Step(dep, Strategy.SKIP, note="covered by the workspace install above it")
    cmd = _node_installer(main / dep.rel.parent)
    if cmd is None:
        return Step(dep, Strategy.SKIP, note="no lockfile found; install dependencies yourself if needed")
    return Step(dep, Strategy.INSTALL, cmd)


def plan(deps: list[DepDir], main: Path, mode: Mode, sandboxed: bool) -> list[Step]:
    """Pure apart from lockfile existence checks. Shallow node dirs first so workspace roots cover nested ones."""
    if mode is Mode.NONE:
        return [Step(d, Strategy.SKIP, note="--deps none") for d in deps]
    steps: list[Step] = []
    covered: set[Path] = set()
    for dep in sorted(deps, key=lambda d: len(d.rel.parts)):
        if dep.kind == "venv":
            steps.append(_plan_venv(dep, main))
            continue
        step = _plan_node(dep, main, mode, sandboxed, covered)
        if step.strategy is Strategy.INSTALL:
            covered.add(dep.rel.parent)
        steps.append(step)
    return steps


def fallback_to_install(step: Step, main: Path, covered: set[Path]) -> Step:
    return _plan_node(step.dep, main, Mode.INSTALL, sandboxed=False, covered=covered)


def reflink_copy(src: Path, dst: Path) -> bool:
    if dst.exists():
        return True
    proc = subprocess.run(["cp", "-a", "--reflink=always", str(src), str(dst)], capture_output=True, check=False)
    if proc.returncode != 0:
        shutil.rmtree(dst, ignore_errors=True)
        return False
    return True


def run_install(cmd: list[str], cwd: Path) -> bool:
    if shutil.which(cmd[0]) is None:
        print(f"claude-wt: {cmd[0]} not found; skipping `{' '.join(cmd)}` in {cwd}")
        return False
    print(f"claude-wt: $ {' '.join(cmd)}  (in {cwd})", flush=True)
    return subprocess.run(cmd, cwd=cwd, check=False).returncode == 0


def apply(steps: list[Step], main: Path, worktree: Path) -> list[Step]:
    """Run reflink/install steps. Returns the steps as actually carried out (reflink may become install)."""
    done: list[Step] = []
    covered: set[Path] = set()
    for step in steps:
        actual = step
        if step.strategy is Strategy.REFLINK and not reflink_copy(main / step.dep.rel, worktree / step.dep.rel):
            actual = fallback_to_install(step, main, covered)
        if actual.strategy is Strategy.INSTALL and actual.command:
            covered.add(actual.dep.rel.parent)
            if not (worktree / actual.dep.rel).exists() and not run_install(
                actual.command, worktree / actual.dep.rel.parent
            ):
                actual = Step(actual.dep, Strategy.SKIP, note=f"`{' '.join(actual.command)}` failed or unavailable")
        done.append(actual)
    return done
