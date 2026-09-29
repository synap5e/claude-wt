# claude-wt

Launch Claude Code (or another coding agent) in a throwaway git worktree, and keep it there.

```sh
cd my-repo
claude-wt                      # new worktree from HEAD, branch wt/<timestamp>, launches `claude`
claude-wt fix-login -- --model opus   # name it; everything after -- goes to the agent
claude-wt resume fix-login     # relaunch later, same conversation
claude-wt land fix-login       # merge/squash/keep/discard menu
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
2. **Creates the worktree** at `$XDG_STATE_HOME/claude-wt/<repo>-<hash>/<slug>` on branch `wt/<slug>/work`, from `--base`
   (default `HEAD`).
3. **Sets up dependency directories** (see below).
4. **Launches the agent** in the worktree with:
   - an intro appended to Claude's system prompt: which worktree and branch it's on, the rules, and what happened to
     dependency dirs. It's also written to `$CLAUDE_WT_PROMPT_FILE` for other agents.
   - a `git` shim first on its `PATH` that refuses `switch`, branch-moving `checkout`, and `worktree add/move/remove`
     *before* git runs. Aliases are followed. `git checkout -- <file>` and `git checkout <rev> <paths>` still work.
   - if bubblewrap works: the main checkout mounted **read-only**, and the shared `.git` locked down so the agent
     can commit to its own branch and nothing else (see *Sandbox notes*).
5. **Hands back to you** when the agent exits (see *Landing the work*).

## Landing the work

The branch already lives in the repo's shared `.git`, so nothing needs copying. When the agent exits you get a menu,
and nothing touches the main checkout until you pick an option and confirm it:

```
claude-wt: wt/fix-login/work: 2 commit(s) ahead of main
  [m] merge into the main checkout's branch (fast-forward when possible)
  [s] squash into the main checkout as staged changes, for you to commit
  [l] log of the branch's commits
  [d] diff against where it started
  [r] resume the agent
  [k] keep it for later (default)
  [x] discard: remove the worktree and delete the branch
```

- The target is whatever branch the main checkout has checked out.
- **Merge** fast-forwards when it can; otherwise it makes a merge commit, and your hooks run. If a merge conflicts, it
  stops and leaves the conflict in the main checkout for you. The worktree is kept.
- **Squash** stages everything in the main checkout and doesn't commit, so you review and commit it yourself.
- After a successful merge or squash, it offers to remove the worktree and branch.
- **Resume** continues the same Claude conversation. claude-wt pins a session id per worktree and passes
  `--resume` once the transcript exists.
- `claude-wt land <slug>` reopens the menu later. `--no-land` (or a non-interactive stdin) skips it and just prints
  a summary.

**Carried changes.** With `--carry-dirty`, the branch starts with a WIP commit of your uncommitted changes, which are
still uncommitted in main. Merging into a checkout that already has those changes would fail. So if main's
uncommitted changes are *identical* to the WIP snapshot, claude-wt offers to clear them from main first; nothing is
lost, because the merge brings the same content back. If you've changed them since, it refuses.

Squash is usually what you want here: your in-progress changes come back uncommitted, with the agent's work
alongside them. Merge makes them a real commit on your branch.

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
| `-S`, `--require-sandbox` | refuse to start (before creating anything) unless the sandbox works |
| `--cmd CMD` | agent command (default `claude`); the system-prompt flag is only added for `claude` |
| `--print-prompt` | show the intro and exit |
| `--no-land` | skip the menu after the agent exits |

| Environment | |
|---|---|
| `CLAUDE_WT_ROOT` | worktree and metadata root (default `$XDG_STATE_HOME/claude-wt`) |
| `CLAUDE_WT_REQUIRE_SANDBOX=1` | make `-S` the default; an explicit `--no-sandbox` still wins |
| `CLAUDE_WT_VOLATILE` | overlay write root (default `${TMPDIR:-/tmp}/claude-wt-<uid>`) |

The agent sees `CLAUDE_WT_WORKTREE`, `CLAUDE_WT_BRANCH`, `CLAUDE_WT_MAIN` and `CLAUDE_WT_PROMPT_FILE`.

## Sandbox notes

The bubblewrap sandbox is `--dev-bind / /` plus the read-only main checkout, the dependency overlays, and a
layered view of the shared `.git`:

1. **A throwaway overlay over all of `.git`.** Git creates and removes lock files at the top of `.git` even for
   routine work (deleting a pseudo-ref locks `packed-refs`). A plain read-only `.git` therefore breaks `rebase`,
   which reports success but leaves the worktree mid-cherry-pick. Stray writes, including a planted hook, land in the
   overlay and are discarded.
2. **Read-only binds** over `refs`, `logs`, `HEAD`, `config`, `packed-refs`, `hooks`, `info` and `worktrees`, so
   forbidden writes fail with an error instead of disappearing into the overlay.
3. **Writable binds** for exactly what the agent's own branch needs: the object store, the worktree's git dir, its
   branch's ref and reflog directories, and `refs/remotes` so `fetch` works.

This is why branches are `wt/<slug>/work`. Git needs write access to a ref's *directory* to lock and replace the
ref, so each branch gets its own directory.

The result: the kernel refuses moving any other branch, whether through porcelain or plumbing (`update-ref`,
`branch -f`, a rewritten `packed-refs`). It also refuses creating branches, changing the main checkout's HEAD, and
editing git config or hooks. `git stash` is unavailable, because `refs/stash` is shared with the main checkout. `git fetch` works, but tags are skipped
(the agent's git gets `--no-tags` for every remote, because `refs/tags` is read-only): existing tags stay readable,
and your own next fetch picks up new ones. `fetch --prune` can't remove remote-tracking refs that live in
`packed-refs`.

The network isn't sandboxed, so `git push` reaches the real remote. Git's
background maintenance is turned off inside, since it would try to pack refs. Repos using the reftable ref storage
can't be split per branch, so they count as "no sandbox".

Nothing else is unshared: network, other directories, and your home directory behave as normal. Its job is to stop mistakes, not to
contain a hostile agent. Claude Code's own `/sandbox` also uses bubblewrap; nesting works.

If `bwrap` is missing or user namespaces are blocked (for example, Ubuntu's AppArmor restriction), claude-wt runs
without the sandbox and says so at launch, unless `-S` is set, in which case it refuses to start.

## Development

```sh
uv sync
uv run ruff check src tests && uv run basedpyright && uv run pytest tests -q
```
