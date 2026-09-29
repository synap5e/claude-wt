"""claude-wt: launch a coding agent in a throwaway git worktree.

claude-wt [new] [SLUG] [options] [-- AGENT ARGS...]
claude-wt resume SLUG [options] [-- AGENT ARGS...]
claude-wt ls
claude-wt rm SLUG [--force]
"""

from __future__ import annotations

import argparse
import dataclasses
import os
import re
import shlex
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from . import deps, gitops, guard, sandbox
from .errors import DirtyTreeError, GitError, WorktreeNotFoundError, WtError
from .prompt import PromptContext, render
from .state import Meta, RepoState, sweep_stale_boots

SUBCOMMANDS = {"new", "resume", "ls", "rm"}
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
BRANCH_PREFIX = "wt/"


def _launch_options(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--deps",
        choices=[m.value for m in deps.Mode],
        default=deps.Mode.AUTO.value,
        help="auto: overlay/reflink node_modules, install venvs (default); install: always install; none: skip",
    )
    p.add_argument("--no-sandbox", action="store_true", help="don't wrap the agent in bubblewrap")
    p.add_argument("--cmd", default="claude", help="agent command (default: claude); shell-split")
    p.add_argument("--print-prompt", action="store_true", help="print the intro prompt and exit without launching")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-wt", description=__doc__, formatter_class=argparse.RawTextHelpFormatter
    )
    sub = parser.add_subparsers(dest="command", required=True)

    new = sub.add_parser("new", help="create a worktree and launch the agent in it")
    new.add_argument("slug", nargs="?", help="worktree name; branch becomes wt/<slug> (default: timestamp)")
    new.add_argument("--base", default="HEAD", help="commit to branch from (default: HEAD)")
    dirty = new.add_mutually_exclusive_group()
    dirty.add_argument("--allow-dirty", action="store_true", help="start from --base even with uncommitted changes")
    dirty.add_argument(
        "--carry-dirty",
        action="store_true",
        help="snapshot uncommitted changes (incl. untracked) into a WIP commit on the new branch; no hooks run",
    )
    _launch_options(new)

    resume = sub.add_parser("resume", help="relaunch the agent in an existing worktree")
    resume.add_argument("slug")
    _launch_options(resume)

    sub.add_parser("ls", help="list this repo's worktrees")

    rm = sub.add_parser("rm", help="remove a worktree (keeps the branch unless it's merged or empty)")
    rm.add_argument("slug")
    rm.add_argument("--force", action="store_true", help="discard uncommitted changes and delete the branch")
    return parser


def split_agent_args(argv: list[str]) -> tuple[list[str], list[str]]:
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1 :]
    return argv, []


def normalise_argv(argv: list[str]) -> list[str]:
    """`claude-wt foo` and `claude-wt --base x` mean `claude-wt new ...`."""
    if argv and (argv[0] in SUBCOMMANDS or argv[0] in {"-h", "--help"}):
        return argv
    return ["new", *argv]


# ---------------------------------------------------------------- new


@dataclass(frozen=True)
class Context:
    repo: gitops.Repo
    state: RepoState


def _context() -> Context:
    repo = gitops.discover(Path.cwd())
    return Context(repo, RepoState.for_repo(repo.common_dir, repo.name))


def _validate_slug(ctx: Context, slug: str) -> None:
    if not SLUG_RE.match(slug):
        raise WtError(f"invalid slug {slug!r}: use letters, digits, '.', '_' and '-'")
    if ctx.state.meta_path(slug).exists() or ctx.state.worktree(slug).exists():
        raise WtError(f"worktree {slug!r} already exists; use `claude-wt resume {slug}`")
    if gitops.branch_exists(ctx.repo.toplevel, BRANCH_PREFIX + slug):
        raise WtError(f"branch {BRANCH_PREFIX}{slug} already exists; pick another slug")


