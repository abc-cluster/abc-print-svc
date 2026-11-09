"""The 0.14 contract, held against the dispatch-backed service.

Each 0.14 route is driven end to end — request, admission, the executor, the
worker-side execute, the job view, the artefact — with only the two toolchain
calls stubbed (see conftest). Where 0.15 also offers a native /v1 route, the test
checks that both views describe the same run.
"""
import json
import threading
import time
import zipfile

import pytest

from abcprint import auth, capabilities, delivery, equivalence

from conftest import garden_manuscript_zip, garden_thesis_zip, h, wait_terminal


def _zip_bytes(path):
    return open(path, "rb").read()


# --- the thesis engine ---------------------------------------------------------

def test_a_thesis_archive_builds_and_reports_its_checks(svc, tmp_path):
    client, _, t_a, _ = svc
    z = garden_thesis_zip(tmp_path / "t.zip", ("01-introduction.md", "02-methods.md"))
    r = client.post("/compile/archive", headers=h(t_a),
                    files={"bundle": ("t.zip", _zip_bytes(z))},
                    data={"profile": "su-fmhs", "copy": "examination"})
    assert r.status_code == 202, r.text
    job = r.json()
    assert job["state"] == "queued" or job["state"] in ("running", "succeeded")
    assert job["detected_chapters"] == ["01", "02"]
    assert job["options"]["identity"]["source"] == "template defaults"
    assert "archive" in job["options"]
    done = wait_terminal(client, t_a, job["id"])
    assert done["state"] == "succeeded"
    assert done["compliant"] is True and done["checks"]["passed"] == 23
    assert done["options"]["chapters"] == ["01", "02"]
    assert [a["name"] for a in done["artifacts"]] == ["thesis-assembled.pdf"]
    pdf = client.get(f"/jobs/{job['id']}/artifacts/thesis-assembled.pdf", headers=h(t_a))
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")


def test_a_document_that_fails_its_checks_is_still_a_succeeded_job(svc, make_svc, tmp_path):
    """`succeeded` means a document was produced; `compliant` says whether it passes."""
    client, _, t_a, _ = svc
    make_svc.engines.behaviour = "noncompliant"
    z = garden_thesis_zip(tmp_path / "t.zip")
    job = client.post("/compile/archive", headers=h(t_a), files={"bundle": ("t.zip", _zip_bytes(z))},
                      data={"profile": "su-fmhs"}).json()
    done = wait_terminal(client, t_a, job["id"])
    assert done["state"] == "succeeded" and done["compliant"] is False
    assert done["checks"]["failures"] == ["C4 unresolved cross-reference"]


def test_a_render_that_produces_nothing_is_a_failed_job_with_its_log(svc, make_svc, tmp_path):
    client, _, t_a, _ = svc
    make_svc.engines.behaviour = "fail"
    z = garden_thesis_zip(tmp_path / "t.zip")
    job = client.post("/compile/archive", headers=h(t_a), files={"bundle": ("t.zip", _zip_bytes(z))},
                      data={"profile": "su-fmhs"}).json()
    done = wait_terminal(client, t_a, job["id"])
    assert done["state"] == "failed"
    assert "produced no PDF" in done["error"]
    assert done["log_tail"] == ["ERROR something broke"]
    r = client.get(f"/jobs/{job['id']}/artifacts/thesis-assembled.pdf", headers=h(t_a))
    assert r.status_code == 409


def test_multipart_compile_keeps_figure_subpaths_and_passes_options(svc, make_svc):
    client, _, t_a, _ = svc
    r = client.post("/compile", headers=h(t_a), files=[
        ("sources", ("01-introduction.md", b"# One\n")),
        ("sources", ("04-results.md", b"# Four\n")),
        ("sources", ("Acknowledgements.md", b"Thanks\n")),
        ("figures", ("F6.2-metro-src/F6.2.svg", b"<svg/>")),
        ("bibliography", ("references.bib", b"@misc{x,title={X}}")),
    ], data={"chapters": "04", "copy": "submission", "checks": "false"})
    assert r.status_code == 202, r.text
    assert r.json()["detected_chapters"] == ["01", "04"]
    done = wait_terminal(client, t_a, r.json()["id"])
    assert done["state"] == "succeeded"
    assert make_svc.engines.calls[-1] == ("thesis", ["04"], "submission", False)


