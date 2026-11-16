"""0.18.0: equivalence by reference, and comparisons need `compile`."""
import time

import pytest

from abcprint import auth, equivalence
from conftest import garden_manuscript_zip, h, wait_terminal


class Rep:
    def __init__(self, a, b):
        self.a, self.b = a, b

    def as_dict(self):
        return {"equivalent": True, "a_bytes": len(open(self.a, "rb").read()),
                "b_bytes": len(open(self.b, "rb").read())}


@pytest.fixture
def compare(monkeypatch):
    seen = []

    def fake(a, b, dpi, sample):
        seen.append((a, b))
        return Rep(a, b)
    monkeypatch.setattr(equivalence, "compare", fake)
    return seen


def _build(client, tok, tmp_path):
    z = garden_manuscript_zip(tmp_path / f"m-{time.monotonic_ns()}.zip")
    up = client.post("/v1/uploads", headers=h(tok), files={"file": ("m.zip", z.read_bytes())}).json()
    r = client.post("/v1/manuscript/runs", headers=h(tok), json={"bundle": up["upload_id"]})
    assert r.status_code == 202, r.text
    wait_terminal(client, tok, r.json()["run_id"])
    run = client.get(f"/v1/manuscript/runs/{r.json()['run_id']}", headers=h(tok)).json()
    assert run["state"] == "complete", run
    return run


def _wait(client, tok, run_id):
    for _ in range(400):
        st = client.get(f"/v1/equivalence/runs/{run_id}", headers=h(tok)).json()
        if st["state"] in ("complete", "executor_error", "system_error"):
            return st
        time.sleep(0.05)
    raise AssertionError(st)


def test_two_builds_compared_by_reference(svc, tmp_path, compare):
    client, _, t_a, _ = svc
    one, two = _build(client, t_a, tmp_path), _build(client, t_a, tmp_path)
    body = {"a": {"run": one["run_id"], "output": "index.pdf"},
            "b": {"run": two["run_id"], "output": "index.pdf"}}
    r = client.post("/v1/equivalence/runs", headers=h(t_a), json=body)
    assert r.status_code == 202, r.text
    st = _wait(client, t_a, r.json()["run_id"])
    assert st["state"] == "complete" and st["outcome"]["equivalent"] is True
    assert st["outcome"]["a_bytes"] > 0
    # The sources are untouched and can be compared again.
    pdf = next(o for o in one["outputs"] if o["name"] == "index.pdf")
    assert client.get(pdf["href"], headers=h(t_a)).status_code == 200
    again = client.post("/v1/equivalence/runs", headers=h(t_a), json=body)
    assert _wait(client, t_a, again.json()["run_id"])["state"] == "complete"


def test_a_reference_and_an_upload_mix(svc, tmp_path, compare):
    client, _, t_a, _ = svc
    one = _build(client, t_a, tmp_path)
    pdf = client.get(next(o for o in one["outputs"] if o["name"] == "index.pdf")["href"],
                     headers=h(t_a)).content
    up = client.post("/v1/uploads", headers=h(t_a), files={"file": ("mine.pdf", pdf)}).json()["upload_id"]
    r = client.post("/v1/equivalence/runs", headers=h(t_a),
                    json={"a": {"run": one["run_id"], "output": "index.pdf"}, "b": up})
    st = _wait(client, t_a, r.json()["run_id"])
    assert st["outcome"]["a_bytes"] == st["outcome"]["b_bytes"] == len(pdf)


def test_references_are_owner_only_pdf_only_and_listed(svc, tmp_path, compare):
    client, _, t_a, t_b = svc
    one = _build(client, t_a, tmp_path)
    ref = {"run": one["run_id"], "output": "index.pdf"}
    # Another caller's run reads as unknown, as any other caller's resource does.
    r = client.post("/v1/equivalence/runs", headers=h(t_b), json={"a": ref, "b": ref})
    assert r.status_code == 422 and "not yours" in r.json()["msg"]
    r = client.post("/v1/equivalence/runs", headers=h(t_a),
                    json={"a": ref, "b": {"run": one["run_id"], "output": "other.pdf"}})
    assert r.status_code == 422 and "index.pdf" in r.json()["msg"]
    r = client.post("/v1/equivalence/runs", headers=h(t_a),
                    json={"a": ref, "b": {"run": one["run_id"], "output": "run.log"}})
    assert r.status_code == 422 and "only a PDF" in r.json()["msg"]
    assert not compare


def test_creating_a_comparison_needs_compile_and_reading_it_needs_read(make_svc, tmp_path, compare):
    t_full, e_full = auth.new_token("alice", ("compile", "read"))
    t_ro, e_ro = auth.new_token("alice", ("read",))
    client, _ = make_svc([e_full, e_ro])
    one = _build(client, t_full, tmp_path)
    ref = {"run": one["run_id"], "output": "index.pdf"}
    r = client.post("/v1/equivalence/runs", headers=h(t_ro), json={"a": ref, "b": ref})
    assert r.status_code == 403 and "compile" in r.json()["msg"]
    r = client.post("/v1/equivalence/runs", headers=h(t_full), json={"a": ref, "b": ref})
    rid = r.json()["run_id"]
    _wait(client, t_full, rid)
    # Reading the verdict back is a read.
    assert client.get(f"/v1/equivalence/runs/{rid}", headers=h(t_ro)).json()["outcome"]["equivalent"]
