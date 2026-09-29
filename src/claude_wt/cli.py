"""claude-wt: launch a coding agent in a throwaway git worktree.

claude-wt [new] [SLUG] [options] [-- AGENT ARGS...]
claude-wt resume SLUG [options] [-- AGENT ARGS...]
claude-wt land SLUG
claude-wt ls
claude-wt rm SLUG [--force]

When the agent exits, a menu offers to merge or squash its branch into the main checkout, resume, keep or discard.
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
import uuid
from pathlib import Path

from . import deps, gitops, guard, sandbox
from .context import Context, branch_for, remove
from .errors import DirtyTreeError, WorktreeNotFoundError, WtError
from .land import Outcome, land
from .prompt import PromptContext, render
from .state import Meta, sweep_stale_boots

SUBCOMMANDS = {"new", "resume", "land", "ls", "rm"}
REQUIRE_SANDBOX_ENV = "CLAUDE_WT_REQUIRE_SANDBOX"
SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


def _launch_options(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--deps",
        choices=[m.value for m in deps.Mode],
        default=deps.Mode.AUTO.value,
        help="auto: overlay/reflink node_modules, install venvs (default); install: always install; none: skip",
    )
    box = p.add_mutually_exclusive_group()
    box.add_argument("--no-sandbox", action="store_true", help="don't wrap the agent in bubblewrap")
    box.add_argument(
        "-S",
        "--require-sandbox",
        action="store_true",
        help=f"refuse to start unless the sandbox works (default when {REQUIRE_SANDBOX_ENV}=1)",
    )
    p.add_argument("--cmd", default="claude", help="agent command (default: claude); shell-split")
    p.add_argument("--print-prompt", action="store_true", help="print the intro prompt and exit without launching")
    p.add_argument("--no-land", action="store_true", help="skip the merge/keep menu after the agent exits")


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

    land_p = sub.add_parser("land", help="merge, squash, resume, keep or discard a worktree's work")
    land_p.add_argument("slug")

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


def _validate_slug(ctx: Context, slug: str) -> None:
    if not SLUG_RE.match(slug):
        raise WtError(f"invalid slug {slug!r}: use letters, digits, '.', '_' and '-'")
    if ctx.state.meta_path(slug).exists() or ctx.state.worktree(slug).exists():
        raise WtError(f"worktree {slug!r} already exists; use `claude-wt resume {slug}`")
    if gitops.branch_exists(ctx.repo.toplevel, branch_for(slug)):
        raise WtError(f"branch {branch_for(slug)} already exists; pick another slug")


def _start_commit(ctx: Context, args: argparse.Namespace, base_sha: str) -> tuple[str, str | None]:
    """The commit the new branch starts at, and the carried-changes WIP commit if there is one."""
    entries = gitops.dirty_entries(ctx.repo.toplevel)
    if not entries or args.allow_dirty:
        return base_sha, None
    if not args.carry_dirty:
        raise DirtyTreeError(str(ctx.repo.toplevel), entries)
    if base_sha != ctx.repo.head:
        raise WtError("--carry-dirty needs --base to be the current HEAD: the changes were made against it")
    message = f"WIP: uncommitted changes carried over by claude-wt\n\nFrom {ctx.repo.toplevel} ({len(entries)} paths)."
    wip = gitops.snapshot_commit(ctx.repo.toplevel, ctx.repo.head, message)
    return wip, wip


def sandbox_capabilities(args: argparse.Namespace, repo_dir: Path) -> sandbox.Capabilities:
    """What the sandbox can do here. Raises when it's required and unavailable. --no-sandbox beats the env default."""
    if args.no_sandbox:
        return sandbox.Capabilities(False, False, "--no-sandbox")
    caps = sandbox.probe()
    if caps.bwrap and not caps.overlay:
        # The shared .git needs a throwaway overlay; read-only alone breaks rebase (see sandbox.py).
        caps = sandbox.Capabilities(False, False, caps.detail)
    elif caps.bwrap and gitops.ref_format(repo_dir) != "files":
        # reftable keeps every ref in one set of files, so there's no per-branch directory to make writable.
        caps = sandbox.Capabilities(False, False, "reftable ref storage can't be pinned per branch")
    required = args.require_sandbox or os.environ.get(REQUIRE_SANDBOX_ENV) == "1"
    if required and not caps.bwrap:
        raise WtError(f"sandbox required but unavailable ({caps.detail}); not starting")
    return caps


