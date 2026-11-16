"""0.17.0: profiles published without rebuilding the worker, and every run records
which version (and content digest) it was built with."""
import io
import json
import os
import shutil
import time
import zipfile

import pytest

from abcprint import profile as profiles, profile_store, publish
from conftest import QUARTO_PROFILE, garden_manuscript_zip, h, wait_terminal


def _mkprofile(root, version, *, extra=None, body="", bundle=None):
    os.makedirs(root, exist_ok=True)
    y = QUARTO_PROFILE.replace("version: '0.1'", f"version: '{version}'") + body
    if bundle is not None:
        import yaml
        y += yaml.safe_dump({"bundle": bundle})
    with open(os.path.join(root, "profile.yaml"), "w") as fh:
        fh.write(y)
    with open(os.path.join(root, "style.csl"), "w") as fh:
        fh.write(extra or f"<style version='{version}'/>")
    return str(root)


@pytest.fixture
def store_env(tmp_path, monkeypatch):
    src = tmp_path / "store"
    src.mkdir()
    monkeypatch.setenv(profile_store.SOURCE_VAR, f"file://{src}")
    monkeypatch.setenv(profile_store.CACHE_VAR, str(tmp_path / "cache"))
    monkeypatch.setenv(profile_store.TTL_VAR, "0")
    monkeypatch.setattr(profile_store, "_DEFAULT", None)
    return f"file://{src}"


def _publish(url, d, promote=False):
    rc = publish.main(["publish", d, "--source", url] + (["--promote"] if promote else []))
    assert rc == 0


# --- digests and the store ----------------------------------------------------

def test_the_digest_is_of_content_not_packing(tmp_path):
    d = _mkprofile(tmp_path / "p", "0.2")
    a = profile_store.dir_digest(d)
    (tmp_path / "p" / "PROVENANCE.json").write_text('{"vendored_at": "now"}')
    assert profile_store.dir_digest(d) == a           # vendoring metadata is not content
    out = tmp_path / "unpacked"
    out.mkdir()
    profile_store._safe_unpack(profile_store.pack(d), str(out))
    assert profile_store.dir_digest(str(out)) == a
    os.chmod(os.path.join(d, "style.csl"), 0o755)
    assert profile_store.dir_digest(d) != a           # the executable bit is content


def test_a_version_never_changes_and_publishing_does_not_promote(store_env, tmp_path):
    _publish(store_env, _mkprofile(tmp_path / "v2" / "biorxiv-dev", "0.2"))
    st = profile_store.Store(store_env, ttl_s=0)
    assert st.index()["profiles"]["biorxiv-dev"]["default"] == "0.2"     # the first one is
    _publish(store_env, _mkprofile(tmp_path / "v3" / "biorxiv-dev", "0.3"))
    assert st.index()["profiles"]["biorxiv-dev"]["default"] == "0.2"     # a later one is not
    _publish(store_env, _mkprofile(tmp_path / "v2" / "biorxiv-dev", "0.2"))  # same content: fine
    other = _mkprofile(tmp_path / "v2b" / "biorxiv-dev", "0.2", extra="<changed/>")
    assert publish.main(["publish", other, "--source", store_env]) == 1
    assert publish.main(["promote", "biorxiv-dev@0.3", "--source", store_env]) == 0
    assert st.index()["profiles"]["biorxiv-dev"]["default"] == "0.3"


def test_a_tampered_bundle_is_refused(store_env, tmp_path):
    _publish(store_env, _mkprofile(tmp_path / "v2" / "biorxiv-dev", "0.2"))
    st = profile_store.Store(store_env, cache_dir=str(tmp_path / "c2"), ttl_s=0)
    r = st.resolve("biorxiv-dev@0.2")
    path = os.path.join(store_env[len("file://"):], r.key)
    evil = tmp_path / "evil"
    _mkprofile(evil, "0.2", extra="<evil/>")
    with open(path, "wb") as fh:
        fh.write(profile_store.pack(str(evil)))
    with pytest.raises(profile_store.StoreError, match="refusing to build"):
        st.materialize(r)


# --- runs pin and record the version --------------------------------------------

def _manuscript_run(client, tok, tmp_path, profile):
    z = garden_manuscript_zip(tmp_path / f"m-{time.monotonic_ns()}.zip")
    up = client.post("/v1/uploads", headers=h(tok), files={"file": ("m.zip", z.read_bytes())}).json()
    r = client.post("/v1/manuscript/runs", headers=h(tok),
                    json={"bundle": up["upload_id"], "profile": profile})
    assert r.status_code == 202, r.text
    wait_terminal(client, tok, r.json()["run_id"])
    return client.get(f"/v1/manuscript/runs/{r.json()['run_id']}", headers=h(tok)).json()