def _start_commit(ctx: Context, args: argparse.Namespace, base_sha: str) -> tuple[str, bool]:
    entries = gitops.dirty_entries(ctx.repo.toplevel)
    if not entries:
        return base_sha, False
    if args.allow_dirty:
        return base_sha, False
    if not args.carry_dirty:
        raise DirtyTreeError(str(ctx.repo.toplevel), entries)
    if base_sha != ctx.repo.head:
        raise WtError("--carry-dirty needs --base to be the current HEAD: the changes were made against it")
    message = f"WIP: uncommitted changes carried over by claude-wt\n\nFrom {ctx.repo.toplevel} ({len(entries)} paths)."
    return gitops.snapshot_commit(ctx.repo.toplevel, ctx.repo.head, message), True


def cmd_new(args: argparse.Namespace, agent_args: list[str]) -> int:
    ctx = _context()
    slug = args.slug or time.strftime("%Y%m%d-%H%M%S")
    _validate_slug(ctx, slug)
    base_sha = gitops.resolve_commit(ctx.repo.toplevel, args.base)
    start, carried = _start_commit(ctx, args, base_sha)
    meta = Meta(
        slug=slug,
        branch=BRANCH_PREFIX + slug,
        base_ref=ctx.repo.branch if args.base == "HEAD" and ctx.repo.branch else args.base,
        base_sha=base_sha,
        main_checkout=str(ctx.repo.toplevel),
        carried_dirty=carried,
    )
    path = ctx.state.worktree(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    gitops.add_worktree(ctx.repo.toplevel, path, meta.branch, start)
    ctx.state.write_meta(meta)
    return launch(ctx, meta, args, agent_args)


def cmd_resume(args: argparse.Namespace, agent_args: list[str]) -> int:
    ctx = _context()
    meta = ctx.state.read_meta(args.slug)
    if meta is None or not ctx.state.worktree(args.slug).exists():
        raise WorktreeNotFoundError(args.slug)
    return launch(ctx, meta, args, agent_args)


# ---------------------------------------------------------------- launch


def _overlays(ctx: Context, meta: Meta, steps: list[deps.Step]) -> list[sandbox.Overlay]:
    main, wt = Path(meta.main_checkout), ctx.state.worktree(meta.slug)
    overlays: list[sandbox.Overlay] = []
    for i, step in enumerate(s for s in steps if s.strategy is deps.Strategy.OVERLAY):
        scratch = ctx.state.overlay_dir(meta.slug, i)
        ov = sandbox.Overlay(main / step.dep.rel, wt / step.dep.rel, scratch / "upper", scratch / "work")
        sandbox.prepare_overlay(ov)
        overlays.append(ov)
    return overlays


def agent_command(cmd: str, prompt_text: str, agent_args: list[str]) -> list[str]:
    argv = shlex.split(cmd)
    if Path(argv[0]).name == "claude":
        argv += ["--append-system-prompt", prompt_text]
    return argv + agent_args


def agent_env(ctx: Context, meta: Meta, prompt_file: Path) -> dict[str, str]:
    bin_dir = ctx.state.bin_dir
    return {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        guard.SHIM_DIR_ENV: str(bin_dir),
        "CLAUDE_WT_WORKTREE": str(ctx.state.worktree(meta.slug)),
        "CLAUDE_WT_BRANCH": meta.branch,
        "CLAUDE_WT_MAIN": meta.main_checkout,
        "CLAUDE_WT_PROMPT_FILE": str(prompt_file),
    }


def launch(ctx: Context, meta: Meta, args: argparse.Namespace, agent_args: list[str]) -> int:
    sweep_stale_boots()
    wt, main = ctx.state.worktree(meta.slug), Path(meta.main_checkout)
    caps = sandbox.Capabilities(False, False, "--no-sandbox") if args.no_sandbox else sandbox.probe()
    steps = deps.plan(deps.detect(main), main, deps.Mode(args.deps), sandboxed=caps.bwrap and caps.overlay)
    prompt_ctx = PromptContext(
        wt, meta.branch, meta.base_ref, meta.base_sha, main, meta.carried_dirty, caps.bwrap, steps
    )
    if args.print_prompt:
        print(render(prompt_ctx), end="")
        return 0

    steps = deps.apply(steps, main, wt)
    overlays = _overlays(ctx, meta, steps)
    prompt_text = render(dataclasses.replace(prompt_ctx, steps=steps))
    prompt_file = ctx.state.meta_path(meta.slug).with_suffix(".prompt.md")
    prompt_file.write_text(prompt_text)
    guard.install_shim(ctx.state.bin_dir)

    cmd = agent_command(args.cmd, prompt_text, agent_args)
    if caps.bwrap:
        cmd = sandbox.build_argv(main, ctx.repo.common_dir, wt, overlays, cmd)
    print(f"claude-wt: {meta.branch} at {wt}")
    print(f"claude-wt: sandbox {'on (main checkout read-only)' if caps.bwrap else f'off: {caps.detail}'}", flush=True)

    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl-C belongs to the agent
    try:
        code = subprocess.run(cmd, cwd=wt, env=agent_env(ctx, meta, prompt_file), check=False).returncode
    finally:
        signal.signal(signal.SIGINT, previous)
    print(summary(ctx, meta))
    return code


def summary(ctx: Context, meta: Meta) -> str:
    wt = ctx.state.worktree(meta.slug)
    ahead = gitops.commits_ahead(wt, meta.base_sha)
    dirty = len(gitops.dirty_entries(wt))
    state = "clean" if dirty == 0 else f"{dirty} uncommitted path(s)"
    return (
        f"\nclaude-wt: {meta.branch}: {ahead} commit(s) ahead of {meta.base_ref}, {state}\n"
        f"  worktree: {wt}\n"
        f"  resume:   claude-wt resume {meta.slug}\n"
        f"  remove:   claude-wt rm {meta.slug}"
    )


# ---------------------------------------------------------------- ls / rm


def cmd_ls(_args: argparse.Namespace, _agent_args: list[str]) -> int:
    ctx = _context()
    slugs = ctx.state.slugs()
    if not slugs:
        print("no claude-wt worktrees for this repo")
        return 0
    for slug in slugs:
        meta = ctx.state.read_meta(slug)
        wt = ctx.state.worktree(slug)
        if meta is None or not wt.exists():
            print(f"{slug:24} (missing worktree; `claude-wt rm {slug}` to forget it)")
            continue
        ahead = gitops.commits_ahead(wt, meta.base_sha)
        dirty = len(gitops.dirty_entries(wt))
        print(f"{slug:24} {meta.branch:30} +{ahead} commits  {dirty} dirty  {wt}")
    return 0


def cmd_rm(args: argparse.Namespace, _: list[str]) -> int:
    ctx = _context()
    meta = ctx.state.read_meta(args.slug)
    if meta is None:
        raise WorktreeNotFoundError(args.slug)
    wt, top = ctx.state.worktree(args.slug), ctx.repo.toplevel
    if wt.exists():
        entries = gitops.dirty_entries(wt)
        if entries and not args.force:
            raise WtError(f"{wt} has {len(entries)} uncommitted path(s); commit them or pass --force")
        gitops.remove_worktree(top, wt, force=True)
    ctx.state.forget(args.slug)
    print(f"claude-wt: removed {wt}")
    if gitops.branch_exists(top, meta.branch):
        _drop_branch(top, meta, args.force)
    return 0


def _drop_branch(top: Path, meta: Meta, force: bool) -> None:
    empty = gitops.commits_ahead(top, meta.base_sha, meta.branch) == 0
    try:
        gitops.delete_branch(top, meta.branch, force=force or empty)
        print(f"claude-wt: deleted branch {meta.branch}")
    except GitError:
        print(
            f"claude-wt: kept branch {meta.branch}: it has commits git doesn't see as merged "
            f"(squash merges look unmerged). Delete with `git branch -D {meta.branch}` once you're sure."
        )


HANDLERS = {"new": cmd_new, "resume": cmd_resume, "ls": cmd_ls, "rm": cmd_rm}


def main(argv: list[str] | None = None) -> None:
    own, agent_args = split_agent_args(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(normalise_argv(own))
    try:
        code = HANDLERS[args.command](args, agent_args)
    except WtError as e:
        print(f"claude-wt: {e}", file=sys.stderr)
        code = 2
    raise SystemExit(code)
