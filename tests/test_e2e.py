"""Drive the real CLI against throwaway repos, with `sh -c` standing in for the agent."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from claude_wt import cli, sandbox
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
    assert out.read_text().split() == [str(wt), "wt/t1/work", "wt/t1/work"]
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
    assert text.strip().endswith("wt/t6/work")
    assert sh("git", "log", "-1", "--format=%s", "wt/t6/work", cwd=repo) == "c"


@pytest.mark.usefixtures("repo")
def test_allow_env_lets_switch_through(tmp_path: Path) -> None:
    out = tmp_path / "out"
    script = f"CLAUDE_WT_ALLOW=1 git switch -q -c elsewhere; git branch --show-current > {out}"
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
    assert sh("git", "branch", "--list", "wt/t9/work", cwd=repo) != ""


def test_rm_deletes_empty_branch(repo: Path) -> None:
    assert run("new", "t10", *agent("true", "--no-sandbox")) == 0
    assert run("rm", "t10") == 0
    assert sh("git", "branch", "--list", "wt/t10/work", cwd=repo) == ""


@pytest.mark.usefixtures("repo")
def test_duplicate_slug_is_rejected() -> None:
    assert run("new", "t11", *agent("true", "--no-sandbox")) == 0
    assert run("new", "t11", *agent("true", "--no-sandbox")) == 2


@pytest.mark.usefixtures("repo")
def test_print_prompt_mentions_branch_and_rules(capsys: pytest.CaptureFixture[str]) -> None:
    assert run("new", "t12", "--print-prompt", "--no-sandbox") == 0
    text = capsys.readouterr().out
    assert "wt/t12/work" in text
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


UNAVAILABLE = sandbox.Capabilities(False, False, "bwrap not installed")


@pytest.mark.parametrize("how", ["flag", "env"])
def test_require_sandbox_refuses_before_creating_anything(
    repo: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], how: str
) -> None:
    monkeypatch.setattr(sandbox, "probe", lambda: UNAVAILABLE)
    flags = ["-S"] if how == "flag" else []
    if how == "env":
        monkeypatch.setenv("CLAUDE_WT_REQUIRE_SANDBOX", "1")
    assert run("new", "t13", *flags, *agent("true")) == 2
    assert "sandbox required but unavailable (bwrap not installed)" in capsys.readouterr().err
    assert not worktree(repo, "t13").exists()
    assert sh("git", "branch", "--list", "wt/t13/work", cwd=repo) == ""


def test_no_sandbox_overrides_env_requirement(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sandbox, "probe", lambda: UNAVAILABLE)
    monkeypatch.setenv("CLAUDE_WT_REQUIRE_SANDBOX", "1")
    assert run("new", "t14", *agent("true", "--no-sandbox")) == 0
    assert worktree(repo, "t14").exists()


@pytest.mark.usefixtures("repo")
def test_require_and_no_sandbox_conflict() -> None:
    assert run("new", "t15", "-S", "--no-sandbox") == 2


@needs_bwrap
def test_sandbox_pins_refs_to_own_branch(repo: Path, tmp_path: Path) -> None:
    """The escape routes an agent actually reached for in a blind test: plumbing onto another branch, etc."""
    out = tmp_path / "out"
    script = (
        f"git update-ref refs/heads/main HEAD 2>/dev/null; echo update_ref=$? >> {out}; "
        f"git branch -f other HEAD 2>/dev/null; echo branch_f=$? >> {out}; "
        f"git branch scratch 2>/dev/null; echo branch_new=$? >> {out}; "
        f"git -C {repo} symbolic-ref HEAD refs/heads/other 2>/dev/null; echo symref=$? >> {out}; "
        f"git config core.hooksPath /x 2>/dev/null; echo config=$? >> {out}; "
        f"echo evil > {repo}/.git/hooks/post-checkout 2>/dev/null; echo hook=$? >> {out}; "
        f"git pack-refs --all 2>/dev/null; echo pack_refs=$? >> {out}; "
        f"echo x > c.txt && git add c.txt && git commit -qm c 2>> {out}; echo commit=$? >> {out}; "
        f"git rebase -q other 2>> {out}; echo rebase=$? >> {out}; git status --short >> {out}"
    )
    sh("git", "pack-refs", "--all", cwd=repo)  # worst case: protected branches live only in packed-refs
    main_before = sh("git", "rev-parse", "main", cwd=repo)
    other_before = sh("git", "rev-parse", "other", cwd=repo)
    assert run("new", "t16", "-S", *agent(script)) == 0
    text = out.read_text()
    for step in ("update_ref", "branch_f", "branch_new", "symref", "config", "hook", "pack_refs"):
        assert f"{step}=0" not in text, step
    assert "commit=0" in text
    assert "rebase=0" in text
    assert "error" not in text.lower()  # routine lock files land in the .git overlay, so no stray errors
    assert text.rstrip().endswith("rebase=0")  # and the rebase left no half-finished state
    assert not (repo / ".git" / "hooks" / "post-checkout").exists()
    assert sh("git", "rev-parse", "main", cwd=repo) == main_before
    assert sh("git", "rev-parse", "other", cwd=repo) == other_before
    assert sh("git", "symbolic-ref", "HEAD", cwd=repo) == "refs/heads/main"
    assert sh("git", "branch", "--list", "scratch", cwd=repo) == ""
    assert sh("git", "log", "-1", "--format=%s", "wt/t16/work", cwd=repo) == "c"
    assert sh("git", "merge-base", "--is-ancestor", "other", "wt/t16/work", cwd=repo) == ""


@needs_bwrap
def test_sandboxed_fetch_updates_remote_refs_despite_new_tags(repo: Path, tmp_path: Path) -> None:
    origin = tmp_path / "origin.git"
    sh("git", "clone", "-q", "--bare", str(repo), str(origin), cwd=tmp_path)
    sh("git", "remote", "add", "origin", str(origin), cwd=repo)
    sh("git", "fetch", "-q", "origin", cwd=repo)
    sh("git", "pack-refs", "--all", cwd=repo)
    upstream = tmp_path / "upstream"
    sh("git", "clone", "-q", str(origin), str(upstream), cwd=tmp_path)
    (upstream / "u.txt").write_text("u\n")
    sh("git", "add", "u.txt", cwd=upstream)
    sh("git", "commit", "-qm", "upstream", cwd=upstream)
    sh("git", "tag", "v1", cwd=upstream)
    sh("git", "push", "-q", "origin", "main", "v1", cwd=upstream)

    out = tmp_path / "out"
    assert run("new", "t17", "-S", *agent(f"git fetch -q origin 2>> {out}; echo fetch=$? >> {out}")) == 0
    assert out.read_text() == "fetch=0\n"
    assert sh("git", "log", "-1", "--format=%s", "origin/main", cwd=repo) == "upstream"
    assert sh("git", "tag", cwd=repo) == ""  # tags are left for the user's own fetch


@pytest.mark.parametrize("sandbox", [["--no-sandbox"], pytest.param(["-S"], marks=needs_bwrap)])
def test_push_only_own_branch(repo: Path, tmp_path: Path, sandbox: list[str]) -> None:
    origin = tmp_path / "origin.git"
    sh("git", "clone", "-q", "--bare", str(repo), str(origin), cwd=tmp_path)
    sh("git", "remote", "add", "origin", str(origin), cwd=repo)
    sh("git", "fetch", "-q", "origin", cwd=repo)
    out = tmp_path / "out"
    script = (
        f"echo x > c.txt && git add c.txt && git commit -qm c; "
        f"git push -q origin HEAD 2>/dev/null; echo own=$? >> {out}; "
        f"git push -q origin HEAD:pr-branch 2>/dev/null; echo new=$? >> {out}; "
        f"git push -q -f origin HEAD:main 2>> {out}; echo main=$? >> {out}; "
        f"git push -q origin other:elsewhere 2>/dev/null; echo other=$? >> {out}; "
        f"git push -q origin :other 2>/dev/null; echo delete=$? >> {out}"
    )
    main_before = sh("git", "rev-parse", "main", cwd=origin)
    assert run("new", "t18", *sandbox, *agent(script)) == 0
    text = out.read_text()
    assert "own=0" in text
    assert "new=0" in text
    assert "claude-wt: blocked: pushing to origin/main" in text
    for step in ("main", "other", "delete"):
        assert f"{step}=1" in text, step
    assert sh("git", "rev-parse", "main", cwd=origin) == main_before
    assert sh("git", "branch", "--list", "other", cwd=origin) != ""
    assert sh("git", "branch", "--list", "elsewhere", cwd=origin) == ""
    assert sh("git", "log", "-1", "--format=%s", "pr-branch", cwd=origin) == "c"
