"""In-process job store.

The build is multi-pass and takes tens of seconds, so `/compile` cannot be a
synchronous request: a Logseq plugin pushing a chapter should get an id back
immediately and poll. This store is deliberately in-process and bounded — it is
the smallest thing that makes the async contract real. A deployment serving many
students needs a shared store and a retention sweep, which is a separate piece
of work and is flagged rather than faked here.
"""
from __future__ import annotations

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
            while len(self._order) > self._limit:
                self._jobs.pop(self._order.pop(0), None)
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


STORE = Store()
