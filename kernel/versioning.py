"""Git-driven version history (the "disk / recovery partition").

Every accepted app version is a commit in a bare repo at `state/versions.git`. The
agent NEVER runs git — only the kernel does. The agent merely edits files in a
staging clone; the kernel turns that into an immutable, revertible version.
"""

from __future__ import annotations

import os
import pathlib
import shutil
import stat
import subprocess
import tempfile
import uuid
import zipfile

from kernel import registry, state_store
from kernel.state_store import APP_SEED, STATE_DIR, TASKS_DIR, VERSIONS_GIT, ensure_dirs
from kernel.util import CHILD_CREATIONFLAGS

_IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc", ".git", "node_modules")
_GITCONFIG = STATE_DIR / "gitconfig"
# An always-empty directory we point git's core.hooksPath at, so hooks in an
# agent-controlled staging tree (e.g. a planted .git/hooks/pre-commit) NEVER run when the
# kernel commits it — that would be a ring-3 → ring-0 escape into the kernel process.
_NOHOOKS_DIR = STATE_DIR / "git-no-hooks"


def _ensure_gitconfig() -> None:
    """A kernel-owned git config (identity + safe.directory) so we never touch the
    user's global config and never trip 'dubious ownership' on non-NTFS drives."""
    if not _GITCONFIG.exists():
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        _GITCONFIG.write_text(
            "[safe]\n\tdirectory = *\n"
            "[user]\n\temail = kernel@quine.dev\n\tname = Quine Kernel\n"
            "[init]\n\tdefaultBranch = main\n",
            encoding="utf-8",
        )
    _NOHOOKS_DIR.mkdir(parents=True, exist_ok=True)


def _env() -> dict[str, str]:
    _ensure_gitconfig()
    # Secrets stripped: a git operation can execute repo-supplied code (hooks, filter/
    # textconv drivers), and the staging tree is agent-controlled — so it must never run
    # with provider keys in its environment.
    env = state_store.stripped_env()
    env["GIT_CONFIG_GLOBAL"] = str(_GITCONFIG)
    env["GIT_CONFIG_NOSYSTEM"] = "1"
    return env


def _run(args: list[str], cwd: pathlib.Path | None = None) -> subprocess.CompletedProcess:
    """Run git hardened (no hooks, no secrets) and return the raw result — callers that
    need the returncode/stderr (conflict detection, existence probes) use this directly."""
    return subprocess.run(
        # `-c core.hooksPath=…` (command line ⇒ highest precedence, so a staging-local
        # .git/config cannot override it) disables any repository hooks for every call.
        ["git", "-c", f"core.hooksPath={_NOHOOKS_DIR}", *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=_env(),
        creationflags=CHILD_CREATIONFLAGS,
    )


def _git(args: list[str], cwd: pathlib.Path | None = None, check: bool = True) -> str:
    res = _run(args, cwd=cwd)
    if check and res.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {res.stderr.strip()}")
    return res.stdout


def ensure_repo() -> None:
    ensure_dirs()
    # A valid bare repo has a HEAD file; if it's missing (fresh, or a partial wipe left
    # a stray objects/ dir on Windows), (re)initialize so boot can always proceed.
    if not (VERSIONS_GIT / "HEAD").exists():
        VERSIONS_GIT.mkdir(parents=True, exist_ok=True)
        _git(["init", "--bare", "-b", "main", str(VERSIONS_GIT)])


def has_history() -> bool:
    res = _run(["--git-dir", str(VERSIONS_GIT), "rev-parse", "--verify", "--quiet", "main"])
    return res.returncode == 0


def head() -> str:
    return _git(["--git-dir", str(VERSIONS_GIT), "rev-parse", "main"]).strip()


def seed_initial() -> str:
    """If the repo has no history, import the `app/` seed as version 1. Returns sha."""
    ensure_repo()
    if has_history():
        return head()
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="aimprove-seed-"))
    try:
        shutil.copytree(APP_SEED, tmp, dirs_exist_ok=True, ignore=_IGNORE)
        _git(["init", "-b", "main"], cwd=tmp)
        _git(["add", "-A"], cwd=tmp)
        _git(["commit", "-m", "seed: initial app v1"], cwd=tmp)
        _git(["remote", "add", "origin", str(VERSIONS_GIT)], cwd=tmp)
        _git(["push", "origin", "main"], cwd=tmp)
        sha = _git(["rev-parse", "HEAD"], cwd=tmp).strip()
        _git(["push", "origin", f"HEAD:refs/heads/v_{sha[:8]}"], cwd=tmp)
        registry.record_commit(sha, parent=None, message="seed: initial app v1", origin="seed")
        return sha
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _force_rmtree(path: pathlib.Path) -> None:
    """Remove a tree even when it holds read-only files. A local `git clone` links the source
    repo's packed objects in read-only, and on Windows a read-only file cannot be unlinked — so
    a plain rmtree(ignore_errors=True) leaves the dir behind and the next clone into that path
    fails with 'already exists and is not an empty directory'. Clear the bit and retry per entry."""
    def _onexc(func, p, _exc):  # py3.12 rmtree callback: chmod +w, then retry the unlink/rmdir
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            pass

    shutil.rmtree(path, onexc=_onexc)


