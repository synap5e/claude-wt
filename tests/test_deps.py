from __future__ import annotations

from pathlib import Path

from claude_wt.deps import DepDir, Mode, Step, Strategy, detect, plan

from .conftest import sh


def by_rel(steps: list[Step], rel: str) -> Step:
    return next(s for s in steps if s.dep.rel == Path(rel))


def make_node(root: Path, rel: str, lockfile: str | None) -> DepDir:
    d = root / rel
    d.mkdir(parents=True, exist_ok=True)
    (d / "package.json").write_text("{}")
    if lockfile:
        (d / lockfile).write_text("")
    return DepDir(Path(rel) / "node_modules", "node_modules")


def test_venv_always_installs_with_uv(tmp_path: Path) -> None:
    (tmp_path / "uv.lock").write_text("")
    steps = plan([DepDir(Path(".venv"), "venv")], tmp_path, Mode.AUTO, sandboxed=True)
    assert steps[0].strategy is Strategy.INSTALL
    assert steps[0].command == ["uv", "sync", "--frozen"]


def test_venv_without_lock_is_skipped(tmp_path: Path) -> None:
    steps = plan([DepDir(Path(".venv"), "venv")], tmp_path, Mode.AUTO, sandboxed=False)
    assert steps[0].strategy is Strategy.SKIP


def test_node_auto_prefers_overlay_then_reflink(tmp_path: Path) -> None:
    dep = make_node(tmp_path, ".", "pnpm-lock.yaml")
    assert plan([dep], tmp_path, Mode.AUTO, sandboxed=True)[0].strategy is Strategy.OVERLAY
    assert plan([dep], tmp_path, Mode.AUTO, sandboxed=False)[0].strategy is Strategy.REFLINK


def test_node_install_picks_lockfile_and_workspace_covers_nested(tmp_path: Path) -> None:
    root = make_node(tmp_path, ".", "pnpm-lock.yaml")
    nested = make_node(tmp_path, "packages/app", None)
    steps = plan([nested, root], tmp_path, Mode.INSTALL, sandboxed=True)
    assert by_rel(steps, "node_modules").command == ["pnpm", "install", "--frozen-lockfile"]
    assert by_rel(steps, "packages/app/node_modules").strategy is Strategy.SKIP


def test_none_skips_everything(tmp_path: Path) -> None:
    dep = make_node(tmp_path, ".", "package-lock.json")
    assert plan([dep], tmp_path, Mode.NONE, sandboxed=True)[0].strategy is Strategy.SKIP


def test_detect_finds_tracked_projects_only(repo: Path) -> None:
    (repo / "package.json").write_text("{}")
    (repo / "node_modules").mkdir()
    sh("git", "add", "package.json", cwd=repo)
    (repo / "untracked").mkdir()
    (repo / "untracked" / "package.json").write_text("{}")
    (repo / "untracked" / "node_modules").mkdir()
    assert detect(repo) == [DepDir(Path("node_modules"), "node_modules")]
