"""Which `git push` invocations the agent may run: its own branch, to its own name or a new remote branch.

The sandbox pins local refs, but the network isn't sandboxed, so without this an agent could force-push over
origin/main. Pure: all repo state comes through `Lookups`.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Lookups:
    own_branch: str
    config: Callable[[str], str | None]
    remote_has_branch: Callable[[str, str], bool]


# Options that consume the next argv element (the attached `--opt=value` forms need no special handling).
_WITH_VALUE = {"--repo", "--receive-pack", "--exec", "-o", "--push-option"}
_BLOCKED = {
    "--all": "`git push --all` pushes every branch",
    "--branches": "`git push --branches` pushes every branch",
    "--mirror": "`git push --mirror` rewrites every ref on the remote",
    "--tags": "`git push --tags` pushes tags",
    "--follow-tags": "`git push --follow-tags` pushes tags",
    "--delete": "`git push --delete` deletes remote refs",
    "--prune": "`git push --prune` deletes remote branches",
}
_SHORT_CLUSTER = re.compile(r"^-[A-Za-z]{2,}$")
_ALWAYS_PROTECTED = {"main", "master"}


@dataclass(frozen=True)
class PushArgs:
    remote: str | None
    refspecs: list[str]
    blocked: str | None


def parse(args: list[str]) -> PushArgs:
    positional: list[str] = []
    repo: str | None = None
    i = 0
    while i < len(args):
        a = args[i]
        if a in _WITH_VALUE:
            if a == "--repo" and i + 1 < len(args):
                repo = args[i + 1]
            i += 2
            continue
        if a.startswith("--repo="):
            repo = a.split("=", 1)[1]
        elif a in _BLOCKED:
            return PushArgs(None, [], _BLOCKED[a])
        elif a == "-d" or (_SHORT_CLUSTER.match(a) and "d" in a):
            return PushArgs(None, [], _BLOCKED["--delete"])
        elif not a.startswith("-"):
            positional.append(a)
        i += 1
    remote = repo if repo is not None else (positional[0] if positional else None)
    refspecs = positional if repo is not None else positional[1:]
    return PushArgs(remote, refspecs, None)


def _short(ref: str) -> str:
    return ref.removeprefix("refs/heads/")


def _check_destination(remote: str, dst: str, lk: Lookups) -> str | None:
    if dst.startswith("refs/"):
        return f"pushing to {dst} writes refs other than branches"
    if dst == lk.own_branch:
        return None
    if dst in _ALWAYS_PROTECTED or lk.remote_has_branch(remote, dst):
        return f"pushing to {remote}/{dst} would overwrite a branch that isn't yours"
    return None


def _check_refspec(remote: str, spec: str, lk: Lookups) -> str | None:
    spec = spec.removeprefix("+")
    src, sep, dst = spec.partition(":")
    if sep and not src:
        return f"`git push {remote} {spec}` deletes a remote branch"
    own = {"HEAD", "@", lk.own_branch, f"refs/heads/{lk.own_branch}"}
    if src not in own:
        return f"`git push {remote} {spec}` pushes something other than your branch {lk.own_branch}"
    return _check_destination(remote, _short(dst) if sep else lk.own_branch, lk)


def _implicit_destination(lk: Lookups) -> str:
    """Where a bare `git push` sends the current branch."""
    mode = lk.config("push.default") or "simple"
    merge = lk.config(f"branch.{lk.own_branch}.merge")
    if mode in {"upstream", "tracking"} and merge:
        return _short(merge)
    return lk.own_branch


def verdict(args: list[str], lk: Lookups) -> str | None:
    parsed = parse(args)
    if parsed.blocked:
        return parsed.blocked
    remote = parsed.remote or lk.config(f"branch.{lk.own_branch}.remote") or "origin"
    if not parsed.refspecs:
        if lk.config("push.default") == "matching":
            return "`git push` with push.default=matching pushes every matching branch; name your branch explicitly"
        if lk.config(f"remote.{remote}.push"):
            return f"remote.{remote}.push is configured, so a bare push may push other refs; name your branch"
        return _check_destination(remote, _implicit_destination(lk), lk)
    for spec in parsed.refspecs:
        reason = _check_refspec(remote, spec, lk)
        if reason:
            return reason
    return None
