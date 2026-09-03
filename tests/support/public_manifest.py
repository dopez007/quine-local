"""Verify a generated public tree against its manifest, Git index, and HEAD."""

from __future__ import annotations

import hashlib
import io
import json
import pathlib
import subprocess
import sys
import tarfile

MANIFEST_NAME = "PUBLIC-MANIFEST.json"
LICENSE_ID = "PolyForm-Shield-1.0.0"


def _sha256(path: pathlib.Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_public_manifest(
    root: pathlib.Path,
    tracked_paths: set[str] | None = None,
    committed_files: dict[str, bytes] | None = None,
) -> list[str]:
    manifest_path = root / MANIFEST_NAME
    if not manifest_path.is_file():
        return [f"missing {MANIFEST_NAME}"]

    try:
        payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"invalid {MANIFEST_NAME}: {exc}"]

    entries = payload.get("files")
    if not isinstance(entries, list):
        return [f"invalid {MANIFEST_NAME}: files must be a list"]

    errors = []
    paths = [entry.get("path") for entry in entries if isinstance(entry, dict)]
    if len(paths) != len(entries) or not all(isinstance(path, str) for path in paths):
        return [f"invalid {MANIFEST_NAME}: every entry needs a string path"]
    if payload.get("license") != LICENSE_ID:
        errors.append(f"license must be {LICENSE_ID}")
    if payload.get("fileCount") != len(entries):
        errors.append("fileCount does not match files")
    if paths != sorted(set(paths)):
        errors.append("manifest paths must be unique and sorted")
    if MANIFEST_NAME in paths:
        errors.append(f"{MANIFEST_NAME} must not list itself")

    for entry in entries:
        relative = entry["path"]
        path = root / pathlib.PurePosixPath(relative)
        if not path.is_file():
            errors.append(f"missing file: {relative}")
            continue
        if _sha256(path) != entry.get("sha256"):
            errors.append(f"hash mismatch: {relative}")

    expected = set(paths) | {MANIFEST_NAME}
    if tracked_paths is not None:
        missing = sorted(expected - tracked_paths)
        unexpected = sorted(tracked_paths - expected)
        if missing:
            errors.append(f"manifest files not tracked: {missing}")
        if unexpected:
            errors.append(f"tracked files not in manifest: {unexpected}")

    if committed_files is not None:
        committed_paths = set(committed_files)
        missing = sorted(expected - committed_paths)
        unexpected = sorted(committed_paths - expected)
        if missing:
            errors.append(f"manifest files missing from HEAD: {missing}")
        if unexpected:
            errors.append(f"HEAD files not in manifest: {unexpected}")
        if committed_files.get(MANIFEST_NAME) != manifest_path.read_bytes():
            errors.append(f"checked-out {MANIFEST_NAME} differs from HEAD")
        for entry in entries:
            relative = entry["path"]
            committed = committed_files.get(relative)
            if committed is not None and hashlib.sha256(committed).hexdigest() != entry.get("sha256"):
                errors.append(f"committed hash mismatch: {relative}")

    return errors


def _decode_git_paths(payload: bytes) -> set[str]:
    return {
        raw.decode("utf-8", errors="surrogateescape")
        for raw in payload.split(b"\0")
        if raw
    }


def _git_tracked_paths(root: pathlib.Path) -> set[str]:
    result = subprocess.run(
        ["git", "-c", "core.quotePath=false", "ls-files", "-z"],
        cwd=root,
        capture_output=True,
    )
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(message or "git ls-files failed")
    return _decode_git_paths(result.stdout)


def _git_head_files(root: pathlib.Path) -> dict[str, bytes]:
    result = subprocess.run(
        ["git", "archive", "--format=tar", "HEAD"],
        cwd=root,
        capture_output=True,
    )
    if result.returncode != 0:
        message = result.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(message or "git archive HEAD failed")

    files = {}
    with tarfile.open(fileobj=io.BytesIO(result.stdout), mode="r:") as archive:
        for member in archive.getmembers():
            if not member.isfile():
                continue
            handle = archive.extractfile(member)
            if handle is None:
                raise RuntimeError(f"could not read HEAD file: {member.name}")
            files[pathlib.PurePosixPath(member.name).as_posix()] = handle.read()
    return files


def main() -> int:
    root = pathlib.Path(__file__).resolve().parents[2]
    try:
        tracked_paths = _git_tracked_paths(root)
        committed_files = _git_head_files(root)
    except RuntimeError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    errors = verify_public_manifest(root, tracked_paths, committed_files)
    if errors:
        print("ERROR: public manifest verification failed:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(
        f"Verified {len(tracked_paths)} tracked public files and HEAD blobs against "
        f"{MANIFEST_NAME}."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
