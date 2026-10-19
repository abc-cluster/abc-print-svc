"""Regressions for four defects found by measuring concurrency on 2026-10-19.

Each had been invisible because it only appears when two people use the service
at once, and the whole suite had run one request at a time.
"""
import os
import tempfile
import threading
import time

import pytest

from abcprint import capacity, delivery, jobs
from abcprint.capacity import Full, Gate
from abcprint.delivery import DeliveryError


# --- 1. the render log was a fixed path -----------------------------------

def test_the_pipeline_does_not_write_a_shared_render_log():
    """/tmp/su-pdf.log was written by EVERY render and read back to count
    citation warnings, so a clean document was reported as carrying another
    document's warnings — four of four concurrent trials.
    """
    src = "pipeline/thesis-su-pdf.sh"
    if not os.path.isfile(src):
        pytest.skip("pipeline not vendored")
    text = open(src, encoding="utf-8").read()
    code = [l for l in text.splitlines() if not l.lstrip().startswith("#")]
    offenders = [l.strip() for l in code if "/tmp/su-pdf.log" in l]
    assert not offenders, f"a shared render log is back: {offenders}"
    # And the warning count must come from the per-build log.
    assert 'RLOG="$QDIR/$SLUG.log"' in text
    counted = [l for l in code if "citation .* not found" in l]
    assert counted and all('"$RLOG"' in l for l in counted), counted


# --- 2. delivery had no per-caller namespace ------------------------------

def test_downloads_are_namespaced_by_the_caller(tmp_path, monkeypatch):
    """Two tokens delivering one filename wrote one path; the second won."""
    monkeypatch.setattr(delivery, "DOWNLOADS_DIR", str(tmp_path))
    a = tmp_path / "a.pdf"; a.write_bytes(b"A" * 10)
    b = tmp_path / "b.pdf"; b.write_bytes(b"B" * 20)

    r1 = delivery.to_downloads(str(a), "thesis.pdf", owner="alice")
    r2 = delivery.to_downloads(str(b), "thesis.pdf", owner="bob")
    assert r1["path"] != r2["path"]
    assert open(r1["path"], "rb").read() == b"A" * 10      # not clobbered
    assert open(r2["path"], "rb").read() == b"B" * 20


@pytest.mark.parametrize("bad", ["../bob", "..", "a/b", "/abs", ".", ""])
def test_a_subdir_that_is_not_one_component_is_refused(tmp_path, monkeypatch, bad):
    """Refused, not basenamed. `a/b` used to become `b` and `../bob` became
    `bob` — contained, but not what the caller asked for, and two callers
    wanting different nested folders would have collided in one."""
    monkeypatch.setattr(delivery, "DOWNLOADS_DIR", str(tmp_path))
    src = tmp_path / "s.pdf"; src.write_bytes(b"x")
    if bad == "":                      # empty means "no subdir", which is valid
        assert delivery.to_downloads(str(src), "t.pdf", bad, owner="alice")["path"]
        return
    with pytest.raises(DeliveryError):
        delivery.to_downloads(str(src), "t.pdf", bad, owner="alice")


def test_a_delivery_always_lands_inside_the_callers_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(delivery, "DOWNLOADS_DIR", str(tmp_path))
    src = tmp_path / "s.pdf"; src.write_bytes(b"x")
    r = delivery.to_downloads(str(src), "t.pdf", "drafts", owner="alice")
    mine = os.path.abspath(os.path.join(str(tmp_path), "alice"))
    assert os.path.commonpath([mine, os.path.abspath(r["path"])]) == mine


def test_a_minio_key_outside_the_callers_prefix_is_refused():
    assert delivery.minio_key_for("alice", "users/alice/t.pdf") == "users/alice/t.pdf"
    for bad in ("users/bob/t.pdf", "t.pdf", "/users/alice/t.pdf",
                "users/alice/../bob/t.pdf", ""):
        with pytest.raises(DeliveryError):
            delivery.minio_key_for("alice", bad)


def test_the_key_is_refused_rather_than_silently_relocated():
    """Rewriting it would make the returned uri differ from the request, with no
    way for the caller to tell that from success."""
    with pytest.raises(DeliveryError) as e:
        delivery.minio_key_for("alice", "users/bob/t.pdf")
    assert "users/alice/" in str(e.value) and "users/bob/t.pdf" in str(e.value)