@pytest.mark.parametrize("data,files,needle", [
    ({"chapters": "09"}, [("sources", ("01-a.md", b"x"))], "requested chapters not in the bundle"),
    # A source name is reduced to its basename (contained, as in 0.14); a figure
    # keeps its subpath, so an escape there is refused.
    ({}, [("sources", ("01-a.md", b"x")), ("figures", ("../../x.svg", b"<svg/>"))], "unsafe path"),
    ({"copy": "pirate"}, [("sources", ("01-a.md", b"x"))], "copy"),
])
def test_a_refused_compile_is_400_and_leaves_no_uploads(svc, data, files, needle):
    client, app, t_a, _ = svc
    r = client.post("/compile", headers=h(t_a), files=files, data=data)
    assert r.status_code == 400, r.text
    assert needle in r.json()["detail"]
    assert app.state.store.all_runs() == []
    # Nothing staged for the refused request survives it: 0.14 deleted the
    # workspace of a refused bundle, and a refused request must not leave the
    # student's text behind as an orphan upload either.
    assert app.state.store.all_uploads() == []


def test_a_refused_archive_names_the_offending_entry(svc, tmp_path):
    client, _, t_a, _ = svc
    p = tmp_path / "bad.zip"
    with zipfile.ZipFile(p, "w") as zf:
        zf.writestr("src/../../etc/passwd", "x")
    r = client.post("/compile/archive", headers=h(t_a), files={"bundle": ("bad.zip", _zip_bytes(p))},
                    data={"profile": "su-fmhs"})
    assert r.status_code == 400
    assert "passwd" in r.json()["detail"] or ".." in r.json()["detail"]


# --- the quarto engine ----------------------------------------------------------

def test_a_manuscript_renders_and_claims_no_compliance(svc):
    client, _, t_a, _ = svc
    r = client.post("/compile/manuscript", headers=h(t_a), files=[
        ("sources", ("index.qmd", b"---\ntitle: T\n---\nHi\n")),
        ("bibliography", ("references.bib", b"@misc{x,title={X}}")),
    ], data={"profile": "biorxiv-dev", "outputs": "pdf"})
    assert r.status_code == 202, r.text
    done = wait_terminal(client, t_a, r.json()["id"])
    assert done["state"] == "succeeded"
    assert done["compliant"] is None          # a profile with no rules must not claim compliance
    assert done["artifacts"][0]["name"] == "index.pdf"


def test_a_manuscript_archive_renders(svc, tmp_path):
    client, _, t_a, _ = svc
    z = garden_manuscript_zip(tmp_path / "m.zip")
    r = client.post("/compile/archive", headers=h(t_a), files={"bundle": ("m.zip", _zip_bytes(z))},
                    data={"profile": "biorxiv-dev"})
    assert r.status_code == 202, r.text
    assert r.json()["options"]["entry"] == "index.qmd"
    assert wait_terminal(client, t_a, r.json()["id"])["state"] == "succeeded"


def test_the_wrong_engine_for_a_route_is_400(svc):
    client, _, t_a, _ = svc
    r = client.post("/compile/manuscript", headers=h(t_a),
                    files=[("sources", ("index.qmd", b"x"))], data={"profile": "su-fmhs"})
    assert r.status_code == 400 and "thesis-assemble" in r.json()["detail"]


def test_a_refused_quarto_metadata_key_is_400_before_any_build(svc, make_svc):
    client, _, t_a, _ = svc
    r = client.post("/compile/manuscript", headers=h(t_a), files=[("sources", ("index.qmd", b"x"))],
                    data={"profile": "biorxiv-dev", "quarto_metadata": "filters: [evil.lua]"})
    assert r.status_code == 400 and "filters" in r.json()["detail"]
    assert make_svc.engines.calls == []


# --- one run, two views ------------------------------------------------------------

def test_a_native_run_is_also_a_job(svc):
    client, _, t_a, _ = svc
    up = client.post("/v1/uploads", headers=h(t_a), files={"file": ("01-introduction.md", b"# One\n")})
    assert up.status_code == 201, up.text
    r = client.post("/v1/thesis/runs", headers=h(t_a),
                    json={"sources": [{"upload": up.json()["upload_id"]}]})
    assert r.status_code == 202, r.text
    run_id = r.json()["run_id"]
    job = wait_terminal(client, t_a, run_id)
    run = client.get(f"/v1/thesis/runs/{run_id}", headers=h(t_a)).json()
    assert run["state"] == "complete" and job["state"] == "succeeded"
    assert run["outcome"]["compliant"] is job["compliant"] is True
    assert [o["name"] for o in run["outputs"]] == [a["name"] for a in job["artifacts"]]
    native = client.get(f"/v1/thesis/runs/{run_id}/outputs/thesis-assembled.pdf", headers=h(t_a))
    legacy = client.get(f"/jobs/{run_id}/artifacts/thesis-assembled.pdf", headers=h(t_a))
    assert native.content == legacy.content


