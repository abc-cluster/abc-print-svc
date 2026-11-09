"""Regressions for four defects found by measuring concurrency on 2026-10-19.

Each had been invisible because it only appears when two people use the service
at once, and the whole suite had run one request at a time.
"""
import os
import tempfile
import threading
import time

import pytest

from abcprint import delivery
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


# --- 3 and 4. admission and workspace reclamation -------------------------
#
# 0.14 built both by hand (capacity.Gate, jobs.Store.sweep). Since 0.15 they are
# the dispatch kit's — Admission bounds the queue, the executor's retention and
# the Sweeper reclaim — and test_service.py holds the same properties against the
# running service: the queue refuses with 429 and Retry-After, a refused
# submission gives its slot back, /health reports capacity, and an expired
# artefact is a 410 while its job stays readable.


def test_a_bad_key_is_reported_as_a_bad_key_even_when_unconfigured(monkeypatch):
    """Request before deployment.

    The credential check used to run first, so a caller writing outside their own
    prefix was told "object-store delivery is not configured" — true, unrelated,
    and it would have sent them to an operator instead of to their own key.
    """
    for var in ("ABCPRINT_S3_ENDPOINT", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"):
        monkeypatch.delenv(var, raising=False)
    with pytest.raises(DeliveryError) as e:
        delivery.to_minio("/dev/null", "b", "users/bob/x.pdf", owner="alice")
    assert "users/alice/" in str(e.value)
    assert "not configured" not in str(e.value)
