#!/usr/bin/env python
"""Run one serial Quine test lane under a process-tree resource budget."""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import subprocess
import sys
import time
from dataclasses import asdict

import psutil

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.support.process_guard import sample_tree, terminate_tree  # noqa: E402
from tests.support.resources import BUDGETS, LaneBudget  # noqa: E402

def _default_lane_paths(lane: str) -> list[str]:
    """Return serial unit/contract directories from each available test suite."""
    categories = ("unit",) if lane == "quick" else ("unit", "contract")
    tests_root = ROOT / "tests"
    paths = []
    suites = sorted(
        (path for path in tests_root.iterdir() if path.is_dir()),
        key=lambda path: (path.name != "local", path.name),
    )
    for suite in suites:
        for category in categories:
            candidate = suite / category
            if candidate.is_dir():
                paths.append(candidate.relative_to(ROOT).as_posix())
    return paths


def _target(value: str) -> str:
    normalized = value.replace("\\", "/")
    path = pathlib.PurePosixPath(normalized.split("::", 1)[0])
    if path.is_absolute() or ".." in path.parts or not normalized.startswith("tests/"):
        raise argparse.ArgumentTypeError("targets must stay under tests/")
    return normalized


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("lane", choices=tuple(BUDGETS))
    parser.add_argument("targets", nargs="*", type=_target)
    parser.add_argument("--max-rss-mb", type=int)
    parser.add_argument("--max-children", type=int)
    parser.add_argument("--timeout-seconds", type=int)
    return parser.parse_args()


def _budget(args: argparse.Namespace) -> LaneBudget:
    default = BUDGETS[args.lane]
    return LaneBudget(
        max_rss_mb=args.max_rss_mb or default.max_rss_mb,
        max_children=(
            args.max_children if args.max_children is not None else default.max_children
        ),
        timeout_seconds=args.timeout_seconds or default.timeout_seconds,
    )


def _pytest_targets(args: argparse.Namespace) -> list[str]:
    if args.lane in {"quick", "contract"} and not args.targets:
        targets = _default_lane_paths(args.lane)
        if not targets:
            raise SystemExit(f"{args.lane} lane has no test directories in this checkout")
        return targets
    if not args.targets:
        raise SystemExit(f"{args.lane} lane requires at least one tests/ target")

    allowed_segments = {
        "quick": ("/unit/",),
        "contract": ("/unit/", "/contract/"),
        "integration": ("/integration/",),
        "system": ("/system/",),
    }[args.lane]
    for target in args.targets:
        normalized = "/" + target.replace("\\", "/")
        if not any(
            segment in normalized or normalized.endswith(segment.rstrip("/"))
            for segment in allowed_segments
        ):
            allowed = ", ".join(allowed_segments)
            raise SystemExit(f"{args.lane} target must live under {allowed}: {target}")
    return args.targets


def _process_identity(process: psutil.Process) -> tuple[int, float]:
    return process.pid, process.create_time()


def _still_alive(identity: tuple[int, float]) -> psutil.Process | None:
    pid, created = identity
    try:
        process = psutil.Process(pid)
        if abs(process.create_time() - created) < 0.001 and process.is_running():
            return process
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass
    return None


def main() -> int:
    args = _arguments()
    budget = _budget(args)
    targets = _pytest_targets(args)
    results_dir = ROOT / "test-results"
    results_dir.mkdir(parents=True, exist_ok=True)
    log_path = results_dir / f"{args.lane}.log"
    summary_path = results_dir / f"{args.lane}.json"

    command = [
        sys.executable,
        "-m",
        "pytest",
        "-p",
        "no:xdist",
        "--confcutdir=tests",
        *targets,
    ]
    environment = os.environ.copy()
    environment["NO_COLOR"] = "1"
    environment["PY_COLORS"] = "0"
    environment.pop("FORCE_COLOR", None)
    environment.pop("PYTEST_XDIST_WORKER", None)
    environment.pop("PYTEST_XDIST_WORKER_COUNT", None)

    started = time.monotonic()
    peak_rss = 0
    peak_children = 0
    seen_children: set[tuple[int, float]] = set()
    observed_children: dict[tuple[int, float], dict[str, object]] = {}
    violation = ""
    creationflags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0

    with log_path.open("wb") as log:
        process = psutil.Popen(
            command,
            cwd=str(ROOT),
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=creationflags,
        )
        try:
            while process.poll() is None:
                elapsed = time.monotonic() - started
                sample = sample_tree(process)
                peak_rss = max(peak_rss, sample.rss_bytes)
                peak_children = max(peak_children, sample.child_count)
                try:
                    for child in process.children(recursive=True):
                        identity = _process_identity(child)
                        seen_children.add(identity)
                        if identity not in observed_children:
                            try:
                                observed_children[identity] = {
                                    "pid": child.pid,
                                    "name": child.name(),
                                    "command": child.cmdline(),
                                }
                            except (psutil.NoSuchProcess, psutil.AccessDenied):
                                observed_children[identity] = {"pid": child.pid}
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass

                if sample.rss_bytes > budget.max_rss_mb * 1024 * 1024:
                    violation = (
                        f"process-tree RSS exceeded {budget.max_rss_mb} MB "
                        f"({sample.rss_bytes / 1024 / 1024:.1f} MB)"
                    )
                    break
                if sample.child_count > budget.max_children:
                    violation = (
                        f"child-process budget exceeded {budget.max_children} "
                        f"({sample.child_count} observed)"
                    )
                    break
                if elapsed > budget.timeout_seconds:
                    violation = f"timeout exceeded {budget.timeout_seconds} seconds"
                    break
                time.sleep(0.1)
        except KeyboardInterrupt:
            violation = "interrupted"
        finally:
            if violation and process.poll() is None:
                terminate_tree(process)

        returncode = process.wait() if process.poll() is None else int(process.returncode or 0)

    leaked = []
    for identity in seen_children:
        candidate = _still_alive(identity)
        if candidate is not None:
            leaked.append(candidate)
    for candidate in leaked:
        terminate_tree(candidate)
    if leaked and not violation:
        violation = f"{len(leaked)} owned child process(es) remained after pytest exited"

    elapsed = time.monotonic() - started
    summary = {
        "lane": args.lane,
        "targets": targets,
        "command": command,
        "budget": asdict(budget),
        "elapsedSeconds": round(elapsed, 3),
        "peakRssMb": round(peak_rss / 1024 / 1024, 3),
        "peakChildren": peak_children,
        "observedChildren": list(observed_children.values()),
        "pytestExitCode": returncode,
        "violation": violation or None,
        "log": str(log_path),
    }
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))

    if violation:
        return 3
    return returncode


if __name__ == "__main__":
    raise SystemExit(main())