def cmd_new(args: argparse.Namespace, agent_args: list[str]) -> int:
    ctx = Context.here()
    if not args.print_prompt:
        sandbox_capabilities(args, ctx.repo.toplevel)  # fail before creating anything
    slug = args.slug or time.strftime("%Y%m%d-%H%M%S")
    _validate_slug(ctx, slug)
    base_sha = gitops.resolve_commit(ctx.repo.toplevel, args.base)
    start, wip = _start_commit(ctx, args, base_sha)
    meta = Meta(
        slug=slug,
        branch=branch_for(slug),
        base_ref=ctx.repo.branch if args.base == "HEAD" and ctx.repo.branch else args.base,
        base_sha=base_sha,
        main_checkout=str(ctx.repo.toplevel),
        carried_dirty=wip is not None,
        wip_sha=wip,
        session_id=str(uuid.uuid4()) if is_claude(args.cmd) else None,
    )
    path = ctx.state.worktree(slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    gitops.add_worktree(ctx.repo.toplevel, path, meta.branch, start)
    ctx.state.write_meta(meta)
    return run_session(ctx, meta, args, agent_args)


def cmd_resume(args: argparse.Namespace, agent_args: list[str]) -> int:
    ctx = Context.here()
    meta = ctx.state.read_meta(args.slug)
    if meta is None or not ctx.state.worktree(args.slug).exists():
        raise WorktreeNotFoundError(args.slug)
    if meta.session_id is None and is_claude(args.cmd):
        meta = dataclasses.replace(meta, session_id=str(uuid.uuid4()))
        ctx.state.write_meta(meta)
    return run_session(ctx, meta, args, agent_args)


def cmd_land(args: argparse.Namespace, _agent_args: list[str]) -> int:
    ctx = Context.here()
    meta = ctx.state.read_meta(args.slug)
    if meta is None or not ctx.state.worktree(args.slug).exists():
        raise WorktreeNotFoundError(args.slug)
    if land(ctx, meta) is Outcome.RESUME:
        return run_session(ctx, meta, build_parser().parse_args(["resume", args.slug]), [])
    return 0


def run_session(ctx: Context, meta: Meta, args: argparse.Namespace, agent_args: list[str]) -> int:
    """Launch, then hand the human the land menu; loop while they choose to resume."""
    while True:
        code = launch(ctx, meta, args, agent_args)
        if args.print_prompt:
            return code
        if args.no_land or not sys.stdin.isatty():
            print(summary(ctx, meta))
            return code
        if land(ctx, meta) is not Outcome.RESUME:
            return code
        agent_args = []  # the first launch's prompt was already delivered


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


SESSION_FLAGS = {"-r", "--resume", "-c", "--continue", "--session-id", "--fork-session"}


def is_claude(cmd: str) -> bool:
    return Path(shlex.split(cmd)[0]).name == "claude"


def claude_transcript_exists(session_id: str) -> bool:
    config = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    return any((config / "projects").glob(f"*/{session_id}.jsonl"))


def agent_command(cmd: str, prompt_text: str, agent_args: list[str], session_id: str | None) -> list[str]:
    argv = shlex.split(cmd)
    if not is_claude(cmd):
        return argv + agent_args
    argv += ["--append-system-prompt", prompt_text]
    if session_id and not SESSION_FLAGS & set(agent_args):
        # Continue this worktree's conversation if it got as far as writing a transcript; otherwise start it.
        argv += ["--resume" if claude_transcript_exists(session_id) else "--session-id", session_id]
    return argv + agent_args


# Background maintenance would try to pack refs into the read-only .git and print errors after every commit.
AGENT_GIT_CONFIG = (("maintenance.auto", "false"), ("gc.auto", "0"))


def with_git_config(env: dict[str, str], pairs: tuple[tuple[str, str], ...]) -> dict[str, str]:
    """Append config via GIT_CONFIG_COUNT/KEY_n/VALUE_n, keeping any the caller already set."""
    out = dict(env)
    start = int(out.get("GIT_CONFIG_COUNT", "0") or 0)
    for i, (key, value) in enumerate(pairs, start):
        out[f"GIT_CONFIG_KEY_{i}"] = key
        out[f"GIT_CONFIG_VALUE_{i}"] = value
    out["GIT_CONFIG_COUNT"] = str(start + len(pairs))
    return out


def agent_env(ctx: Context, meta: Meta, prompt_file: Path) -> dict[str, str]:
    bin_dir = ctx.state.bin_dir
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        guard.SHIM_DIR_ENV: str(bin_dir),
        "CLAUDE_WT_WORKTREE": str(ctx.state.worktree(meta.slug)),
        "CLAUDE_WT_BRANCH": meta.branch,
        "CLAUDE_WT_MAIN": meta.main_checkout,
        "CLAUDE_WT_PROMPT_FILE": str(prompt_file),
    }
    return with_git_config(env, AGENT_GIT_CONFIG)


