from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def sh(*args: str, cwd: Path) -> str:
    return subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_WT_ROOT", str(tmp_path / "state"))
    monkeypatch.setenv("CLAUDE_WT_VOLATILE", str(tmp_path / "volatile"))
    for var in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{var}_NAME", "Test")
        monkeypatch.setenv(f"GIT_{var}_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.delenv("CLAUDE_WT_ALLOW_SWITCH", raising=False)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """main: a.txt, b.txt. Branch `other` changes a.txt. cwd is set to the repo."""
    root = tmp_path / "main"
    root.mkdir()
    sh("git", "init", "-q", "-b", "main", cwd=root)
    (root / "a.txt").write_text("a\n")
    (root / "b.txt").write_text("b\n")
    (root / ".gitignore").write_text("node_modules/\n.venv/\n")
    sh("git", "add", ".", cwd=root)
    sh("git", "commit", "-qm", "init", cwd=root)
    sh("git", "branch", "other", cwd=root)
    sh("git", "switch", "-q", "other", cwd=root)
    (root / "a.txt").write_text("other\n")
    sh("git", "commit", "-qam", "other", cwd=root)
    sh("git", "switch", "-q", "main", cwd=root)
    monkeypatch.chdir(root)
    return root
