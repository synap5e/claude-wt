"""Drive the real CLI against throwaway repos, with `sh -c` standing in for the agent."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from claude_wt import cli
from claude_wt.errors import DirtyTreeError
from claude_wt.state import RepoState, boot_id, sweep_stale_boots, volatile_root

from .conftest import sh

needs_bwrap = pytest.mark.skipif(shutil.which("bwrap") is None, reason="bwrap not installed")


def run(*argv: str) -> int:
    with pytest.raises(SystemExit) as exc:
        cli.main(list(argv))
    return int(exc.value.code or 0)


def agent(script: str, *extra: str) -> list[str]:
    """Options that launch `sh -c <script>` as the agent."""
    return ["--cmd", "sh", *extra, "--", "-c", script]


def worktree(repo: Path, slug: str) -> Path:
    return RepoState.for_repo(repo / ".git", repo.name).worktree(slug)


def test_launches_in_a_new_worktree_with_context(repo: Path, tmp_path: Path) -> None:
    out = tmp_path / "out"
    script = f'pwd > {out}; git branch --show-current >> {out}; echo "$CLAUDE_WT_BRANCH" >> {out}'
    assert run("new", "t1", *agent(script, "--no-sandbox")) == 0
    wt = worktree(repo, "t1")
    assert out.read_text().split() == [str(wt), "wt/t1", "wt/t1"]
    assert sh("git", "branch", "--show-current", cwd=repo) == "main"


def test_refuses_dirty_tree_by_default(repo: Path) -> None:
    (repo / "a.txt").write_text("edited\n")
    with pytest.raises(DirtyTreeError):
        cli.cmd_new(cli.build_parser().parse_args(["new", "t2", "--no-sandbox"]), [])
    assert run("new", "t2", *agent("true", "--no-sandbox")) == 2


def test_allow_dirty_leaves_changes_behind(repo: Path) -> None:
    (repo / "a.txt").write_text("edited\n")
    assert run("new", "t3", "--allow-dirty", *agent("true", "--no-sandbox")) == 0
    assert (worktree(repo, "t3") / "a.txt").read_text() == "a\n"
    assert (repo / "a.txt").read_text() == "edited\n"


def test_carry_dirty_snapshots_without_touching_main(repo: Path) -> None:
    (repo / "a.txt").write_text("edited\n")
    (repo / "b.txt").unlink()
    (repo / "new.txt").write_text("untracked\n")
    sh("git", "add", "a.txt", cwd=repo)
    before = sh("git", "status", "--porcelain", cwd=repo)
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)

    assert run("new", "t4", "--carry-dirty", *agent("true", "--no-sandbox")) == 0

    wt = worktree(repo, "t4")
    assert (wt / "a.txt").read_text() == "edited\n"
    assert not (wt / "b.txt").exists()
    assert (wt / "new.txt").read_text() == "untracked\n"
    assert sh("git", "log", "-1", "--format=%s", cwd=wt).startswith("WIP: uncommitted changes")
    assert sh("git", "status", "--porcelain", cwd=wt) == ""
    assert sh("git", "status", "--porcelain", cwd=repo) == before


def test_carry_dirty_rejects_other_base(repo: Path) -> None:
    (repo / "a.txt").write_text("edited\n")
    assert run("new", "t5", "--carry-dirty", "--base", "other", *agent("true", "--no-sandbox")) == 2


@pytest.mark.parametrize("sandbox", [["--no-sandbox"], pytest.param([], marks=needs_bwrap)])
def test_shim_blocks_switch_but_allows_commits(repo: Path, tmp_path: Path, sandbox: list[str]) -> None:
    out = tmp_path / "out"
    script = (
        f"git switch other 2>> {out}; echo switch=$? >> {out}; "
        f"git checkout other 2>/dev/null; echo checkout=$? >> {out}; "
        f"echo x > c.txt && git add c.txt && git commit -qm c; echo commit=$? >> {out}; "
        f"git branch --show-current >> {out}"
    )
    assert run("new", "t6", *agent(script, *sandbox)) == 0
    text = out.read_text()
    assert "claude-wt: blocked" in text
    assert "switch=1" in text
    assert "checkout=1" in text
    assert "commit=0" in text
    assert text.strip().endswith("wt/t6")
    assert sh("git", "log", "-1", "--format=%s", "wt/t6", cwd=repo) == "c"


@pytest.mark.usefixtures("repo")
def test_allow_env_lets_switch_through(tmp_path: Path) -> None:
    out = tmp_path / "out"
    script = f"CLAUDE_WT_ALLOW_SWITCH=1 git switch -q -c elsewhere; git branch --show-current > {out}"
    assert run("new", "t7", *agent(script, "--no-sandbox")) == 0
    assert out.read_text().strip() == "elsewhere"


@needs_bwrap
def test_sandbox_makes_main_readonly_and_overlays_node_modules(repo: Path, tmp_path: Path) -> None:
    (repo / "package.json").write_text("{}")
    sh("git", "add", "package.json", cwd=repo)
    sh("git", "commit", "-qm", "pkg", cwd=repo)
    (repo / "node_modules").mkdir()
    (repo / "node_modules" / "dep.js").write_text("main\n")

    out = tmp_path / "out"
    script = (
        f"echo hacked > {repo}/a.txt 2>/dev/null; echo main_write=$? >> {out}; "
        f"cat node_modules/dep.js >> {out}; echo mine > node_modules/added.js; echo nm_write=$? >> {out}"
    )
    assert run("new", "t8", *agent(script)) == 0
    text = out.read_text()
    assert "main_write=1" in text or "main_write=2" in text
    assert "main\n" in text
    assert "nm_write=0" in text
    assert (repo / "a.txt").read_text() == "a\n"
    assert not (repo / "node_modules" / "added.js").exists()

    # Overlay writes persist across a resume (same boot).
    out.unlink()
    assert run("resume", "t8", *agent(f"cat node_modules/added.js > {out}")) == 0
    assert out.read_text() == "mine\n"

    # rm clears the overlay writes, including overlayfs's mode-000 work dir.
    volatile = RepoState.for_repo(repo / ".git", repo.name).volatile / "t8"
    assert volatile.exists()
    assert run("rm", "t8") == 0
    assert not volatile.exists()


def test_rm_refuses_dirty_then_removes_and_keeps_unmerged_branch(repo: Path) -> None:
    script = "echo x > c.txt && git add c.txt && git commit -qm c && echo dirty > d.txt"
    assert run("new", "t9", *agent(script, "--no-sandbox")) == 0
    assert run("rm", "t9") == 2
    os.remove(worktree(repo, "t9") / "d.txt")
    assert run("rm", "t9") == 0
    assert not worktree(repo, "t9").exists()
    assert sh("git", "branch", "--list", "wt/t9", cwd=repo) != ""


def test_rm_deletes_empty_branch(repo: Path) -> None:
    assert run("new", "t10", *agent("true", "--no-sandbox")) == 0
    assert run("rm", "t10") == 0
    assert sh("git", "branch", "--list", "wt/t10", cwd=repo) == ""


@pytest.mark.usefixtures("repo")
def test_duplicate_slug_is_rejected() -> None:
    assert run("new", "t11", *agent("true", "--no-sandbox")) == 0
    assert run("new", "t11", *agent("true", "--no-sandbox")) == 2


@pytest.mark.usefixtures("repo")
def test_print_prompt_mentions_branch_and_rules(capsys: pytest.CaptureFixture[str]) -> None:
    assert run("new", "t12", "--print-prompt", "--no-sandbox") == 0
    text = capsys.readouterr().out
    assert "wt/t12" in text
    assert "Don't switch branches" in text


def test_sweep_removes_only_other_boots() -> None:
    current = volatile_root() / boot_id() / "x"
    stale = volatile_root() / "old-boot" / "work"
    current.mkdir(parents=True)
    stale.mkdir(parents=True)
    stale.chmod(0)
    sweep_stale_boots()
    assert current.exists()
    assert not stale.parent.exists()