def test_a_published_version_is_used_without_a_restart_and_recorded(store_env, svc, tmp_path):
    client, _, t_a, _ = svc
    baked = _manuscript_run(client, t_a, tmp_path, "biorxiv-dev")
    assert baked["outcome"]["options"]["profile"] == "biorxiv-dev@0.1"
    assert baked["outcome"]["manifest"]["profile"]["source"] == "baked"
    baked_digest = baked["outcome"]["options"]["profile_digest"]
    assert baked_digest.startswith("sha256:")

    d2 = _mkprofile(tmp_path / "v2" / "biorxiv-dev", "0.2")
    _publish(store_env, d2)                                   # service keeps running
    run = _manuscript_run(client, t_a, tmp_path, "biorxiv-dev")
    prov = run["outcome"]["manifest"]["profile"]
    assert run["outcome"]["options"]["profile"] == "biorxiv-dev@0.2"
    assert prov["digest"] == profile_store.dir_digest(d2) == run["outcome"]["options"]["profile_digest"]
    assert prov["source"] == store_env and prov["published_at"]

    # 0.3 published but not promoted: unpinned still gets 0.2; a pin gets 0.3.
    _publish(store_env, _mkprofile(tmp_path / "v3" / "biorxiv-dev", "0.3"))
    assert _manuscript_run(client, t_a, tmp_path, "biorxiv-dev")["outcome"]["options"]["profile"] == "biorxiv-dev@0.2"
    assert _manuscript_run(client, t_a, tmp_path, "biorxiv-dev@0.3")["outcome"]["options"]["profile"] == "biorxiv-dev@0.3"
    # The baked version stays reachable by pin.
    old = _manuscript_run(client, t_a, tmp_path, "biorxiv-dev@0.1")
    assert old["outcome"]["options"]["profile_digest"] == baked_digest


def test_the_version_is_pinned_at_admission(store_env, svc, make_svc, tmp_path):
    """A promote between admission and build must not change what the run builds."""
    import threading
    client, _, t_a, _ = svc
    _publish(store_env, _mkprofile(tmp_path / "v2" / "biorxiv-dev", "0.2"))
    _publish(store_env, _mkprofile(tmp_path / "v3" / "biorxiv-dev", "0.3"))
    make_svc.engines.behaviour = "slow"
    make_svc.engines.release = threading.Event()
    z = garden_manuscript_zip(tmp_path / "m.zip")
    up = client.post("/v1/uploads", headers=h(t_a), files={"file": ("m.zip", z.read_bytes())}).json()
    rid = client.post("/v1/manuscript/runs", headers=h(t_a),
                      json={"bundle": up["upload_id"], "profile": "biorxiv-dev"}).json()["run_id"]
    publish.main(["promote", "biorxiv-dev@0.3", "--source", store_env])
    make_svc.engines.release.set()
    wait_terminal(client, t_a, rid)
    run = client.get(f"/v1/manuscript/runs/{rid}", headers=h(t_a)).json()
    assert run["outcome"]["options"]["profile"] == "biorxiv-dev@0.2"


def test_the_listing_shows_published_and_baked_versions(store_env, svc, tmp_path):
    client, _, t_a, _ = svc
    _publish(store_env, _mkprofile(tmp_path / "v2" / "biorxiv-dev", "0.2"))
    body = client.get("/svc/v1/profiles", headers=h(t_a)).json()
    by = {p["name"]: p for p in body["profiles"]}
    assert body["store"] == store_env
    assert by["biorxiv-dev"]["default"] == "0.2" and by["biorxiv-dev"]["source"] == store_env
    assert by["su-fmhs"]["source"] == "baked" and by["su-fmhs"]["versions"][0]["digest"].startswith("sha256:")
    assert client.get("/svc/v1/profiles").status_code == 401


# --- the profile's own zip rules -------------------------------------------------

def _zip(path, files):
    with zipfile.ZipFile(path, "w") as zf:
        for n, b in files.items():
            zf.writestr(n, b)
    return path


def _submit_zip(client, tok, path, profile="biorxiv-dev"):
    up = client.post("/v1/uploads", headers=h(tok), files={"file": ("b.zip", path.read_bytes())}).json()
    return client.post("/v1/manuscript/runs", headers=h(tok),
                       json={"bundle": up["upload_id"], "profile": profile})


