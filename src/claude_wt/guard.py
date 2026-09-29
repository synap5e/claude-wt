"""The `git` shim put first on the agent's PATH.

It refuses commands that would move a worktree's HEAD to another branch (or detach it) *before* git runs.
A post-hoc git hook can't do this: `reference-transaction` fires after checkout has already rewritten the
working tree, so aborting there leaves the other branch's files staged on the old branch.

Everything else is exec'd straight through to the next `git` on PATH.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import pushrules

ALLOW_ENV = "CLAUDE_WT_ALLOW"
SHIM_DIR_ENV = "CLAUDE_WT_SHIM_DIR"

# Global options that consume the following argv element.
_GLOBAL_WITH_ARG = {
    "-C",
    "-c",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--exec-path",
    "--super-prefix",
    "--config-env",
}

# Subcommands that are never HEAD-moving, so no alias lookup is needed for them.
_KNOWN_SAFE = {
    "add", "am", "apply", "blame", "branch", "cat-file", "cherry-pick", "clean", "commit", "config", "describe",
    "diff", "fetch", "format-patch", "grep", "log", "ls-files", "ls-tree", "merge", "merge-base", "mv", "notes",
    "pull", "rebase", "reflog", "remote", "reset", "restore", "rev-list", "rev-parse", "revert", "rm",
    "shortlog", "show", "stash", "status", "submodule", "symbolic-ref", "tag", "update-index", "bisect", "help",
}  # fmt: skip

_WORKTREE_BLOCKED = {"add", "move", "remove"}


@dataclass(frozen=True)
class Invocation:
    global_args: list[str]
    subcommand: str | None
    args: list[str]


def split_argv(argv: list[str]) -> Invocation:
    i = 0
    while i < len(argv):
        tok = argv[i]
        if tok in _GLOBAL_WITH_ARG:
            i += 2
        elif tok.startswith("-"):
            i += 1
        else:
            return Invocation(argv[:i], tok, argv[i + 1 :])
    return Invocation(argv, None, [])


def _is_help(args: list[str]) -> bool:
    return any(a in {"-h", "--help"} for a in args)


def _checkout_verdict(args: list[str], is_commit: Callable[[str], bool]) -> str | None:
    if "--" in args or any(a in {"-p", "--patch"} for a in args):
        return None  # path mode: `git checkout [<tree-ish>] -- <paths>`
    if any(a in {"-b", "-B", "--orphan", "--detach"} for a in args):
        return "`git checkout -b/-B/--orphan/--detach` moves this worktree's HEAD"
    positional = [a for a in args if not a.startswith("-")]
    if "-" in args:
        return "`git checkout -` switches branches"
    if len(positional) == 1 and is_commit(positional[0]):
        return f"`git checkout {positional[0]}` would move this worktree's HEAD"
    return None  # `git checkout <file>` or `git checkout <tree-ish> <paths...>` restores files


def verdict(
    inv: Invocation,
    is_commit: Callable[[str], bool],
    alias_of: Callable[[str], str | None],
    push: pushrules.Lookups | None = None,
) -> str | None:
    """Reason to block, or None to pass through. Pure apart from the injected lookups.

    `push` is None outside a claude-wt session (no own branch known), which leaves pushes alone."""
    sub, args = inv.subcommand, inv.args
    if sub is None or _is_help(args):
        return None
    if sub not in _KNOWN_SAFE and sub not in {"switch", "checkout", "worktree", "push"}:
        expansion = alias_of(sub)
        if expansion and not expansion.startswith("!"):
            words = expansion.split()
            expanded = Invocation(inv.global_args, words[0], words[1:] + args)
            return verdict(expanded, is_commit, lambda _: None, push)
    if sub == "push":
        return pushrules.verdict(args, push) if push else None
    if sub == "switch":
        return "`git switch` moves this worktree's HEAD"
    if sub == "checkout":
        return _checkout_verdict(args, is_commit)
    if sub == "worktree" and args and args[0] in _WORKTREE_BLOCKED:
        return f"`git worktree {args[0]}` manages other worktrees"
    return None


def path_without(shim_dir: str, path: str) -> str:
    """PATH minus the shim. Handing off with this PATH stops other PATH-shadowing git wrappers
    (which look for "the first git that isn't me") from finding the shim again and looping."""
    shim = os.path.realpath(shim_dir)
    return os.pathsep.join(e for e in path.split(os.pathsep) if e and os.path.realpath(e) != shim)


def find_real_git(path: str) -> str:
    candidate = shutil.which("git", path=path)
    if candidate is None:
        raise SystemExit("claude-wt shim: no git found on PATH after the shim")
    return candidate


def _run_git(real_git: str, global_args: list[str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run([real_git, *global_args, *args], capture_output=True, text=True, check=False)


def block_message(reason: str) -> str:
    wt = os.environ.get("CLAUDE_WT_WORKTREE", "this worktree")
    branch = os.environ.get("CLAUDE_WT_BRANCH", "its branch")
    return (
        f"claude-wt: blocked: {reason}.\n"
        f"This session is pinned to {wt} on branch {branch}. Commit your work here instead.\n"
        f"If the user has explicitly asked for this, rerun with {ALLOW_ENV}=1."
    )


def main() -> None:
    argv = sys.argv[1:]
    os.environ["PATH"] = path_without(
        os.environ.get(SHIM_DIR_ENV) or str(Path(sys.argv[0]).parent), os.environ.get("PATH", "")
    )
    real_git = find_real_git(os.environ["PATH"])
    if os.environ.get(ALLOW_ENV) != "1":
        inv = split_argv(argv)

        def is_commit(ref: str) -> bool:
            return (
                _run_git(real_git, inv.global_args, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}").returncode
                == 0
            )

        def alias_of(name: str) -> str | None:
            out = _run_git(real_git, inv.global_args, "config", "--get", f"alias.{name}").stdout.strip()
            return out or None

        def config(key: str) -> str | None:
            return _run_git(real_git, inv.global_args, "config", "--get", key).stdout.strip() or None

        def remote_has_branch(remote: str, branch: str) -> bool:
            ref = f"refs/remotes/{remote}/{branch}"
            return _run_git(real_git, inv.global_args, "rev-parse", "--verify", "--quiet", ref).returncode == 0

        own = os.environ.get("CLAUDE_WT_BRANCH")
        push = pushrules.Lookups(own, config, remote_has_branch) if own else None
        reason = verdict(inv, is_commit, alias_of, push)
        if reason:
            print(block_message(reason), file=sys.stderr)
            raise SystemExit(1)
    os.execv(real_git, [real_git, *argv])


SHIM_TEMPLATE = """#!{python}
import sys
sys.path.insert(0, {pkg_parent!r})
from claude_wt.guard import main
main()
"""


def install_shim(bin_dir: Path) -> Path:
    """Write the shim, bound to this interpreter so it works regardless of what python is on PATH."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "git"
    pkg_parent = str(Path(__file__).resolve().parent.parent)
    content = SHIM_TEMPLATE.format(python=sys.executable, pkg_parent=pkg_parent)
    if not shim.exists() or shim.read_text() != content:
        tmp = shim.with_suffix(".tmp")
        tmp.write_text(content)
        tmp.chmod(0o755)
        tmp.replace(shim)
    return shim