# --- 3. no admission control ----------------------------------------------

def test_the_queue_is_bounded_and_refuses_rather_than_accepting_forever():
    g = Gate(max_renders=2, max_queued=3)
    for _ in range(5):
        g.reserve()
    with pytest.raises(Full) as e:
        g.reserve()
    assert "2 rendering" in str(e.value) and "3 may wait" in str(e.value)


def test_only_max_renders_run_at_once():
    g = Gate(max_renders=2, max_queued=10)
    peak, inside, lock = 0, 0, threading.Lock()

    def work():
        nonlocal peak, inside
        g.reserve()
        with g:
            with lock:
                inside += 1
                peak = max(peak, inside)
            time.sleep(0.2)
            with lock:
                inside -= 1

    ts = [threading.Thread(target=work) for _ in range(6)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert peak == 2, f"{peak} renders ran at once, limit was 2"
    assert g.state()["admitted"] == 0


def test_a_refused_reservation_is_given_back():
    g = Gate(max_renders=1, max_queued=0)
    g.reserve()
    g.release_reservation()
    g.reserve()                      # would raise if the first leaked


def test_the_gate_reports_what_it_is_doing():
    g = Gate(max_renders=2, max_queued=2)
    g.reserve(); g.reserve(); g.reserve()
    st = g.state()
    assert st == {"max_renders": 2, "max_queued": 2, "rendering": 0,
                  "waiting": 3, "admitted": 3}


@pytest.mark.parametrize("bad", ["0", "-1", "nope"])
def test_a_nonsense_limit_is_fatal_rather_than_defaulted(bad, monkeypatch):
    monkeypatch.setenv("ABCPRINT_MAX_RENDERS", bad)
    with pytest.raises(RuntimeError):
        capacity._env_int("ABCPRINT_MAX_RENDERS", 2)


# --- 4. workspaces were never reclaimed -----------------------------------

def _job_with_workdir(store, state=jobs.State.SUCCEEDED, age=0.0, size=1000):
    d = tempfile.mkdtemp(prefix="abcprint-test-")
    with open(os.path.join(d, "artifact.pdf"), "wb") as fh:
        fh.write(b"x" * size)
    job = store.create({})
    job.workdir = d
    job.state = state
    job.finished = time.time() - age
    return job, d


def test_eviction_reclaims_the_workspace():
    """The evicted Job held the only record of the path, so dropping it leaked
    the directory permanently."""
    store = jobs.Store(limit=2)
    _, d0 = _job_with_workdir(store)
    _, d1 = _job_with_workdir(store)
    _, d2 = _job_with_workdir(store)
    assert not os.path.exists(d0)
    assert os.path.exists(d1) and os.path.exists(d2)


def test_the_sweep_reclaims_finished_jobs_past_the_ttl():
    store = jobs.Store()
    old, d_old = _job_with_workdir(store, age=10_000, size=2000)
    new, d_new = _job_with_workdir(store, age=5)
    res = store.sweep(ttl=3600)
    assert res["reclaimed"] == 1 and res["bytes_freed"] == 2000
    assert not os.path.exists(d_old) and os.path.exists(d_new)
    # Clearing workdir is what makes the 410 honest.
    assert old.workdir is None and new.workdir is not None


@pytest.mark.parametrize("state", [jobs.State.QUEUED, jobs.State.RUNNING])
def test_the_sweep_never_touches_a_build_in_flight(state):
    """Deleting under a running render would remove files it is still writing."""
    store = jobs.Store()
    job, d = _job_with_workdir(store, state=state, age=10_000)
    assert store.sweep(ttl=1)["reclaimed"] == 0
    assert os.path.exists(d) and job.workdir == d


def test_the_job_survives_its_workspace():
    """A caller polling an old id still gets its state and check report; it is
    the artefacts that expire."""
    store = jobs.Store()
    job, _ = _job_with_workdir(store, age=10_000)
    job.checks = {"passed": 22, "failed": 0}
    store.sweep(ttl=1)
    assert store.get(job.id) is job
    assert store.get(job.id).checks == {"passed": 22, "failed": 0}
