"""How many documents may render at once, and what happens to the rest.

Measured 2026-10-19 on the deployment's own limits (2 CPU, 4 GiB): background
work went to anyio's default thread limiter, which is 40, and nothing else bounded
it. Eight concurrent demo builds each took 6.7x as long as one — while total
throughput rose about 20%, from 0.23 to 0.28 jobs/s. The CPU was already saturated
at two. Everything past that was queueing inside the kernel scheduler, where it is
invisible and shared out equally, so every user waited longer and nobody got a
document sooner.

Memory was the harder wall: a concurrent full-thesis render costs ~674 MiB, so
4 GiB is exhausted at about six, against an admission bound of forty. One replica,
so an OOM kill takes every in-flight build with it.

So: a semaphore sized to the CPUs, a BOUNDED queue behind it, and a 429 when the
queue is full. A caller who waits is told they are waiting; a caller who cannot be
served is told that immediately rather than by a timeout.
"""
from __future__ import annotations

import os
import threading


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if not raw:
        return default
    try:
        v = int(raw)
    except ValueError:
        raise RuntimeError(f"{name} must be an integer, got {raw!r}")
    if v < 1:
        raise RuntimeError(f"{name} must be at least 1, got {v}")
    return v


# Two renders on two cores. Raising this does not raise throughput — it was
# measured not to — it only spreads the same throughput over more waiting users.
MAX_RENDERS = _env_int("ABCPRINT_MAX_RENDERS", 2)
# How many may wait. Kept small on purpose: a queue deeper than this is a promise
# the service cannot keep inside a submission deadline, and saying so now is
# better than saying it in four minutes. Also bounds the threads held by waiters.
MAX_QUEUED = _env_int("ABCPRINT_MAX_QUEUED", 8)


class Full(RuntimeError):
    """Raised at admission when the queue is already at its bound."""


class Gate:
    """Admission control for renders.

    `reserve()` is called on the REQUEST path and either takes a slot or refuses,
    so the refusal reaches the caller as a 429 rather than as a job that sits
    queued forever. `run()` is called on the worker and blocks until a render slot
    frees up.
    """

    def __init__(self, max_renders: int = MAX_RENDERS, max_queued: int = MAX_QUEUED):
        self.max_renders = max_renders
        self.max_queued = max_queued
        self._sem = threading.BoundedSemaphore(max_renders)
        self._lock = threading.Lock()
        self._admitted = 0          # reserved, not yet finished
        self._rendering = 0         # holding a render slot right now

    # --- request path --------------------------------------------------------
    def reserve(self) -> None:
        with self._lock:
            if self._admitted >= self.max_renders + self.max_queued:
                raise Full(
                    f"{self._admitted} builds are already admitted "
                    f"({self.max_renders} rendering, {self.max_queued} may wait). "
                    f"Retry shortly — this deployment renders {self.max_renders} at "
                    f"a time because that is how many CPUs it has, and admitting "
                    f"more makes every build slower without finishing any sooner.")
            self._admitted += 1

    def release_reservation(self) -> None:
        """Give a reservation back when a job is refused before it ever runs."""
        with self._lock:
            self._admitted = max(0, self._admitted - 1)

    # --- worker path ---------------------------------------------------------
    def __enter__(self):
        self._sem.acquire()
        with self._lock:
            self._rendering += 1
        return self

    def __exit__(self, *exc) -> None:
        with self._lock:
            self._rendering = max(0, self._rendering - 1)
            self._admitted = max(0, self._admitted - 1)
        self._sem.release()
        return None

    # --- reporting -----------------------------------------------------------
    def state(self) -> dict:
        with self._lock:
            return {"max_renders": self.max_renders, "max_queued": self.max_queued,
                    "rendering": self._rendering,
                    "waiting": max(0, self._admitted - self._rendering),
                    "admitted": self._admitted}


GATE = Gate()
