"""The gate, and the isolation that makes the gate worth having.

This is a regression suite for a live exposure, not a hypothetical. Until
2026-10-19 the service was reachable unauthenticated on the public internet,
`GET /jobs` enumerated every caller's jobs, and every artifact was fetchable by
id — so one caller could and did download another's compiled document. Two
things had to be true to close it, and only one of them is a lock on the door:

  1. a request without a recognised token is refused, and
  2. a request WITH a recognised token sees only its own jobs.

A shared secret would have satisfied (1) and left (2) exactly as broken.

Since 0.15 a job is a dispatch run, so (2) is now the kit's ownership model;
these tests hold it to the same assertions, on both the 0.14 routes and the
native /v1 routes.
"""
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="web stack not installed; run the suite in the image")
pytest.importorskip("abc_dispatch", reason="abc-dispatch not installed")

from abc_dispatch import State                         # noqa: E402
from abc_dispatch.identity import Caller               # noqa: E402
from abc_dispatch.ids import new_id                    # noqa: E402
from abc_dispatch.store import OutputRecord, RunRecord  # noqa: E402

from abcprint import auth                              # noqa: E402

from conftest import h                                 # noqa: E402


def add_job(app, owner, *, kind="thesis", state=State.COMPLETE, outputs=()):
    """A run owned by `owner`, put straight into the store the routes read."""
    run = RunRecord(run_id=new_id("run"), kind=kind,
                    owner=Caller(owner, f"users/{owner}"), state=state,
                    request={"profile": "su-fmhs", "copy": "examination"}, request_hash="x",
                    created_at=time.time(), outcome={"artifacts": [{"name": n} for n in outputs]},
                    outputs=[OutputRecord(name=n, path=Path(__file__), bytes=1, sha256="0")
                             for n in outputs])
    app.state.store.add_run(run)
    time.sleep(0.002)                 # distinct created_at, so ordering is defined
    return run


# --- (1) the door ----------------------------------------------------------

def test_health_is_open_because_the_probe_needs_it(svc):
    client, _, _, _ = svc
    r = client.get("/health")
    assert r.status_code in (200, 503)
    # And it says what the gate is doing, so an open deployment is visible.
    assert r.json()["auth"]["mode"] == "token"
    assert r.json()["auth"]["tokens"] == 2


@pytest.mark.parametrize("path", ["/jobs", "/profiles", "/toolchain", "/fonts",
                                  "/v1/thesis/runs", "/v1/limits"])
def test_no_token_is_refused(svc, path):
    client, _, _, _ = svc
    r = client.get(path)
    assert r.status_code == 401
    assert "bearer token" in r.json()["detail"].lower()
    assert r.headers.get("WWW-Authenticate") == "Bearer"


@pytest.mark.parametrize("header", [
    "", "Bearer", "Bearer ", "Basic abcp_x", "abcp_x", "Bearer abcp_wrong",
])
def test_a_bad_authorization_header_is_refused(svc, header):
    client, _, _, _ = svc
    assert client.get("/jobs", headers={"Authorization": header}).status_code == 401
    assert client.get("/v1/thesis/runs", headers={"Authorization": header}).status_code == 401


def test_a_valid_token_gets_in(svc):
    client, _, t_a, _ = svc
    assert client.get("/jobs", headers=h(t_a)).status_code == 200
    assert client.get("/v1/thesis/runs", headers=h(t_a)).status_code == 200


def test_a_scope_it_does_not_hold_is_403_not_401(svc):
    """403 says "fix your permissions"; 401 says "fix your token". Different jobs."""
    client, _, _, t_b = svc                 # bob has no deliver scope
    r = client.get("/delivery/destinations", headers=h(t_b))
    assert r.status_code == 403
    assert "deliver" in r.json()["detail"]


def test_a_read_only_token_cannot_create_a_native_run(make_svc):
    """The kit has no scopes; print-svc decides the scope from the route."""
    t_r, e_r = auth.new_token("reader", ("read",))
    client, _ = make_svc([e_r])
    assert client.get("/v1/thesis/runs", headers=h(t_r)).status_code == 200
    r = client.post("/v1/uploads", headers=h(t_r), files={"file": ("a.md", b"x")})
    assert r.status_code == 403 and "compile" in r.json()["detail"]


def test_a_refusal_does_not_disclose_which_tokens_exist(svc):
    client, _, _, _ = svc
    body = client.get("/jobs", headers={"Authorization": "Bearer abcp_wrong"}).text
    for leak in ("alice", "bob", "sha256"):
        assert leak not in body


# --- (2) the isolation, which is the part a shared secret would have missed --

