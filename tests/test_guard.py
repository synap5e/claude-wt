from __future__ import annotations

import pytest

from claude_wt.guard import split_argv, verdict

COMMITS = {"main", "other", "HEAD", "origin/x", "abc123"}


def check(argv: list[str], aliases: dict[str, str] | None = None) -> str | None:
    return verdict(split_argv(argv), lambda ref: ref in COMMITS, lambda name: (aliases or {}).get(name))


@pytest.mark.parametrize(
    "argv",
    [
        ["switch", "other"],
        ["switch", "-c", "new"],
        ["checkout", "other"],
        ["checkout", "-q", "other"],
        ["checkout", "abc123"],
        ["checkout", "-b", "new"],
        ["checkout", "--detach"],
        ["checkout", "-t", "origin/x"],
        ["checkout", "-"],
        ["-C", "/elsewhere", "switch", "main"],
        ["-c", "core.pager=cat", "checkout", "main"],
        ["worktree", "add", "../x"],
        ["worktree", "remove", "x"],
    ],
)
def test_blocks_head_moves(argv: list[str]) -> None:
    assert check(argv) is not None


@pytest.mark.parametrize(
    "argv",
    [
        ["status"],
        ["commit", "-m", "switch"],
        ["checkout", "--", "a.txt"],
        ["checkout", "a.txt"],
        ["checkout", "HEAD", "a.txt"],
        ["checkout", "main", "--", "a.txt"],
        ["checkout", "-p"],
        ["switch", "--help"],
        ["worktree", "list"],
        ["rebase", "main"],
        ["reset", "--hard", "HEAD~1"],
        [],
        ["--version"],
    ],
)
def test_allows_everything_else(argv: list[str]) -> None:
    assert check(argv) is None


def test_follows_aliases() -> None:
    assert check(["co", "other"], {"co": "checkout"}) is not None
    assert check(["sw", "main"], {"sw": "switch -q"}) is not None
    assert check(["st"], {"st": "status -s"}) is None


def test_shell_aliases_are_not_parsed() -> None:
    assert check(["x", "other"], {"x": "!git switch"}) is None
