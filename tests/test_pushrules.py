from __future__ import annotations

import pytest

from claude_wt.pushrules import Lookups, verdict

OWN = "wt/s1/work"
REMOTE_BRANCHES = {("origin", "main"), ("origin", "feature"), ("origin", OWN)}


def lookups(config: dict[str, str] | None = None) -> Lookups:
    cfg = config or {}
    return Lookups(OWN, cfg.get, lambda remote, branch: (remote, branch) in REMOTE_BRANCHES)


@pytest.mark.parametrize(
    "args",
    [
        ["origin", "HEAD"],
        ["origin", OWN],
        ["-u", "origin", "HEAD"],
        ["origin", f"HEAD:{OWN}"],
        ["--force", "origin", "HEAD"],
        ["origin", "+HEAD"],
        ["origin", "HEAD:my-pr-branch"],  # a new remote branch
        ["origin", f"refs/heads/{OWN}:refs/heads/my-pr-branch"],
        ["--force-with-lease", "origin", "HEAD:my-pr-branch"],
        ["-o", "ci.skip", "origin", "HEAD"],
        [],  # bare push: current branch to its own name
        ["origin"],
    ],
)
def test_allows_own_branch_to_own_or_new_name(args: list[str]) -> None:
    assert verdict(args, lookups()) is None


@pytest.mark.parametrize(
    "args",
    [
        ["origin", "HEAD:main"],
        ["-f", "origin", "HEAD:main"],
        ["origin", "+HEAD:feature"],
        ["origin", "HEAD:refs/heads/feature"],
        ["origin", "main"],  # someone else's branch as the source
        ["origin", "feature:my-pr-branch"],
        ["origin", "abc123:refs/heads/x"],
        ["origin", ":feature"],  # delete
        ["origin", "--delete", "feature"],
        ["-d", "origin", OWN],
        ["-fd", "origin", OWN],
        ["--all", "origin"],
        ["--mirror", "origin"],
        ["--tags", "origin"],
        ["--follow-tags", "origin", "HEAD"],
        ["--prune", "origin", "HEAD"],
        ["origin", "HEAD:refs/tags/v1"],
        ["upstream", "HEAD:master"],  # main/master stay protected with no local tracking ref
        ["origin", "HEAD", "main"],  # any bad refspec blocks the whole push
    ],
)
def test_blocks_everything_else(args: list[str]) -> None:
    assert verdict(args, lookups()) is not None


def test_bare_push_follows_push_default() -> None:
    assert verdict([], lookups({"push.default": "matching"})) is not None
    upstream_main = {"push.default": "upstream", f"branch.{OWN}.merge": "refs/heads/main"}
    assert verdict([], lookups(upstream_main)) is not None
    upstream_own = {"push.default": "upstream", f"branch.{OWN}.merge": f"refs/heads/{OWN}"}
    assert verdict([], lookups(upstream_own)) is None
    assert verdict(["origin"], lookups({"remote.origin.push": "refs/heads/*"})) is not None


def test_repo_option_is_the_remote() -> None:
    assert verdict(["--repo", "origin", "HEAD:feature"], lookups()) is not None
    assert verdict(["--repo=origin", "HEAD"], lookups()) is None
