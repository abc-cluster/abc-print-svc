"""0.15.1: the 0.14 API lives under /v0; its bare paths are deprecated aliases."""
from conftest import garden_thesis_zip, h, wait_terminal

LEGACY = ["/compile", "/compile/manuscript", "/compile/archive", "/jobs", "/jobs/{job_id}",
          "/jobs/{job_id}/artifacts/{name}", "/delivery/destinations", "/jobs/{job_id}/deliver",
          "/profiles", "/toolchain", "/fonts"]


def _archive(client, tok, tmp_path, prefix):
    z = garden_thesis_zip(tmp_path / "t.zip")
    return client.post(f"{prefix}/compile/archive", headers=h(tok), data={"profile": "su-fmhs"},
                       files={"bundle": ("t.zip", z.read_bytes())})


def test_the_schema_documents_v0_and_hides_the_bare_aliases(svc):
    client, _, _, _ = svc
    paths = client.get("/openapi.json").json()["paths"]
    for p in LEGACY:
        assert f"/v0{p}" in paths, p
        assert p not in paths, p
        assert all(op.get("deprecated") for op in paths[f"/v0{p}"].values()), p
    assert "/health" in paths and not paths["/health"]["get"].get("deprecated")
    assert not any(op.get("deprecated") for op in paths["/v1/thesis/runs"].values())


def test_a_build_through_v0_stays_in_v0(svc, tmp_path):
    client, _, t_a, _ = svc
    r = _archive(client, t_a, tmp_path, "/v0")
    assert r.status_code == 202, r.text
    assert r.headers["location"] == f"/v0/jobs/{r.json()['id']}"
    assert r.headers["deprecation"].startswith("@")
    assert 'rel="deprecation"' in r.headers["link"]
    assert "successor-version" not in r.headers["link"]
    job = wait_terminal(client, t_a, r.json()["id"])
    got = client.get(f"/v0/jobs/{job['id']}", headers=h(t_a))
    assert got.status_code == 200 and got.json()["state"] == "succeeded"
    art = client.get(f"/v0/jobs/{job['id']}/artifacts/thesis-assembled.pdf", headers=h(t_a))
    assert art.status_code == 200 and art.content.startswith(b"%PDF")
    assert "deprecation" in art.headers


def test_the_bare_alias_still_works_and_points_at_v0(svc, tmp_path):
    client, _, t_a, _ = svc
    r = _archive(client, t_a, tmp_path, "")
    assert r.status_code == 202, r.text
    assert r.headers["location"] == f"/jobs/{r.json()['id']}"
    assert "deprecation" in r.headers
    assert '</v0/compile/archive>; rel="successor-version"' in r.headers["link"]
    # one run, both namespaces
    rid = r.json()["id"]
    assert client.get(f"/v0/jobs/{rid}", headers=h(t_a)).json()["id"] == rid


def test_refusals_on_v0_are_marked_too(svc):
    client, _, _, _ = svc
    r = client.get("/v0/jobs")
    assert r.status_code == 401 and "deprecation" in r.headers
    r = client.get("/jobs")
    assert r.status_code == 401 and "deprecation" in r.headers


def test_native_and_health_are_not_marked(svc):
    client, _, t_a, _ = svc
    for p in ("/health", "/healthz", "/v1/limits", "/v1/thesis/runs", "/v0/nope"):
        assert "deprecation" not in client.get(p, headers=h(t_a)).headers, p


def test_sunset_is_sent_only_once_configured(make_svc):
    from abcprint import auth
    tok, entry = auth.new_token("alice", ("compile", "read"))
    client, _ = make_svc([entry])
    assert "sunset" not in client.get("/v0/jobs", headers=h(tok)).headers
    client, _ = make_svc([entry], extra_env={"ABCPRINT_V0_SUNSET": "2027-06-30"})
    r = client.get("/v0/jobs", headers=h(tok))
    assert r.headers["sunset"] == "Wed, 30 Jun 2027 00:00:00 GMT"
    assert client.get("/jobs", headers=h(tok)).headers["sunset"] == r.headers["sunset"]


def test_a_run_deleted_natively_lists_no_artefacts_as_a_job(svc):
    client, _, t_a, _ = svc
    up = client.post("/v1/uploads", headers=h(t_a), files={"file": ("01-a.md", b"# A\n")}).json()
    rid = client.post("/v1/thesis/runs", headers=h(t_a),
                      json={"sources": [{"upload": up["upload_id"]}]}).json()["run_id"]
    assert wait_terminal(client, t_a, rid)["artifacts"]
    assert client.delete(f"/v1/thesis/runs/{rid}", headers=h(t_a)).status_code == 200
    job = client.get(f"/v0/jobs/{rid}", headers=h(t_a)).json()
    assert job["state"] == "succeeded" and job["artifacts"] == []
    assert client.get(f"/v0/jobs/{rid}/artifacts/thesis-assembled.pdf",
                      headers=h(t_a)).status_code == 410