def create_staging(task_id: str, base: str | None = None) -> pathlib.Path:
    """A fresh working clone of the history for the agent to edit freely.

    Defaults to the active line (`main`, the current good version). Pass `base` (a full sha,
    already resolved by the caller) to stage from a SPECIFIC version instead — this is how
    "continue from a commit" re-bases the agent's edits onto that exact tree. Committing on a
    detached base produces a child of it (a branch off the active line if `base` isn't head);
    `commit_staging` pushes to its own `v_*` branch and `main` only advances on health-gated
    promotion, so branching here is as safe as rollback-then-edit."""
    staging = TASKS_DIR / task_id / "staging"
    if staging.exists():
        _force_rmtree(staging)
    staging.parent.mkdir(parents=True, exist_ok=True)
    _git(["clone", "--quiet", str(VERSIONS_GIT), str(staging)])
    if base:
        # Detached checkout of the chosen version; the next commit's parent is exactly `base`.
        _git(["checkout", "--quiet", "--detach", base], cwd=staging)
    return staging


def commit_staging(
    staging: pathlib.Path,
    message: str,
    *,
    task_id: str | None = None,
    prompt: str | None = None,
    origin: str = "self-mod",
    reverts: str | None = None,
    reapplies: str | None = None,
) -> str | None:
    """Commit + push staging as a new version. Returns sha, or None if no changes.

    Every version is registered in the version registry at birth (seq number +
    provenance), so the metadata index can never miss a commit this module made."""
    _git(["add", "-A"], cwd=staging)
    if not _git(["status", "--porcelain"], cwd=staging).strip():
        return None
    _git(["commit", "-m", message], cwd=staging)
    sha = _git(["rev-parse", "HEAD"], cwd=staging).strip()
    parent_res = _run(["rev-parse", "--verify", "--quiet", "HEAD^"], cwd=staging)
    parent = parent_res.stdout.strip() if parent_res.returncode == 0 else None
    # Store as its own version branch — NOT main. main advances only on promotion
    # (see promote()), so a broken/rolled-back version never becomes the base for
    # the next edit.
    _git(["push", "origin", f"HEAD:refs/heads/v_{sha[:8]}"], cwd=staging)
    registry.record_commit(sha, parent=parent or None, message=message, task_id=task_id,
                           prompt=prompt, origin=origin, reverts=reverts, reapplies=reapplies)
    return sha


def promote(sha: str) -> None:
    """Advance the active line (`main`) to a version that passed health checks.
    New staging clones `main`, so it always starts from the current GOOD version."""
    _git(["--git-dir", str(VERSIONS_GIT), "update-ref", "refs/heads/main", sha])


# ── named lines (experiment/A-B branches beside `main`) ────────────────────────────────
# A line is just a git ref (refs/heads/line_<name>) over the SAME version commits — every
# commit is still pinned by its v_* branch, so lines add zero storage and survive anything
# that survives versions.git. Human metadata (description, provenance) lives in
# state/lines.json (state_store); losing that file loses only cosmetics.
def _line_ref(name: str) -> str:
    return f"refs/heads/line_{name}"


def line_tip(name: str) -> str | None:
    """The line's current tip sha, or None if the ref doesn't exist."""
    res = _run(["--git-dir", str(VERSIONS_GIT), "rev-parse", "--verify", "--quiet",
                _line_ref(name)])
    return res.stdout.strip() or None if res.returncode == 0 else None


def set_line(name: str, sha: str) -> None:
    """Create or advance a line ref (creation and promotion are the same git op)."""
    _git(["--git-dir", str(VERSIONS_GIT), "update-ref", _line_ref(name), sha])


def delete_line(name: str) -> None:
    _git(["--git-dir", str(VERSIONS_GIT), "update-ref", "-d", _line_ref(name)])


def list_line_refs() -> dict[str, str]:
    """name → tip sha for every line_* ref."""
    out = _git(["--git-dir", str(VERSIONS_GIT), "for-each-ref",
                "--format=%(refname:short)%00%(objectname)", "refs/heads/line_*"])
    lines: dict[str, str] = {}
    for row in out.strip().splitlines():
        ref, _, sha = row.partition("\x00")
        if ref.startswith("line_") and sha:
            lines[ref[len("line_"):]] = sha
    return lines


