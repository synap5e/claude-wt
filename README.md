# claude-wt

Launch Claude Code (or another coding agent) in a throwaway git worktree, and keep it there.

```sh
cd my-repo
claude-wt                      # new worktree from HEAD, branch wt/<timestamp>, launches `claude`
claude-wt fix-login -- --model opus   # name it; everything after -- goes to the agent
claude-wt resume fix-login     # relaunch later
claude-wt ls
claude-wt rm fix-login
```
Linux only. Requires git and Python 3.11+. Uses [bubblewrap](https://github.com/containers/bubblewrap) when it's available.
No other dependencies.

```sh
uv tool install .   # from a clone
```

## What it does

1. **Refuses uncommitted changes** unless you choose what happens to them:
   - `--carry-dirty` snapshots them (including untracked files) into a `WIP:` commit on the new branch. It uses a
     throwaway index and `git commit-tree`, so no hooks run and your working tree and index are left exactly as
     they were.
   - `--allow-dirty` starts from the commit anyway and leaves the changes where they are.
2. **Creates the worktree** at `$XDG_STATE_HOME/claude-wt/<repo>-<hash>/<slug>` on branch `wt/<slug>`, from `--base`
   (default `HEAD`).
3. **Sets up dependency directories** (see below).
4. **Launches the agent** in the worktree with:
   - an intro appended to Claude's system prompt: which worktree and branch it's on, the rules, and what happened to
     dependency dirs. It's also written to `$CLAUDE_WT_PROMPT_FILE` for other agents.
   - a `git` shim first on its `PATH` that refuses `switch`, branch-moving `checkout`, and `worktree add/move/remove`
     *before* git runs. Aliases are followed. `git checkout -- <file>` and `git checkout <rev> <paths>` still work.
   - if bubblewrap works: the main checkout mounted **read-only**, with the shared `.git` still writable so commits
     work.
5. **Prints a summary** on exit: commits ahead, uncommitted paths, and the resume and remove commands.

`claude-wt rm` refuses a worktree with uncommitted changes (`--force` overrides). It deletes the branch only if it's
empty or git sees it as merged. Squash-merged branches look unmerged, so they're kept and you get the command to
delete them.

### Why a PATH shim and not a git hook

The `reference-transaction` hook can abort a HEAD move, but it fires *after* `git switch` has rewritten the working
tree. Aborting there leaves the other branch's files staged on the old branch. It also breaks `git rebase`, which
detaches HEAD partway through.

The shim is a guardrail, not a security boundary. `CLAUDE_WT_ALLOW_SWITCH=1`, or calling git by its absolute path,
bypasses it. The block message tells the agent about the override and says to use it only when the user asks.

If you already have a PATH-shadowing git wrapper, the shim removes itself from `PATH` before handing off, so the two
don't find each other in a loop.

## Dependency directories (`--deps`)

Symlinking or copying `.venv` is wrong. A uv venv has the main checkout's absolute path baked into its editable
install (`_editable_impl_*.pth`), so the worktree's Python would import **main's** code while you edit the
worktree's, and tests would pass against unchanged code.

| | `auto` (default) | `install` | `none` |
|---|---|---|---|
| `.venv` (with `uv.lock`) | `uv sync --frozen` | `uv sync --frozen` | skip |
| `node_modules` | overlay if sandboxed, else reflink copy, else install | install from lockfile | skip |

- **overlay**: bubblewrap overlayfs. Main's `node_modules` is the read-only lower layer, and writes go to
  `$CLAUDE_WT_VOLATILE/<boot_id>/...` (default `/tmp/claude-wt-<uid>`). It's instant, works on any filesystem, and
  leaves main untouched. Writes survive `resume` but are swept after a reboot. Only the agent's process tree sees
  the mount; from outside, the worktree's `node_modules` is an empty directory.
- **reflink**: `cp --reflink=always`, a real copy-on-write copy. Needs btrfs or XFS, on the same filesystem as main.
  Falls back to install.
- **install**: `pnpm`/`bun`/`yarn`/`npm ci`, chosen from the lockfile. A workspace root's install covers nested
  packages.

Only directories next to a tracked `pyproject.toml`/`package.json` are considered.

## Options

| | |
|---|---|
| `--base REF` | branch from REF instead of HEAD |
| `--carry-dirty` / `--allow-dirty` | see above |
| `--deps auto\|install\|none` | see above |
| `--no-sandbox` | don't use bubblewrap |
| `--cmd CMD` | agent command (default `claude`); the system-prompt flag is only added for `claude` |
| `--print-prompt` | show the intro and exit |

| Environment | |
|---|---|
| `CLAUDE_WT_ROOT` | worktree and metadata root (default `$XDG_STATE_HOME/claude-wt`) |
| `CLAUDE_WT_VOLATILE` | overlay write root (default `${TMPDIR:-/tmp}/claude-wt-<uid>`) |

The agent sees `CLAUDE_WT_WORKTREE`, `CLAUDE_WT_BRANCH`, `CLAUDE_WT_MAIN` and `CLAUDE_WT_PROMPT_FILE`.

## Sandbox notes

The bubblewrap sandbox is `--dev-bind / /` plus the read-only main checkout and the overlays. Nothing else is
unshared: network, other directories, and your home directory behave as normal. Its job is to stop mistakes, not to
contain a hostile agent. Claude Code's own `/sandbox` also uses bubblewrap; nesting works.

If `bwrap` is missing or user namespaces are blocked (for example, Ubuntu's AppArmor restriction), claude-wt runs
without the sandbox and says so at launch.

## Development

```sh
uv sync
uv run ruff check src tests && uv run basedpyright && uv run pytest tests -q
```