def test_a_native_refusal_is_422_with_the_reason(svc):
    client, _, t_a, _ = svc
    up = client.post("/v1/uploads", headers=h(t_a), files={"file": ("01-a.md", b"x")}).json()
    r = client.post("/v1/thesis/runs", headers=h(t_a),
                    json={"sources": [{"upload": up["upload_id"]}], "chapters": ["07"]})
    assert r.status_code == 422 and r.json()["error"] == "invalid_request"
    assert "requested chapters" in r.json()["msg"]


def test_a_cancelled_run_is_a_failed_job(svc, make_svc):
    client, _, t_a, _ = svc
    make_svc.engines.behaviour = "slow"
    make_svc.engines.release = threading.Event()
    up = client.post("/v1/uploads", headers=h(t_a), files={"file": ("01-a.md", b"x")}).json()
    run_id = client.post("/v1/thesis/runs", headers=h(t_a),
                         json={"sources": [{"upload": up["upload_id"]}]}).json()["run_id"]
    time.sleep(0.2)
    assert client.post(f"/v1/thesis/runs/{run_id}/cancel", headers=h(t_a)).status_code == 202
    make_svc.engines.release.set()
    done = wait_terminal(client, t_a, run_id)
    assert done["state"] == "failed" and "Cancel" in (done["error"] or "")


# --- admission, which 0.14 built by hand and the kit now owns --------------------

def test_the_queue_is_bounded_and_refuses_with_retry_after(make_svc, tmp_path):
    t, e = auth.new_token("alice")
    client, app = make_svc([e], extra_env={"ABCPRINT_MAX_RENDERS": "1", "ABCPRINT_MAX_QUEUED": "1"})
    make_svc.engines.behaviour = "slow"
    make_svc.engines.release = threading.Event()
    z = _zip_bytes(garden_thesis_zip(tmp_path / "t.zip"))

    def submit():
        return client.post("/compile/archive", headers=h(t), files={"bundle": ("t.zip", z)},
                           data={"profile": "su-fmhs"})

    first, second, third = submit(), submit(), submit()
    assert first.status_code == 202 and second.status_code == 202
    assert third.status_code == 429
    assert int(third.headers["Retry-After"]) >= 1
    assert third.json()["error"] in ("queue_full", "caller_quota_exceeded")
    health = client.get("/health").json()["capacity"]
    assert health["max_renders"] == 1 and health["max_queued"] == 1
    make_svc.engines.release.set()
    for r in (first, second):
        assert wait_terminal(client, t, r.json()["id"])["state"] == "succeeded"
    assert submit().status_code == 202          # the slots were given back


def test_an_expired_artefact_is_410_and_the_job_survives_it(make_svc, tmp_path):
    t, e = auth.new_token("alice")
    client, _ = make_svc([e], extra_env={"ABCPRINT_ARTIFACT_TTL_S": "0.3"})
    z = _zip_bytes(garden_thesis_zip(tmp_path / "t.zip"))
    job = client.post("/compile/archive", headers=h(t), files={"bundle": ("t.zip", z)},
                      data={"profile": "su-fmhs"}).json()
    done = wait_terminal(client, t, job["id"])
    time.sleep(0.5)
    r = client.get(f"/jobs/{job['id']}/artifacts/thesis-assembled.pdf", headers=h(t))
    assert r.status_code == 410
    again = client.get(f"/jobs/{job['id']}", headers=h(t)).json()
    assert again["state"] == "succeeded" and again["checks"] == done["checks"]


# --- the rest of the 0.14 surface ---------------------------------------------------

def test_profiles_toolchain_and_fonts_come_from_the_worker_record(svc):
    client, _, t_a, _ = svc
    profs = client.get("/profiles", headers=h(t_a)).json()
    assert {p["ref"] for p in profs["profiles"]} == {"su-fmhs@0.2", "biorxiv-dev@0.1"}
    assert "filters" in profs["refused_quarto_metadata_keys"]
    assert client.get("/toolchain", headers=h(t_a)).json()["quarto"] == "1.7.31"
    fonts = client.get("/fonts", headers=h(t_a)).json()
    assert fonts["ok"] is True and len(fonts["resolutions"]) == 6
    odd = client.get("/fonts", headers=h(t_a), params={"family": "Comic Sans MS"}).json()
    assert odd["ok"] is False and odd["unmeasured"] == ["Comic Sans MS"]


