"""Observe and clean only the process tree owned by one test-lane command."""

from __future__ import annotations

import time
from dataclasses import dataclass

import psutil


@dataclass(frozen=True)
class ProcessTreeSample:
    rss_bytes: int
    child_count: int


def sample_tree(process: psutil.Process) -> ProcessTreeSample:
    processes = [process]
    try:
        processes.extend(process.children(recursive=True))
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

    rss = 0
    alive_children = 0
    for candidate in processes:
        try:
            if candidate.pid != process.pid:
                alive_children += 1
            rss += candidate.memory_info().rss
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return ProcessTreeSample(rss_bytes=rss, child_count=alive_children)


def terminate_tree(process: psutil.Process, grace_seconds: float = 3.0) -> None:
    try:
        children = process.children(recursive=True)
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        children = []

    for candidate in reversed(children):
        try:
            candidate.terminate()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    _, alive = psutil.wait_procs(children, timeout=grace_seconds)
    for candidate in alive:
        try:
            candidate.kill()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass

    try:
        process.terminate()
        process.wait(timeout=grace_seconds)
    except psutil.TimeoutExpired:
        process.kill()
    except (psutil.NoSuchProcess, psutil.AccessDenied):
        pass

    time.sleep(0.05)
