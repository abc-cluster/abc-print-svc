"""In-process job store.

The build is multi-pass and takes tens of seconds, so `/compile` cannot be a
synchronous request: a Logseq plugin pushing a chapter should get an id back
immediately and poll. This store is deliberately in-process and bounded — it is
the smallest thing that makes the async contract real. A deployment serving many
students needs a shared store and a retention sweep, which is a separate piece
of work and is flagged rather than faked here.
"""
from __future__ import annotations

import os
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum


class State(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass
class Job:
    id: str
    state: State = State.QUEUED
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    options: dict = field(default_factory=dict)
    artifacts: list[dict] = field(default_factory=list)
    manifest: dict = field(default_factory=dict)
    checks: dict = field(default_factory=dict)
    error: str | None = None
    # A document can be produced AND fail its checks; that is not job failure.
    compliant: bool | None = None
    # The token label that created this job. Jobs are visible only to their owner:
    # before this field existed, GET /jobs listed every caller's jobs and every
    # artifact was fetchable by id, so one caller could download another's
    # document. A shared secret alone would not have closed that.
    owner: str = "anonymous"
    workdir: str | None = None
    log_tail: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        d = asdict(self)
        d["state"] = self.state.value
        d.pop("workdir", None)          # server-side path, not the client's business
        d["duration_s"] = round((self.finished or time.time()) - (self.started or self.created), 1)
        return d


# How long a finished job's workspace is kept. Artefacts are served straight out
# of it, so it cannot be deleted at the end of the build — but nothing deleted it
# at all, and `rmtree` appeared only on error paths. Four full-thesis builds left
# 148 MB behind (measured 2026-10-19), and eviction past the store limit dropped
# the Job object holding the only record of the path, making the directory
# unreferenced and unreclaimable without a filesystem sweep.
TTL_SECONDS = int(os.environ.get("ABCPRINT_ARTIFACT_TTL_S", str(6 * 3600)))
SWEEP_SECONDS = int(os.environ.get("ABCPRINT_SWEEP_S", "300"))


def _reclaim(job: "Job") -> int:
    """Delete a job's workspace and forget the path. Returns bytes freed, best effort.

    Clearing `workdir` is the point as much as the deletion: _artifact_path
    answers 410 "job workspace has been reclaimed" when it is unset, so after this
    the 410 is true. It was previously a branch describing something that never
    happened.
    """
    path = job.workdir
    if not path:
        return 0
    job.workdir = None
    freed = 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    freed += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
        shutil.rmtree(path, ignore_errors=True)
    except OSError:
        pass
    return freed


class Store:
    def __init__(self, limit: int = 200):
        self._jobs: dict[str, Job] = {}
        self._order: list[str] = []
        self._limit = limit
        self._lock = threading.Lock()

    def create(self, options: dict, owner: str = "anonymous") -> Job:
        job = Job(id=uuid.uuid4().hex[:16], options=options, owner=owner)
        with self._lock:
            self._jobs[job.id] = job
            self._order.append(job.id)
            evicted = []
            while len(self._order) > self._limit:
                old = self._jobs.pop(self._order.pop(0), None)
                if old is not None:
                    evicted.append(old)
        # Outside the lock: rmtree can take a while and must not block submissions.
        # Evicting without this leaked the workspace permanently, because the Job
        # held the only record of where it was.
        for old in evicted:
            _reclaim(old)
        return job

    def get(self, job_id: str, owner: str | None = None) -> Job | None:
        """Fetch a job, optionally restricted to its owner.

        A job belonging to someone else returns None rather than raising, so the
        caller answers 404 and does not confirm that the id exists. Pass owner=None
        only from code that is not serving a request — the background runner, which
        has no principal because it IS the service.
        """
        job = self._jobs.get(job_id)
        if job is None:
            return None
        if owner is not None and job.owner != owner:
            return None
        return job

    def list(self, limit: int = 50, owner: str | None = None) -> list[Job]:
        """Most recent jobs, newest first; the owner's own unless owner is None.

        The limit applies AFTER the ownership filter, so a busy neighbour cannot
        push a caller's own jobs out of their view — which a pre-filter limit would
        have done silently, and which is exactly the kind of quiet wrong answer
        this service is meant not to give.
        """
        with self._lock:
            ids = self._order[::-1]
        out = []
        for i in ids:
            job = self._jobs.get(i)
            if job is None or (owner is not None and job.owner != owner):
                continue
            out.append(job)
            if len(out) >= limit:
                break
        return out


    def sweep(self, ttl: int = TTL_SECONDS) -> dict:
        """Reclaim the workspaces of finished jobs older than the TTL.

        Only TERMINAL jobs: deleting under a running build would remove the files
        it is still writing. The job itself stays in the store, so a caller polling
        an old id still gets its state and check report — it is the artefacts that
        expire, and `_artifact_path` then says so with a 410 rather than a 404.
        """
        now = time.time()
        with self._lock:
            candidates = [self._jobs[i] for i in self._order if i in self._jobs]
        freed, n = 0, 0
        for job in candidates:
            if job.workdir is None:
                continue
            if job.state in (State.QUEUED, State.RUNNING):
                continue
            if now - (job.finished or job.created) < ttl:
                continue
            freed += _reclaim(job)
            n += 1
        return {"reclaimed": n, "bytes_freed": freed}


STORE = Store()


def start_sweeper(store: "Store | None" = None, interval: int = SWEEP_SECONDS,
                  ttl: int = TTL_SECONDS) -> threading.Thread:
    """Run `sweep` forever on a daemon thread."""
    target_store = store or STORE

    def loop():
        while True:
            time.sleep(interval)
            try:
                target_store.sweep(ttl)
            except Exception:      # a sweep must never take the service down
                pass

    t = threading.Thread(target=loop, name="abcprint-sweeper", daemon=True)
    t.start()
    return t