def test_a_token_sees_only_its_own_jobs(svc):
    client, app, t_a, t_b = svc
    a = add_job(app, "alice")
    b = add_job(app, "bob")
    seen_a = [j["id"] for j in client.get("/jobs", headers=h(t_a)).json()["jobs"]]
    seen_b = [j["id"] for j in client.get("/jobs", headers=h(t_b)).json()["jobs"]]
    assert seen_a == [a.run_id]
    assert seen_b == [b.run_id]


def test_another_tokens_job_is_404_not_403(svc):
    """404, so the response does not confirm that the id exists."""
    client, app, t_a, _ = svc
    b = add_job(app, "bob")
    assert client.get(f"/jobs/{b.run_id}", headers=h(t_a)).status_code == 404
    assert client.get(f"/v1/thesis/runs/{b.run_id}", headers=h(t_a)).status_code == 404


def test_another_tokens_artifact_cannot_be_downloaded(svc):
    """The exact call that leaked: GET /jobs/<id>/artifacts/<name> by id alone."""
    client, app, t_a, _ = svc
    b = add_job(app, "bob", outputs=["thesis-assembled.pdf"])
    r = client.get(f"/jobs/{b.run_id}/artifacts/thesis-assembled.pdf", headers=h(t_a))
    assert r.status_code == 404
    r = client.get(f"/v1/thesis/runs/{b.run_id}/outputs/thesis-assembled.pdf", headers=h(t_a))
    assert r.status_code == 404


def test_a_neighbours_volume_cannot_push_out_your_own_jobs(svc):
    """The limit applies after the ownership filter, not before.

    Filtering a pre-limited window would make a caller's own jobs vanish from
    their list whenever a busier token ran more builds — a quiet wrong answer.
    """
    client, app, t_a, _ = svc
    mine = add_job(app, "alice")
    for _ in range(60):
        add_job(app, "bob")
    assert [j["id"] for j in client.get("/jobs", headers=h(t_a)).json()["jobs"]] == [mine.run_id]


def test_an_equivalence_run_is_not_a_job(svc):
    """/jobs lists compilations; a comparison is not one."""
    client, app, t_a, _ = svc
    add_job(app, "alice", kind="equivalence")
    assert client.get("/jobs", headers=h(t_a)).json()["jobs"] == []


# --- configuration ---------------------------------------------------------

def test_no_tokens_configured_refuses_everything_rather_than_allowing_it(make_svc, monkeypatch):
    """Fail closed. The original defect was a service that failed open."""
    client, _ = make_svc([])
    r = client.get("/jobs")
    assert r.status_code == 503
    assert auth.ENV_VAR in r.json()["detail"]
    assert client.get("/v1/thesis/runs").status_code == 503
    assert client.get("/health").json()["auth"]["mode"] == "no-tokens"


def test_auth_can_be_switched_off_but_says_so_loudly(make_svc):
    client, _ = make_svc(None, disabled=True)
    assert client.get("/jobs").status_code == 200
    state = client.get("/health").json()["auth"]
    assert state["mode"] == "disabled"
    assert "warning" in state                  # never silently open


def test_a_malformed_token_table_stops_the_service_starting(make_svc):
    with pytest.raises(RuntimeError, match=auth.ENV_VAR):
        make_svc(["nonsense"])


@pytest.mark.parametrize("bad", [
    "jorge:compile:sha256:nothex",             # not a digest
    "jorge:compile:md5:" + "a" * 64,           # wrong algorithm
    "jorge:teleport:sha256:" + "a" * 64,       # unknown scope
    "jorge::sha256:" + "a" * 64,               # no scopes at all
    "nonsense",
])
def test_malformed_configuration_is_fatal_not_skipped(bad):
    """A dropped entry is a token that stops working with no explanation."""
    with pytest.raises(auth.AuthError):
        auth.load({auth.ENV_VAR: bad})


def test_two_entries_with_one_hash_are_refused():
    _, entry = auth.new_token("alice")
    label_swapped = entry.replace("alice", "bob", 1)
    with pytest.raises(auth.AuthError) as e:
        auth.load({auth.ENV_VAR: f"{entry}|{label_swapped}"})
    assert "never match" in str(e.value)


def test_the_bare_single_token_shape_works():
    """The env var is named in the singular; one token is the common case."""
    token, entry = auth.new_token("x")
    digest = entry.split(":")[-1]
    table = auth.load({auth.ENV_VAR: f"sha256:{digest}"})
    p = auth.verify(token, table)
    assert p is not None and p.label == "default" and set(p.scopes) == set(auth.SCOPES)


def test_the_plaintext_is_never_in_the_config_entry():
    token, entry = auth.new_token("alice")
    assert token not in entry
    assert token.split("_", 1)[1] not in entry


def test_a_label_that_would_corrupt_the_entry_is_refused():
    for bad in ("", "has:colon", "has space", "has|pipe", "has=equals"):
        with pytest.raises(auth.AuthError):
            auth.new_token(bad)