def launch(ctx: Context, meta: Meta, args: argparse.Namespace, agent_args: list[str]) -> int:
    sweep_stale_boots()
    wt, main = ctx.state.worktree(meta.slug), Path(meta.main_checkout)
    caps = sandbox_capabilities(args, main)
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

    cmd = agent_command(args.cmd, prompt_text, agent_args, meta.session_id)
    if caps.bwrap:
        layout = sandbox.GitLayout(ctx.repo.common_dir, gitops.git_dir(wt), meta.branch)
        sandbox.prepare_git(layout)
        cmd = sandbox.build_argv(main, layout, wt, overlays, cmd)
    print(f"claude-wt: {meta.branch} at {wt}")
    status = "on (main checkout and other branches read-only)" if caps.bwrap else f"off: {caps.detail}"
    print(f"claude-wt: sandbox {status}", flush=True)

    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)  # Ctrl-C belongs to the agent
    try:
        code = subprocess.run(cmd, cwd=wt, env=agent_env(ctx, meta, prompt_file), check=False).returncode
    finally:
        signal.signal(signal.SIGINT, previous)
    return code


def summary(ctx: Context, meta: Meta) -> str:
    wt = ctx.state.worktree(meta.slug)
    ahead = gitops.commits_ahead(wt, meta.base_sha)
    dirty = len(gitops.dirty_entries(wt))
    state = "clean" if dirty == 0 else f"{dirty} uncommitted path(s)"
    return (
        f"\nclaude-wt: {meta.branch}: {ahead} commit(s) ahead of {meta.base_ref}, {state}\n"
        f"  worktree: {wt}\n"
        f"  land:     claude-wt land {meta.slug}\n"
        f"  resume:   claude-wt resume {meta.slug}\n"
        f"  remove:   claude-wt rm {meta.slug}"
    )


# ---------------------------------------------------------------- ls / rm


def cmd_ls(_args: argparse.Namespace, _agent_args: list[str]) -> int:
    ctx = Context.here()
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


def cmd_rm(args: argparse.Namespace, _agent_args: list[str]) -> int:
    ctx = Context.here()
    meta = ctx.state.read_meta(args.slug)
    if meta is None:
        raise WorktreeNotFoundError(args.slug)
    remove(ctx, meta, args.force)
    return 0


HANDLERS = {"new": cmd_new, "resume": cmd_resume, "land": cmd_land, "ls": cmd_ls, "rm": cmd_rm}


def main(argv: list[str] | None = None) -> None:
    own, agent_args = split_agent_args(sys.argv[1:] if argv is None else argv)
    args = build_parser().parse_args(normalise_argv(own))
    try:
        code = HANDLERS[args.command](args, agent_args)
    except WtError as e:
        print(f"claude-wt: {e}", file=sys.stderr)
        code = 2
    raise SystemExit(code)