def parent_of(sha: str) -> str | None:
    """A version's parent commit (None for the root/seed)."""
    res = _run(["--git-dir", str(VERSIONS_GIT), "rev-parse", "--verify", "--quiet", f"{sha}^"])
    return res.stdout.strip() or None if res.returncode == 0 else None


def resolve_version(ref: str) -> str | None:
    """Resolve any human reference — full/short sha, `v<seq>`, or a label — to a full sha.
    Registry identifiers first, then anything git itself can resolve."""
    ref = (ref or "").strip()
    if not ref:
        return None
    hit = registry.lookup(ref)
    if hit:
        return hit
    res = _run(["--git-dir", str(VERSIONS_GIT), "rev-parse", "--verify", "--quiet",
                f"{ref}^{{commit}}"])
    return res.stdout.strip() or None if res.returncode == 0 else None


def main_ancestors() -> set[str]:
    """Every commit on the active line — ONE `rev-list` call, so membership checks for a
    whole listing are O(1) each (never per-version `merge-base --is-ancestor`)."""
    if not has_history():
        return set()
    return set(_git(["--git-dir", str(VERSIONS_GIT), "rev-list", "main"]).split())


def commits_only_in(tip: str, not_in: str) -> list[str]:
    """Commits reachable from `tip` but not from `not_in` (`git rev-list not_in..tip`),
    newest first. This is the abandoned/restored range when main moves between the two."""
    out = _git(["--git-dir", str(VERSIONS_GIT), "rev-list", f"{not_in}..{tip}"])
    return out.split()


def _log_all_versions(oldest_first: bool = False) -> list[dict]:
    """Every version commit with parent/author/date/subject in one batched `git log`
    (every version is pinned by its own v_* branch, so the union of those refs IS the
    full version set)."""
    args = ["--git-dir", str(VERSIONS_GIT), "log", "--branches=v_*", "--date-order",
            "--format=%H%x00%P%x00%an%x00%aI%x00%s"]
    if oldest_first:
        args.append("--reverse")
    commits: list[dict] = []
    for line in _git(args).strip().splitlines():
        parts = line.split("\x00")
        if len(parts) < 5:
            continue
        sha, parents, author, date, subject = parts[0], parts[1], parts[2], parts[3], parts[4]
        commits.append({
            "sha": sha,
            "parent": parents.split()[0] if parents.strip() else None,
            "author": author, "date": date, "message": subject,
        })
    return commits


def list_versions(limit: int = 50, offset: int = 0) -> list[dict]:
    """All recorded versions enriched with registry metadata (seq number, label, status,
    provenance, revert edges) and lineage (`on_main` / `is_head`), newest first."""
    if not has_history():
        return []
    commits = _log_all_versions()
    meta = registry.all_versions()
    if any(c["sha"] not in meta for c in commits):
        reconcile_registry()  # lazy self-heal: the index lost entries git still has
        meta = registry.all_versions()
    main_set = main_ancestors()
    head_sha = head()
    rows: list[dict] = []
    for c in commits:
        m = meta.get(c["sha"], {})
        rows.append({
            "sha": c["sha"], "short": c["sha"][:8], "author": c["author"],
            "date": c["date"], "message": c["message"],
            "seq": m.get("seq"), "label": m.get("label"), "parent": c["parent"],
            "on_main": c["sha"] in main_set, "is_head": c["sha"] == head_sha,
            "status": m.get("status"), "task": m.get("task"), "origin": m.get("origin"),
            "reverts": m.get("reverts"), "reapplies": m.get("reapplies"),
            "reverted_by": m.get("reverted_by"), "reapplied_by": m.get("reapplied_by"),
            "verification": m.get("verification"),
        })
    # seq desc (assignment order = true history order); rows the registry somehow still
    # doesn't know keep git's date-order position at the end.
    rows.sort(key=lambda r: -(r["seq"] or 0))
    offset = max(0, offset)
    return rows[offset:offset + max(1, limit)]


def reconcile_registry() -> dict:
    """Repair the registry index from git facts (boot-time migration + lazy self-heal):
    backfill entries git has that the index lost, prune entries git no longer has, and
    fix status drift. Idempotent and cheap (two git calls)."""
    if not has_history():
        return {"added": 0, "pruned": 0, "updated": 0}
    commits = _log_all_versions(oldest_first=True)  # oldest first → stable seq assignment
    pending = {str(p["sha"]) for p in state_store.read_pending() if p.get("sha")}
    return registry.reconcile(commits, main_ancestors(), pending)


