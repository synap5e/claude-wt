"""Where things live on disk.

$CLAUDE_WT_ROOT (default $XDG_STATE_HOME/claude-wt)
  <repo>-<hash>/
    <slug>/                 the worktree
    .meta/<slug>.json       base ref, main checkout, sandbox choice
    .bin/git                the guard shim (shared by every worktree of this repo)

$CLAUDE_WT_VOLATILE (default /tmp/claude-wt-<uid>)
  <boot_id>/<repo>-<hash>/<slug>/<n>/{upper,work}
                            overlay writes: survive relaunches, not reboots. Keyed by boot_id so a
                            /tmp that isn't cleaned on boot still gets swept on the next launch.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
from dataclasses import asdict, dataclass
from pathlib import Path


def state_root() -> Path:
    if env := os.environ.get("CLAUDE_WT_ROOT"):
        return Path(env)
    xdg = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(xdg) / "claude-wt"


def volatile_root() -> Path:
    if env := os.environ.get("CLAUDE_WT_VOLATILE"):
        return Path(env)
    return Path(os.environ.get("TMPDIR", "/tmp")) / f"claude-wt-{os.getuid()}"


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "no-boot-id"


def repo_key(common_dir: Path, name: str) -> str:
    digest = hashlib.sha256(str(common_dir.resolve()).encode()).hexdigest()[:8]
    return f"{name}-{digest}"


@dataclass(frozen=True)
class Meta:
    slug: str
    branch: str
    base_ref: str
    base_sha: str
    main_checkout: str
    carried_dirty: bool


@dataclass(frozen=True)
class RepoState:
    root: Path  # state_root()/<repo-key>
    volatile: Path  # volatile_root()/<boot_id>/<repo-key>

    @classmethod
    def for_repo(cls, common_dir: Path, name: str) -> RepoState:
        key = repo_key(common_dir, name)
        return cls(root=state_root() / key, volatile=volatile_root() / boot_id() / key)

    def worktree(self, slug: str) -> Path:
        return self.root / slug

    def meta_path(self, slug: str) -> Path:
        return self.root / ".meta" / f"{slug}.json"

    @property
    def bin_dir(self) -> Path:
        return self.root / ".bin"

    def overlay_dir(self, slug: str, index: int) -> Path:
        return self.volatile / slug / str(index)

    def write_meta(self, meta: Meta) -> None:
        path = self.meta_path(meta.slug)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(asdict(meta), indent=2) + "\n")

    def read_meta(self, slug: str) -> Meta | None:
        path = self.meta_path(slug)
        if not path.exists():
            return None
        return Meta(**json.loads(path.read_text()))

    def slugs(self) -> list[str]:
        meta_dir = self.root / ".meta"
        return sorted(p.stem for p in meta_dir.glob("*.json")) if meta_dir.exists() else []

    def forget(self, slug: str) -> None:
        self.meta_path(slug).unlink(missing_ok=True)
        _rmtree_writable(self.volatile / slug)


def sweep_stale_boots() -> list[Path]:
    """Delete overlay writes left over from previous boots. Returns what was removed."""
    root = volatile_root()
    if not root.exists():
        return []
    current = boot_id()
    stale = [p for p in root.iterdir() if p.is_dir() and p.name != current]
    for path in stale:
        _rmtree_writable(path)
    return stale


def _rmtree_writable(path: Path) -> None:
    # overlayfs work dirs can be left mode 000; make them removable first.
    for dirpath, dirnames, _ in os.walk(path):
        for d in dirnames:
            with contextlib.suppress(OSError):
                os.chmod(os.path.join(dirpath, d), 0o700)
    shutil.rmtree(path, ignore_errors=True)
