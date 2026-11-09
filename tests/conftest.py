"""A real app, a real store and executor, and engines that do not need a toolchain.

The service is the dispatch kit's app plus print-svc's kinds and its 0.14
routes. Everything here runs that stack for real — admission, the store, the
in-process executor, the worker-side `execute` — and replaces only the two
calls that need quarto and the thesis pipeline, which the image has and a
laptop does not.
"""
import json
import os
import time
import zipfile

import pytest

pytest.importorskip("fastapi", reason="web stack not installed")
pytest.importorskip("abc_dispatch", reason="abc-dispatch not installed")

from fastapi.testclient import TestClient            # noqa: E402

from abcprint import auth, capabilities, compile as compiler, profile as profiles, workspace  # noqa: E402

THESIS_PROFILE = "name: su-fmhs\nversion: '0.2'\nengine: thesis-assemble\nrules: {max_pages: 300}\n"
QUARTO_PROFILE = "name: biorxiv-dev\nversion: '0.1'\nengine: quarto-render\n"


def fake_pdf(path, pages=1):
    from pypdf import PdfWriter
    w = PdfWriter()
    for _ in range(pages):
        w.add_blank_page(612, 792)
    with open(path, "wb") as fh:
        w.write(fh)


def _artifact(build, name):
    os.makedirs(build, exist_ok=True)
    fake_pdf(os.path.join(build, name))
    return {"name": name, "bytes": os.path.getsize(os.path.join(build, name)),
            "media_type": "application/pdf"}


class FakeEngines:
    """Stand-ins for compiler.run / run_quarto. `behaviour` decides the outcome."""

    def __init__(self):
        self.behaviour = "ok"          # ok | noncompliant | fail | slow
        self.calls = []
        self.release = None

    def thesis(self, paths, chapters, copy="examination", run_checks=True, runner=None):
        self.calls.append(("thesis", chapters, copy, run_checks))
        if self.behaviour == "slow" and self.release is not None:
            self.release.wait(10)
        if self.behaviour == "fail":
            err = compiler.CompileError("render failed (rc=1) and produced no PDF or DOCX")
            err.log_tail = ["ERROR something broke"]
            raise err
        art = _artifact(paths["build"], "thesis-assembled.pdf")
        ok = self.behaviour != "noncompliant"
        return {"artifacts": [art], "compliant": ok, "return_code": 0 if ok else 1,
                "checks": {"passed": 23 if ok else 22, "failed": 0 if ok else 1, "warnings": 0,
                           "failures": [] if ok else ["C4 unresolved cross-reference"],
                           "warning_detail": [], "all_clear": ok},
                "manifest": {"copy": copy, "chapters": chapters or "all"},
                "log_tail": ["== checks =="]}

    def quarto(self, paths, prof, *, formats, user_metadata=None, entry="index.qmd", runner=None):
        self.calls.append(("manuscript", formats, entry))
        arts = [_artifact(paths["build"], entry.rsplit(".", 1)[0] + ".pdf")]
        return {"artifacts": arts, "compliant": None, "return_code": 0,
                "checks": {"note": "this profile defines no compliance rules"},
                "manifest": {"profile": prof.as_dict(), "outputs": formats}, "log_tail": []}


