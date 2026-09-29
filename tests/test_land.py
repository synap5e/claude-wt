"""The human-driven land menu, with scripted answers standing in for the human."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from claude_wt import cli
from claude_wt.context import Context
from claude_wt.land import Ask, Outcome, land
from claude_wt.state import Meta

from .conftest import sh


def scripted(*answers: str) -> Iterator[str]:
    yield from answers
    raise AssertionError("menu asked more questions than scripted")


def asker(*answers: str) -> Ask:
    it = scripted(*answers)
    return lambda _prompt: next(it)


def start(slug: str, script: str, *flags: str) -> tuple[Context, Meta]:
    with pytest.raises(SystemExit) as exc:
        cli.main(["new", slug, "--no-sandbox", "--no-land", *flags, "--cmd", "sh", "--", "-c", script])
    assert exc.value.code == 0
    ctx = Context.here()
    meta = ctx.state.read_meta(slug)
    assert meta is not None
    return ctx, meta


COMMIT_C = "echo c > c.txt && git add c.txt && git commit -qm 'add c'"


def test_merge_fast_forwards_and_cleans_up(repo: Path) -> None:
    ctx, meta = start("m1", COMMIT_C)
    assert land(ctx, meta, asker("m", "y", "y")) is Outcome.DONE
    assert sh("git", "log", "-1", "--format=%s", cwd=repo) == "add c"
    assert (repo / "c.txt").exists()
    assert not ctx.state.worktree("m1").exists()
    assert sh("git", "branch", "--list", "wt/m1", cwd=repo) == ""


def test_squash_brings_carried_changes_back_uncommitted(repo: Path) -> None:
    (repo / "a.txt").write_text("in progress\n")
    (repo / "new.txt").write_text("untracked\n")
    ctx, meta = start("s1", COMMIT_C, "--carry-dirty")
    # clear main's identical changes? yes; squash? yes; clean up? yes
    assert land(ctx, meta, asker("s", "y", "y", "y")) is Outcome.DONE
    assert sh("git", "log", "-1", "--format=%s", cwd=repo) == "init"  # squash stages, doesn't commit
    staged = sh("git", "diff", "--cached", "--name-only", cwd=repo).split()
    assert sorted(staged) == ["a.txt", "c.txt", "new.txt"]
    assert (repo / "a.txt").read_text() == "in progress\n"
    assert sh("git", "branch", "--list", "wt/s1", cwd=repo) == ""


def test_refuses_when_main_diverged_from_carried_snapshot(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    (repo / "a.txt").write_text("in progress\n")
    ctx, meta = start("s2", COMMIT_C, "--carry-dirty")
    (repo / "a.txt").write_text("edited again since\n")
    assert land(ctx, meta, asker("m", "k")) is Outcome.DONE
    assert "differ from the carried WIP commit" in capsys.readouterr().out
    assert (repo / "a.txt").read_text() == "edited again since\n"
    assert ctx.state.worktree("s2").exists()


def test_declining_the_clear_leaves_main_alone(repo: Path) -> None:
    (repo / "a.txt").write_text("in progress\n")
    ctx, meta = start("s3", COMMIT_C, "--carry-dirty")
    assert land(ctx, meta, asker("s", "n", "k")) is Outcome.DONE
    assert (repo / "a.txt").read_text() == "in progress\n"
    assert sh("git", "diff", "--cached", "--name-only", cwd=repo) == ""


@pytest.mark.usefixtures("repo")
def test_no_commits_only_offers_resume_keep_discard(capsys: pytest.CaptureFixture[str]) -> None:
    ctx, meta = start("n1", "true")
    assert land(ctx, meta, asker("m", "r")) is Outcome.RESUME
    assert "unknown choice 'm'" in capsys.readouterr().out
    assert ctx.state.worktree("n1").exists()


def test_discard_needs_confirmation(repo: Path) -> None:
    ctx, meta = start("x1", COMMIT_C)
    assert land(ctx, meta, asker("x", "n", "x", "y")) is Outcome.DONE
    assert not ctx.state.worktree("x1").exists()
    assert sh("git", "branch", "--list", "wt/x1", cwd=repo) == ""


def test_merge_with_conflict_keeps_worktree(repo: Path) -> None:
    ctx, meta = start("c1", "echo theirs > a.txt && git commit -qam theirs")
    (repo / "a.txt").write_text("ours\n")
    sh("git", "commit", "-qam", "ours", cwd=repo)
    assert land(ctx, meta, asker("m", "y", "k")) is Outcome.DONE
    assert ctx.state.worktree("c1").exists()
    sh("git", "merge", "--abort", cwd=repo)


def test_claude_session_is_pinned_then_resumed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude"))
    sid = "0b6d0c6e-0000-4000-8000-000000000000"
    first = cli.agent_command("claude", "intro", [], sid)
    assert first[-2:] == ["--session-id", sid]
    transcript = tmp_path / "claude" / "projects" / "-some-dir" / f"{sid}.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")
    assert cli.agent_command("claude", "intro", ["--model", "x"], sid)[-4:] == ["--resume", sid, "--model", "x"]
    assert "--resume" not in cli.agent_command("claude", "intro", ["--continue"], sid)[:-1]
    assert cli.agent_command("sh", "intro", ["-c", "x"], sid) == ["sh", "-c", "x"]
