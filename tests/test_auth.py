"""The gate, and the isolation that makes the gate worth having.

This is a regression suite for a live exposure, not a hypothetical. Until
2026-10-19 the service was reachable unauthenticated on the public internet,
`GET /jobs` enumerated every caller's jobs, and every artifact was fetchable by
id — so one caller could and did download another's compiled document. Two
things had to be true to close it, and only one of them is a lock on the door:

  1. a request without a recognised token is refused, and
  2. a request WITH a recognised token sees only its own jobs.

A shared secret would have satisfied (1) and left (2) exactly as broken.
"""
import pytest
from fastapi.testclient import TestClient

from abcprint import auth, jobs


@pytest.fixture
def two_tokens(monkeypatch):
    """Two tokens with different scopes, and a client that honours them."""
    t_a, e_a = auth.new_token("alice", ("compile", "read", "deliver"))
    t_b, e_b = auth.new_token("bob", ("compile", "read"))
    monkeypatch.setenv(auth.ENV_VAR, f"{e_a}|{e_b}")
    monkeypatch.delenv(auth.DISABLE_VAR, raising=False)
    import importlib

    from abcprint import api
    importlib.reload(api)
    jobs.STORE = jobs.Store()
    api.jobs.STORE = jobs.STORE
    return TestClient(api.app, raise_server_exceptions=False), t_a, t_b


def h(tok):
    return {"Authorization": f"Bearer {tok}"}


# --- (1) the door ----------------------------------------------------------

def test_health_is_open_because_the_probe_needs_it(two_tokens):
    client, _, _ = two_tokens
    r = client.get("/health")
    assert r.status_code in (200, 503)
    # And it says what the gate is doing, so an open deployment is visible.
    assert r.json()["auth"]["mode"] == "token"
    assert r.json()["auth"]["tokens"] == 2


@pytest.mark.parametrize("path", ["/jobs", "/profiles", "/toolchain", "/fonts"])
def test_no_token_is_refused(two_tokens, path):
    client, _, _ = two_tokens
    r = client.get(path)
    assert r.status_code == 401
    assert "bearer token" in r.json()["detail"].lower()
    assert r.headers.get("WWW-Authenticate") == "Bearer"


@pytest.mark.parametrize("header", [
    "", "Bearer", "Bearer ", "Basic abcp_x", "abcp_x", "Bearer abcp_wrong",
])
def test_a_bad_authorization_header_is_refused(two_tokens, header):
    client, _, _ = two_tokens
    assert client.get("/jobs", headers={"Authorization": header}).status_code == 401


def test_a_valid_token_gets_in(two_tokens):
    client, t_a, _ = two_tokens
    assert client.get("/jobs", headers=h(t_a)).status_code == 200


def test_a_scope_it_does_not_hold_is_403_not_401(two_tokens):
    """403 says "fix your permissions"; 401 says "fix your token". Different jobs."""
    client, _, t_b = two_tokens                 # bob has no deliver scope
    r = client.get("/delivery/destinations", headers=h(t_b))
    assert r.status_code == 403
    assert "deliver" in r.json()["detail"]


def test_a_refusal_does_not_disclose_which_tokens_exist(two_tokens):
    client, _, _ = two_tokens
    body = client.get("/jobs", headers={"Authorization": "Bearer abcp_wrong"}).text
    for leak in ("alice", "bob", "sha256"):
        assert leak not in body


# --- (2) the isolation, which is the part a shared secret would have missed --

def test_a_token_sees_only_its_own_jobs(two_tokens):
    client, t_a, t_b = two_tokens
    a = jobs.STORE.create({"copy": "examination"}, owner="alice")
    b = jobs.STORE.create({"copy": "examination"}, owner="bob")

    seen_a = [j["id"] for j in client.get("/jobs", headers=h(t_a)).json()["jobs"]]
    seen_b = [j["id"] for j in client.get("/jobs", headers=h(t_b)).json()["jobs"]]
    assert seen_a == [a.id]
    assert seen_b == [b.id]


def test_another_tokens_job_is_404_not_403(two_tokens):
    """404, so the response does not confirm that the id exists."""
    client, t_a, _ = two_tokens
    b = jobs.STORE.create({"copy": "examination"}, owner="bob")
    assert client.get(f"/jobs/{b.id}", headers=h(t_a)).status_code == 404


def test_another_tokens_artifact_cannot_be_downloaded(two_tokens):
    """The exact call that leaked: GET /jobs/<id>/artifacts/<name> by id alone."""
    client, t_a, _ = two_tokens
    b = jobs.STORE.create({"copy": "examination"}, owner="bob")
    b.artifacts = [{"name": "thesis-assembled.pdf", "bytes": 1}]
    r = client.get(f"/jobs/{b.id}/artifacts/thesis-assembled.pdf", headers=h(t_a))
    assert r.status_code == 404


def test_a_neighbours_volume_cannot_push_out_your_own_jobs(two_tokens):
    """The limit applies after the ownership filter, not before.

    Filtering a pre-limited window would make a caller's own jobs vanish from
    their list whenever a busier token ran more builds — a quiet wrong answer.
    """
    client, t_a, _ = two_tokens
    mine = jobs.STORE.create({}, owner="alice")
    for _ in range(60):
        jobs.STORE.create({}, owner="bob")
    assert [j["id"] for j in client.get("/jobs", headers=h(t_a)).json()["jobs"]] == [mine.id]


# --- configuration ---------------------------------------------------------

def test_no_tokens_configured_refuses_everything_rather_than_allowing_it(monkeypatch):
    """Fail closed. The original defect was a service that failed open."""
    monkeypatch.delenv(auth.ENV_VAR, raising=False)
    monkeypatch.delenv(auth.DISABLE_VAR, raising=False)
    import importlib

    from abcprint import api
    importlib.reload(api)
    client = TestClient(api.app, raise_server_exceptions=False)
    r = client.get("/jobs")
    assert r.status_code == 503
    assert auth.ENV_VAR in r.json()["detail"]
    assert client.get("/health").json()["auth"]["mode"] == "no-tokens"


def test_auth_can_be_switched_off_but_says_so_loudly(monkeypatch):
    monkeypatch.delenv(auth.ENV_VAR, raising=False)
    monkeypatch.setenv(auth.DISABLE_VAR, "disabled")
    import importlib

    from abcprint import api
    importlib.reload(api)
    client = TestClient(api.app, raise_server_exceptions=False)
    assert client.get("/jobs").status_code == 200
    state = client.get("/health").json()["auth"]
    assert state["mode"] == "disabled"
    assert "warning" in state                  # never silently open


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