def test_a_published_profile_brings_its_own_zip_rules(store_env, svc, tmp_path):
    client, _, t_a, _ = svc
    _publish(store_env, _mkprofile(tmp_path / "v2" / "biorxiv-dev", "0.2",
                                   bundle={"required": ["src/references.bib"],
                                           "allowed": ["src/**", "assets/**"]}))
    no_bib = _zip(tmp_path / "a.zip", {"src/index.qmd": "# x\n"})
    r = _submit_zip(client, t_a, no_bib)
    assert r.status_code == 422 and "src/references.bib" in r.json()["msg"]
    stray = _zip(tmp_path / "b.zip", {"src/index.qmd": "# x\n", "src/references.bib": "",
                                      "notes/todo.txt": "x"})
    r = _submit_zip(client, t_a, stray)
    assert r.status_code == 422 and "notes/todo.txt" in r.json()["msg"]
    good = _zip(tmp_path / "c.zip", {"src/index.qmd": "# x\n", "src/references.bib": ""})
    assert _submit_zip(client, t_a, good).status_code == 202


@pytest.mark.parametrize("name", ["src/x.lua", "_extensions/evil/_extension.yml", "src/_quarto.yml"])
def test_a_client_bundle_never_brings_code(svc, tmp_path, name):
    client, _, t_a, _ = svc
    z = _zip(tmp_path / "e.zip", {"src/index.qmd": "# x\n", name: "x"})
    r = _submit_zip(client, t_a, z)
    assert r.status_code == 422 and name in r.json()["msg"]


def test_a_flat_manuscript_zip_is_moved_under_src_and_builds(svc, tmp_path):
    """0.16.0 accepted this at the door and failed it in the worker."""
    client, _, t_a, _ = svc
    z = _zip(tmp_path / "f.zip", {"index.qmd": "# x\n", "references.bib": "",
                                  "figures/a.svg": "<svg xmlns='http://www.w3.org/2000/svg'/>"})
    r = _submit_zip(client, t_a, z)
    assert r.status_code == 202, r.text
    done = wait_terminal(client, t_a, r.json()["run_id"])
    assert done["state"] == "succeeded", done


def test_a_top_level_document_beside_src_is_refused_at_the_door(svc, tmp_path):
    client, _, t_a, _ = svc
    z = _zip(tmp_path / "m.zip", {"index.qmd": "# x\n", "src/other.md": "x"})
    r = _submit_zip(client, t_a, z)
    assert r.status_code == 422 and "under src/" in r.json()["msg"]


def test_a_flat_thesis_zip_says_what_to_do(svc, tmp_path):
    client, _, t_a, _ = svc
    z = _zip(tmp_path / "t.zip", {"01-introduction.md": "# One\n"})
    up = client.post("/v1/uploads", headers=h(t_a), files={"file": ("t.zip", z.read_bytes())}).json()
    r = client.post("/v1/thesis/runs", headers=h(t_a), json={"bundle": up["upload_id"]})
    assert r.status_code == 422 and "src/NN-slug.md" in r.json()["msg"]


# --- publish refuses what would fail later --------------------------------------

def test_publish_refuses_what_would_fail_a_build(tmp_path):
    d = _mkprofile(tmp_path / "x" / "biorxiv-dev", "0.2", bundle={"layouts": ["round"]})
    with pytest.raises(publish.PublishError, match="layouts"):
        publish.check(d, capability_record={"engines_available": {"quarto-render": True}})
    d = _mkprofile(tmp_path / "y" / "biorxiv-dev", "unversioned")
    with pytest.raises(publish.PublishError, match="publishable version"):
        publish.check(d, capability_record={"engines_available": {"quarto-render": True}})
    d = _mkprofile(tmp_path / "z" / "biorxiv-dev", "0.2", bundle={"required": ["src/index.qmd"]})
    os.makedirs(os.path.join(d, "examples"))
    _zip(tmp_path / "z" / "biorxiv-dev" / "examples" / "demo.zip", {"src/main.qmd": "x"})
    with pytest.raises(publish.PublishError, match="own bundle rules"):
        publish.check(d, capability_record={"engines_available": {"quarto-render": True}})
    d = _mkprofile(tmp_path / "w" / "biorxiv-dev", "0.2", extra="by Jane Secret")
    with pytest.raises(publish.PublishError, match="denied term"):
        publish.check(d, deny_terms=["jane secret"],
                      capability_record={"engines_available": {"quarto-render": True}})
    d = _mkprofile(tmp_path / "q" / "biorxiv-dev", "0.2", body="requires: {quarto: '>=9'}\n")
    with pytest.raises(publish.PublishError, match="requires quarto"):
        publish.check(d, capability_record={"engines_available": {"quarto-render": True},
                                            "toolchain": {"quarto": "1.7.31"}})


