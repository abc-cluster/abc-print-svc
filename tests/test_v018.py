"""0.18.0: su-fmhs (and its pipeline) as a bundle, aliases for old pins, fonts in a
bundle, and signed profiles."""
import base64
import os
import struct

import pytest

from abcprint import compile as compiler, fonts, profile as profiles, profile_store, publish, workspace
from conftest import garden_thesis_zip, h, wait_terminal
from test_profile_store import _mkprofile, store_env  # noqa: F401  (fixture)

THESIS_YAML = """schema_version: 2
name: su-fmhs
version: "{v}"
engine: thesis-assemble
aliases: [{aliases}]
"""


def _thesis_bundle(root, version="1.0.0", aliases=("git-d0e8657", "unversioned"), pipeline=True):
    os.makedirs(os.path.join(root, "_extensions", "sun-thesis"), exist_ok=True)
    with open(os.path.join(root, "profile.yaml"), "w") as fh:
        fh.write(THESIS_YAML.format(v=version, aliases=", ".join(f'"{a}"' for a in aliases)))
    with open(os.path.join(root, "_extensions", "sun-thesis", "typst-template.typ"), "w") as fh:
        fh.write(f"// template {version}\n")
    if pipeline:
        os.makedirs(os.path.join(root, "pipeline"), exist_ok=True)
        with open(os.path.join(root, "pipeline", "thesis-assemble.sh"), "w") as fh:
            fh.write("#!/bin/sh\nexit 0\n")
        with open(os.path.join(root, "pipeline", "PROVENANCE.json"), "w") as fh:
            fh.write('{"revision": "d0e8657"}')
    return str(root)


# --- a fake font: an sfnt with only a `name` table ---------------------------

def _font(path, family):
    name = family.encode("utf-16-be")
    records = [(3, 1, 0x409, 1, len(name), 0)]
    table = struct.pack(">HHH", 0, len(records), 6 + 12 * len(records))
    table += b"".join(struct.pack(">HHHHHH", *r) for r in records) + name
    head = struct.pack(">IHHHH", 0x00010000, 1, 16, 0, 0)
    entry = struct.pack(">4sIII", b"name", 0, 12 + 16, len(table))
    with open(path, "wb") as fh:
        fh.write(head + entry + table)


def test_the_family_is_read_from_the_font_file(tmp_path):
    _font(tmp_path / "x.ttf", "Libre Test Sans")
    assert fonts.families_in_file(str(tmp_path / "x.ttf")) == {"Libre Test Sans"}
    (tmp_path / "bad.ttf").write_bytes(b"not a font")
    with pytest.raises(ValueError):
        fonts.families_in_file(str(tmp_path / "bad.ttf"))


# --- the thesis pipeline travels in the profile -----------------------------------

def test_a_thesis_profile_must_carry_its_pipeline(tmp_path):
    publish.check(_thesis_bundle(tmp_path / "ok"), capability_record={
        "engines_available": {"thesis-assemble": True}})
    with pytest.raises(publish.PublishError, match="pipeline/thesis-assemble.sh"):
        publish.check(_thesis_bundle(tmp_path / "no", pipeline=False),
                      capability_record={"engines_available": {"thesis-assemble": True}})


def test_the_workspace_takes_the_pipeline_from_the_bundle_not_the_image(tmp_path, monkeypatch):
    monkeypatch.setattr(workspace, "PIPELINE_DIR", str(tmp_path / "no-image-pipeline"))
    b = _thesis_bundle(tmp_path / "b")
    paths = workspace.create(str(tmp_path / "ws"), profile_dir=b)
    assert os.path.isfile(os.path.join(paths["bin"], "thesis-assemble.sh"))
    # Bundle furniture never lands in the template the pipeline reads.
    q = os.listdir(paths["quarto"])
    assert "_extensions" in q and not {"profile.yaml", "pipeline", "examples"} & set(q)
    assert compiler.toolchain.pipeline_provenance(paths["bin"])["revision"] == "d0e8657"
    with pytest.raises(workspace.BundleError, match="carries no thesis pipeline"):
        workspace.create(str(tmp_path / "ws2"), profile_dir=_thesis_bundle(tmp_path / "n", pipeline=False))


# --- aliases: a pin recorded before publishing still resolves ----------------------

def test_an_old_pin_resolves_through_an_alias(store_env, tmp_path):
    publish.main(["publish", _thesis_bundle(tmp_path / "t"), "--source", store_env])
    st = profile_store.Store(store_env)
    for pin in ("su-fmhs@git-d0e8657", "su-fmhs@unversioned", "su-fmhs@1.0.0", "su-fmhs"):
        r = st.resolve(pin)
        assert (r.name, r.version) == ("su-fmhs", "1.0.0"), pin
    assert st.resolve("su-fmhs@git-0000000") is None
    # An alias may not name a second version's content.
    clash = _thesis_bundle(tmp_path / "t2", version="1.0.1", aliases=("git-d0e8657",))
    assert publish.main(["publish", clash, "--source", store_env]) == 1