def test_health_without_a_worker_record_is_503(make_svc, monkeypatch):
    t, e = auth.new_token("alice")
    client, _ = make_svc([e], extra_env={"ABCPRINT_CAPABILITIES": "/nonexistent"})
    monkeypatch.setenv("ABCPRINT_CAPABILITIES", "/nonexistent")
    monkeypatch.setattr(capabilities, "_CACHE", None)
    r = client.get("/health")
    assert r.status_code == 503 and "capability record" in r.json()["problems"][0]


def test_the_front_door_needs_only_the_exported_registry(make_svc, tmp_path, monkeypatch):
    """The front-door image has no profile directories at all."""
    from abcprint import profile as profiles
    t, e = auth.new_token("alice")
    reg = tmp_path / "registry.json"
    reg.write_text(json.dumps({"profiles": {n: profiles.load(n).as_dict()
                                            for n in ("su-fmhs", "biorxiv-dev")}}))
    client, _ = make_svc([e])
    monkeypatch.setattr(profiles, "REGISTRY_FILE", str(reg))
    monkeypatch.setattr(profiles, "PROFILE_ROOT", str(tmp_path / "no-profiles-here"))
    assert {p["name"] for p in client.get("/profiles", headers=h(t)).json()["profiles"]} == \
        {"su-fmhs", "biorxiv-dev"}
    # Validation (in the front door) works from the registry alone.
    r = client.post("/compile/manuscript", headers=h(t), files=[("sources", ("index.qmd", b"x"))],
                    data={"profile": "su-fmhs"})
    assert r.status_code == 400 and "thesis-assemble" in r.json()["detail"]
    assert client.post("/compile/manuscript", headers=h(t), files=[("sources", ("index.qmd", b"x"))],
                       data={"profile": "nosuch"}).json()["detail"].startswith("unknown profile")


def test_equivalence_answers_synchronously_as_before(svc, monkeypatch):
    client, _, t_a, _ = svc

    class Rep:
        def as_dict(self):
            return {"equivalent": True, "pages": [3, 3], "text_equal": True,
                    "pixel_pages_checked": [1, 2, 3], "pixel_mismatches": [], "meta_equal": True,
                    "meta_diff": {}, "failures": [], "volatile_excluded": ["/ID[1]"]}

    monkeypatch.setattr(equivalence, "compare", lambda a, b, dpi, sample: Rep())
    r = client.post("/equivalence", headers=h(t_a),
                    files={"a": ("x.pdf", b"%PDF-1"), "b": ("x.pdf", b"%PDF-1")})
    assert r.status_code == 200 and r.json()["equivalent"] is True
    assert r.json()["pages"] == [3, 3]


def test_equivalence_of_an_unreadable_pdf_is_422(svc, monkeypatch):
    client, _, t_a, _ = svc

    def boom(*a, **k):
        raise ValueError("not a PDF")

    monkeypatch.setattr(equivalence, "compare", boom)
    r = client.post("/equivalence", headers=h(t_a), files={"a": ("a.pdf", b"x"), "b": ("b.pdf", b"y")})
    assert r.status_code == 422 and "not a PDF" in r.json()["detail"]


def test_delivery_to_downloads_lands_in_the_callers_folder(svc, tmp_path, monkeypatch):
    client, _, t_a, _ = svc
    monkeypatch.setattr(delivery, "DOWNLOADS_DIR", str(tmp_path / "dl"))
    (tmp_path / "dl").mkdir()
    z = garden_thesis_zip(tmp_path / "t.zip")
    job = client.post("/compile/archive", headers=h(t_a), files={"bundle": ("t.zip", _zip_bytes(z))},
                      data={"profile": "su-fmhs"}).json()
    wait_terminal(client, t_a, job["id"])
    r = client.post(f"/jobs/{job['id']}/deliver", headers=h(t_a),
                    data={"artifact": "thesis-assembled.pdf", "destination": "downloads"})
    assert r.status_code == 200, r.text
    assert (tmp_path / "dl" / "alice" / "thesis-assembled.pdf").is_file()