def count_versions() -> int:
    """Total recorded versions — cheap ref count, no truncation cap."""
    if not has_history():
        return 0
    out = _git(["--git-dir", str(VERSIONS_GIT), "for-each-ref",
                "--format=%(objectname)", "refs/heads/v_*"])
    return len(set(out.split()))


def version_message(sha: str) -> str:
    return _git(["--git-dir", str(VERSIONS_GIT), "log", "-1", "--format=%s", sha]).strip()


def diff(sha: str) -> str:
    """The unified diff a version introduced vs its parent (root commit → all files).
    Lets you review exactly what a self-modification changed."""
    # -M detects renames; --stat=1000,1000 stops git from clipping long filenames to the
    # default ~80-col terminal width (so the review UI gets full paths).
    return _git([
        "--git-dir", str(VERSIONS_GIT), "show", "--no-color", "-M",
        "--stat=1000,1000", "-p", sha,
    ])


def diff_range(a: str, b: str) -> str:
    """Unified diff between any two versions' trees (what changed going a → b) —
    e.g. reviewing the net effect of a rollback, or active vs any historical version."""
    return _git([
        "--git-dir", str(VERSIONS_GIT), "diff", "--no-color", "-M",
        "--stat=1000,1000", "-p", a, b,
    ])


def changed_paths(sha: str) -> list[str]:
    """Repo-relative paths a version's commit touched vs its parent (the agent-eval
    gate's diff-scoping input). A root commit yields [] — nothing to compare against."""
    out = _git(["--git-dir", str(VERSIONS_GIT), "diff-tree",
                "--no-commit-id", "--name-only", "-r", sha])
    return [ln.strip() for ln in out.splitlines() if ln.strip()]


_CONFLICT_CODES = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}


def _conflicted_files(staging: pathlib.Path) -> list[str]:
    """Paths in a conflicted merge state (the unmerged codes of `status --porcelain`)."""
    out = _run(["status", "--porcelain"], cwd=staging).stdout
    return [ln[3:].strip().strip('"') for ln in out.splitlines()
            if len(ln) > 3 and ln[:2] in _CONFLICT_CODES]


def _apply_onto_staging(staging: pathlib.Path, op: str, sha: str) -> tuple[bool, str]:
    """Run `git revert`/`cherry-pick` --no-commit in a staging clone. On conflict the
    operation is aborted and the tree hard-reset, so staging is NEVER left half-applied —
    the caller gets (False, detail) and a clean tree. On success the changes are staged,
    ready for the normal validate → commit_staging path."""
    res = _run([op, "--no-commit", "--no-edit", sha], cwd=staging)
    if res.returncode == 0:
        return True, ""
    conflicted = _conflicted_files(staging)
    _run([op, "--abort"], cwd=staging)
    _run(["reset", "--hard"], cwd=staging)
    detail = res.stderr.strip().splitlines()[0] if res.stderr.strip() else f"git {op} failed"
    if conflicted:
        detail += " — conflicting files: " + ", ".join(sorted(conflicted)[:20])
    return False, detail


def revert_onto_staging(staging: pathlib.Path, sha: str) -> tuple[bool, str]:
    """Stage the inverse of one version's changes (undo it, keeping everything after)."""
    return _apply_onto_staging(staging, "revert", sha)


def cherry_pick_onto_staging(staging: pathlib.Path, sha: str) -> tuple[bool, str]:
    """Stage one (abandoned) version's changes onto the current line (re-apply it)."""
    return _apply_onto_staging(staging, "cherry-pick", sha)


def deploy(sha: str, slot_dir: pathlib.Path) -> None:
    """Materialize a version's tree into a slot dir (no .git — just the files)."""
    slot_dir = pathlib.Path(slot_dir)
    if slot_dir.exists():
        shutil.rmtree(slot_dir, ignore_errors=True)
    slot_dir.mkdir(parents=True, exist_ok=True)
    # Unique per call: several slots share this parent dir (a/b plus every preview), so two
    # concurrent deploys of the SAME version — e.g. a preview spinning up while main reboots —
    # would otherwise collide on one scratch archive and delete it out from under each other.
    archive = slot_dir.parent / f".deploy-{sha[:12]}-{os.getpid()}-{uuid.uuid4().hex[:8]}.zip"
    try:
        _git(["--git-dir", str(VERSIONS_GIT), "archive", "--format=zip", "-o", str(archive), sha])
        with zipfile.ZipFile(archive) as zf:
            zf.extractall(slot_dir)
    finally:
        archive.unlink(missing_ok=True)