def test_a_run_pinned_by_alias_records_the_real_version(store_env, svc, tmp_path, monkeypatch):
    publish.main(["publish", _thesis_bundle(tmp_path / "t"), "--source", store_env])
    client, _, t_a, _ = svc
    z = garden_thesis_zip(tmp_path / "t.zip")
    up = client.post("/v1/uploads", headers=h(t_a), files={"file": ("t.zip", z.read_bytes())}).json()
    r = client.post("/v1/thesis/runs", headers=h(t_a),
                    json={"bundle": up["upload_id"], "profile": "su-fmhs@git-d0e8657"})
    assert r.status_code == 202, r.text
    wait_terminal(client, t_a, r.json()["run_id"])
    run = client.get(f"/v1/thesis/runs/{r.json()['run_id']}", headers=h(t_a)).json()
    assert run["outcome"]["options"]["profile"] == "su-fmhs@1.0.0"
    assert run["outcome"]["manifest"]["profile"]["source"] == store_env


# --- fonts in a bundle ---------------------------------------------------------------

def _with_fonts(root, family="Libre Test Sans", license_file="OFL.txt", spdx="OFL-1.1"):
    d = _mkprofile(root, "0.5", body=f"fonts_license: {spdx}\n" if spdx else "")
    os.makedirs(os.path.join(d, "fonts"), exist_ok=True)
    _font(os.path.join(d, "fonts", "LibreTest.ttf"), family)
    if license_file:
        with open(os.path.join(d, "fonts", license_file), "w") as fh:
            fh.write("SIL Open Font License 1.1")
    return d


def test_shipped_fonts_are_checked_and_recorded(tmp_path, store_env):
    d = _with_fonts(tmp_path / "ok")
    publish.check(d, capability_record={"engines_available": {"quarto-render": True}})
    assert publish.shipped_fonts(d)[0]["families"] == ["Libre Test Sans"]
    publish.main(["publish", d, "--source", store_env])
    rec = profile_store.Store(store_env).resolve("biorxiv-dev").entry
    assert rec["descriptor"]["fonts_shipped"][0]["families"] == ["Libre Test Sans"]
    assert rec["descriptor"]["fonts_license"] == "OFL-1.1"
    cap = {"engines_available": {"quarto-render": True}}
    with pytest.raises(publish.PublishError, match="licence text"):
        publish.check(_with_fonts(tmp_path / "nolic", license_file=None), capability_record=cap)
    with pytest.raises(publish.PublishError, match="fonts_license"):
        publish.check(_with_fonts(tmp_path / "nospdx", spdx=None), capability_record=cap)
    with pytest.raises(publish.PublishError, match="not published under OFL-1.1"):
        publish.check(_with_fonts(tmp_path / "calibri", family="Calibri"), capability_record=cap)


def test_requires_fonts_is_satisfied_by_the_bundle(tmp_path):
    d = _with_fonts(tmp_path / "f")
    with open(os.path.join(d, "profile.yaml"), "a") as fh:
        fh.write("requires: {fonts: ['Libre Test Sans']}\n")
    publish.check(d, capability_record={"engines_available": {"quarto-render": True},
                                        "fonts": {"health": {"resolutions": [], "missing": []}}})


def test_a_build_puts_the_profile_fonts_on_the_render_font_path(tmp_path):
    d = _with_fonts(tmp_path / "f")
    paths = workspace.create_quarto(str(tmp_path / "ws"), d)
    assert paths["font_dirs"] and os.path.isfile(os.path.join(paths["font_dirs"][0], "LibreTest.ttf"))
    assert compiler.font_env(paths)["TYPST_FONT_PATHS"].split(os.pathsep)[0] == paths["font_dirs"][0]
    assert "fonts" not in os.listdir(paths["src"])


# --- signatures ----------------------------------------------------------------------

@pytest.fixture
def keypair():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    k = Ed25519PrivateKey.generate()
    raw = k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return k, base64.b64encode(raw).decode(), profile_store.key_id(raw)


def _sign(k, kid, name, version, digest):
    return ["--signature", base64.b64encode(k.sign(profile_store.statement(name, version, digest))).decode(),
            "--key-id", kid]