@pytest.fixture
def make_svc(tmp_path, monkeypatch):
    """Build the service from an environment. Returns a factory so a test can
    choose the tokens, limits and capability record."""
    prof_root = tmp_path / "profiles"
    (prof_root / "su-fmhs").mkdir(parents=True)
    (prof_root / "su-fmhs" / "profile.yaml").write_text(THESIS_PROFILE)
    (prof_root / "biorxiv-dev").mkdir()
    (prof_root / "biorxiv-dev" / "profile.yaml").write_text(QUARTO_PROFILE)
    pipeline = tmp_path / "pipeline"
    pipeline.mkdir()
    (pipeline / "thesis-assemble.sh").write_text("#!/bin/sh\nexit 0\n")
    monkeypatch.setattr(profiles, "PROFILE_ROOT", str(prof_root))
    monkeypatch.setattr(workspace, "PROFILE_DIR", str(prof_root / "su-fmhs"))
    monkeypatch.setattr(workspace, "PIPELINE_DIR", str(pipeline))
    monkeypatch.setenv("ABCPRINT_PIPELINE", str(pipeline))

    engines = FakeEngines()
    monkeypatch.setattr(compiler, "run", engines.thesis)
    monkeypatch.setattr(compiler, "run_quarto", engines.quarto)

    record = {
        "schema": 1,
        "profiles": {n: profiles.load(n).as_dict() for n in ("su-fmhs", "biorxiv-dev")},
        "refused_quarto_metadata_keys": profiles.REFUSED_METADATA,
        "engines_available": {"quarto-render": True, "thesis-assemble": True},
        "toolchain": {"quarto": "1.7.31", "typst_bundled": "0.13.0", "typst_mismatch": False,
                      "problems": []},
        "pipeline": {"vendored": True},
        "fonts": {"required": capabilities.REQUIRED_FONTS,
                  "health": {"ok": True, "font_paths": [], "missing": [], "substituted": [],
                             "resolutions": [{"requested": f, "resolved": f, "available": True}
                                             for f in capabilities.REQUIRED_FONTS]}},
    }
    cap_file = tmp_path / "capabilities.json"
    cap_file.write_text(json.dumps(record))
    monkeypatch.setattr(capabilities, "_CACHE", None)

    clients = []

    def factory(tokens=None, *, disabled=False, extra_env=None):
        from abcprint import service
        env = {"ABCPRINT_DATA_DIR": str(tmp_path / "data"), "ABCPRINT_STORE": "memory",
               "ABCPRINT_RATE_LIMITS": "off", "ABCPRINT_CAPABILITIES": str(cap_file),
               "ABCPRINT_SWEEP_S": "3600"}
        monkeypatch.setenv("ABCPRINT_CAPABILITIES", str(cap_file))
        if tokens is not None:
            env[auth.ENV_VAR] = "|".join(tokens)
        if disabled:
            env[auth.DISABLE_VAR] = "disabled"
        env.update(extra_env or {})
        app = service.build(env)
        client = TestClient(app, raise_server_exceptions=False)
        client.__enter__()
        clients.append(client)
        return client, app

    factory.engines = engines
    yield factory
    for c in clients:
        c.__exit__(None, None, None)


@pytest.fixture
def svc(make_svc):
    """Two tokens: alice (all scopes) and bob (compile, read)."""
    t_a, e_a = auth.new_token("alice", ("compile", "read", "deliver"))
    t_b, e_b = auth.new_token("bob", ("compile", "read"))
    client, app = make_svc([e_a, e_b])
    return client, app, t_a, t_b


def h(tok):
    return {"Authorization": f"Bearer {tok}"}


def wait_terminal(client, tok, job_id, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        j = client.get(f"/jobs/{job_id}", headers=h(tok)).json()
        if j["state"] in ("succeeded", "failed"):
            return j
        time.sleep(0.05)
    raise AssertionError(f"job {job_id} did not finish: {j}")


def garden_thesis_zip(path, chapters=("01-introduction.md",)):
    with zipfile.ZipFile(path, "w") as zf:
        for c in chapters:
            zf.writestr(f"src/{c}", f"# Chapter {c[:2]}\n\nText.\n")
        zf.writestr("src/Acknowledgements.md", "Thanks.\n")
        zf.writestr("src/references.bib", "@misc{x, title={X}}\n")
        zf.writestr("assets/figures/current/F1.1.svg", "<svg xmlns='http://www.w3.org/2000/svg'/>")
    return path


def garden_manuscript_zip(path):
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("src/index.qmd", "---\ntitle: T\n---\n\nHello.\n")
        zf.writestr("src/references.bib", "@misc{x, title={X}}\n")
    return path