def test_the_legacy_thesis_profile_reports_its_vendored_revision(make_svc, tmp_path):
    root = profiles.PROFILE_ROOT
    os.remove(os.path.join(root, "su-fmhs", "profile.yaml"))
    with open(os.path.join(root, "su-fmhs", "PROVENANCE.json"), "w") as fh:
        json.dump({"revision": "d0e8657"}, fh)
    prof = profiles.load("su-fmhs")
    assert prof.version == "git-d0e8657" and prof.digest.startswith("sha256:")
    assert profiles.load("su-fmhs@unversioned").version == "git-d0e8657"   # an old pin still works


# --- clients can ask for the shape of a zip -----------------------------------------

def test_the_shape_of_a_zip_can_be_asked_for(svc):
    client, _, t_a, _ = svc
    s = client.get("/svc/v1/profiles/biorxiv-dev/shape", headers=h(t_a)).json()
    assert s["profile"] == "biorxiv-dev@0.1" and set(s["layouts"]) == {"garden", "flat"}
    assert "**/*.lua" in s["rules"]["forbidden"]
    assert any(e["path"] == "src/index.qmd" and e.get("required") for e in s["tree"])
    t = client.get("/svc/v1/profiles/su-fmhs/shape", headers=h(t_a)).json()
    assert list(t["layouts"]) == ["garden"]
    assert any("NN-slug" in (e.get("pattern") or "") for e in t["tree"])
    assert client.get("/svc/v1/profiles/nosuch/shape", headers=h(t_a)).status_code == 404
    assert client.get("/svc/v1/profiles/biorxiv-dev/shape").status_code == 401


@pytest.mark.parametrize("ref,kind", [("biorxiv-dev", "manuscript"), ("su-fmhs", "thesis")])
def test_the_skeleton_zip_is_accepted_as_it_is(svc, tmp_path, ref, kind):
    client, _, t_a, _ = svc
    r = client.get(f"/svc/v1/profiles/{ref}/skeleton.zip", headers=h(t_a))
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    up = client.post("/v1/uploads", headers=h(t_a), files={"file": ("s.zip", r.content)}).json()
    run = client.post(f"/v1/{kind}/runs", headers=h(t_a), json={"bundle": up["upload_id"], "profile": ref})
    assert run.status_code == 202, run.text


def test_a_published_profile_describes_its_own_shape(store_env, svc, tmp_path):
    client, _, t_a, _ = svc
    d = _mkprofile(tmp_path / "v2" / "biorxiv-dev", "0.2",
                   bundle={"required": ["src/paper.qmd"], "allowed": ["src/**"],
                           "tree": [{"path": "src/paper.qmd", "role": "the paper"}]})
    _publish(store_env, d)
    s = client.get("/svc/v1/profiles/biorxiv-dev/shape", headers=h(t_a)).json()
    assert s["profile"] == "biorxiv-dev@0.2" and s["tree_from"] == "profile"
    assert s["tree"] == [{"path": "src/paper.qmd", "role": "the paper", "required": True}]
    assert s["rules"]["allowed"] == ["src/**"]


def test_the_front_door_mirrors_an_admitted_version_for_the_offline_worker(store_env, svc, tmp_path, monkeypatch):
    client, _, t_a, _ = svc
    mirror = tmp_path / "mirror"
    monkeypatch.setenv(profile_store.MIRROR_VAR, f"file://{mirror}")
    monkeypatch.setattr(profile_store, "_MIRROR", None)
    d2 = _mkprofile(tmp_path / "v2" / "biorxiv-dev", "0.2")
    _publish(store_env, d2)
    _publish(store_env, _mkprofile(tmp_path / "v3" / "biorxiv-dev", "0.3"))   # never admitted
    run = _manuscript_run(client, t_a, tmp_path, "biorxiv-dev")
    idx = json.loads((mirror / "index.json").read_text())["profiles"]["biorxiv-dev"]
    assert sorted(idx["versions"]) == ["0.2"] and idx["default"] == "0.2"
    # What the worker sees: the mirror as its only source, no network.
    worker = profile_store.Store(f"file://{mirror}", cache_dir=str(tmp_path / "wcache"), ttl_s=0)
    r = worker.resolve(run["outcome"]["options"]["profile"])
    assert profile_store.dir_digest(worker.materialize(r)) == profile_store.dir_digest(d2)
    assert worker.resolve("biorxiv-dev@0.3") is None