def test_signed_publishing_and_the_require_policy(store_env, tmp_path, keypair, monkeypatch, capsys):
    k, pub, kid = keypair
    monkeypatch.setenv(profile_store.KEYS_VAR, pub)          # set => require
    d = _mkprofile(tmp_path / "p", "0.7")
    assert publish.main(["publish", d, "--source", store_env]) == 1          # unsigned: refused
    assert "unsigned" in capsys.readouterr().err
    digest = profile_store.dir_digest(d)
    bad = _sign(k, kid, "biorxiv-dev", "0.8", digest)                        # wrong version signed
    assert publish.main(["publish", d, "--source", store_env, *bad]) == 1
    good = _sign(k, kid, "biorxiv-dev", "0.7", digest)
    assert publish.main(["publish", d, "--source", store_env, *good]) == 0
    r = profile_store.Store(store_env).resolve("biorxiv-dev@0.7")
    assert r.signature == "valid"
    # The statement subcommand prints exactly the signed bytes.
    capsys.readouterr()
    assert publish.main(["statement", d]) == 0
    assert capsys.readouterr().out.encode() == profile_store.statement("biorxiv-dev", "0.7", digest)


def test_an_unsigned_version_is_refused_under_require_and_can_be_signed_later(
        store_env, svc, tmp_path, keypair, monkeypatch):
    k, pub, kid = keypair
    d = _mkprofile(tmp_path / "p", "0.7")
    publish.main(["publish", d, "--source", store_env])                       # before keys existed
    monkeypatch.setenv(profile_store.KEYS_VAR, pub)
    client, _, t_a, _ = svc
    from conftest import garden_manuscript_zip
    z = garden_manuscript_zip(tmp_path / "m.zip")
    up = client.post("/v1/uploads", headers=h(t_a), files={"file": ("m.zip", z.read_bytes())}).json()
    r = client.post("/v1/manuscript/runs", headers=h(t_a),
                    json={"bundle": up["upload_id"], "profile": "biorxiv-dev@0.7"})
    assert r.status_code == 503 and r.json()["error"] == "profile_untrusted", r.text
    sig = _sign(k, kid, "biorxiv-dev", "0.7", profile_store.dir_digest(d))
    assert publish.main(["sign", "biorxiv-dev@0.7", "--source", store_env, *sig]) == 0
    r = client.post("/v1/manuscript/runs", headers=h(t_a),
                    json={"bundle": up["upload_id"], "profile": "biorxiv-dev@0.7"})
    assert r.status_code == 202, r.text


def test_a_signature_from_an_untrusted_key_is_not_enough(store_env, tmp_path, keypair, monkeypatch):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    k, pub, kid = keypair
    other = Ed25519PrivateKey.generate()
    d = _mkprofile(tmp_path / "p", "0.7")
    sig = _sign(other, "0123456789abcdef", "biorxiv-dev", "0.7", profile_store.dir_digest(d))
    monkeypatch.setenv(profile_store.KEYS_VAR, pub)
    assert publish.main(["publish", d, "--source", store_env, *sig]) == 1
    monkeypatch.setenv(profile_store.SIG_POLICY_VAR, "warn")
    assert publish.main(["publish", d, "--source", store_env]) == 0          # warn: accepted
    assert profile_store.Store(store_env).resolve("biorxiv-dev@0.7").signature == "unsigned"


def test_the_mirror_carries_a_signature_added_after_mirroring(store_env, tmp_path, keypair, monkeypatch):
    k, pub, kid = keypair
    d = _mkprofile(tmp_path / "p", "0.7")
    publish.main(["publish", d, "--source", store_env])
    st = profile_store.Store(store_env, ttl_s=0)
    mir = profile_store.Store(f"file://{tmp_path / 'mirror'}", ttl_s=0)
    st.mirror_into(st.resolve("biorxiv-dev@0.7"), mir)
    sig = _sign(k, kid, "biorxiv-dev", "0.7", profile_store.dir_digest(d))
    monkeypatch.setenv(profile_store.KEYS_VAR, pub)
    monkeypatch.setenv(profile_store.SIG_POLICY_VAR, "warn")     # the old mirror entry is unsigned
    assert mir.resolve("biorxiv-dev@0.7").signature == "unsigned"
    assert publish.main(["sign", "biorxiv-dev@0.7", "--source", store_env, *sig]) == 0
    st.mirror_into(st.resolve("biorxiv-dev@0.7"), mir)
    assert mir.resolve("biorxiv-dev@0.7").signature == "valid"


def test_a_signature_added_later_is_seen_without_waiting_for_the_index_ttl(
        store_env, tmp_path, keypair, monkeypatch):
    k, pub, kid = keypair
    d = _mkprofile(tmp_path / "p", "0.7")
    publish.main(["publish", d, "--source", store_env])
    monkeypatch.setenv(profile_store.KEYS_VAR, pub)
    st = profile_store.Store(store_env, ttl_s=3600)          # a long-lived cached index
    with pytest.raises(profile_store.SignatureError):
        st.resolve("biorxiv-dev@0.7")
    sig = _sign(k, kid, "biorxiv-dev", "0.7", profile_store.dir_digest(d))
    assert publish.main(["sign", "biorxiv-dev@0.7", "--source", store_env, *sig]) == 0
    assert st.resolve("biorxiv-dev@0.7").signature == "valid"
