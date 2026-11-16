"""Publish, promote and list profile bundles — the operator's tool.

    python -m abcprint.publish check     <dir>                     # validate only
    python -m abcprint.publish statement <dir> | <name>@<version>  # the bytes to sign
    python -m abcprint.publish publish   <dir> --source URL [--promote] [--deny-list FILE]
                                         [--signature B64 --key-id ID]
    python -m abcprint.publish sign      <name>@<version> --signature B64 --key-id ID
    python -m abcprint.publish promote   <name>@<version> --source URL
    python -m abcprint.publish list      --source URL

`--source` defaults to ABCPRINT_PROFILE_SOURCE (file://… or s3://bucket/prefix;
S3 credentials from ABCPRINT_PROFILE_S3_ACCESS_KEY/_SECRET_KEY[_FILE] and
ABCPRINT_PROFILE_S3_ENDPOINT).

Publishing refuses a bundle that would fail a build later rather than now:

* `profile.yaml` must name an engine this image has, a real version, and a
  schema_version the interpreter understands; `bundle:` rules must parse;
* `requires.fonts` must resolve in the worker's capability record (or ship in
  `fonts/`), and `requires.quarto` must match the worker's quarto;
* every file in `examples/*.zip` must pass the profile's own bundle rules, so a
  profile cannot reject its own example;
* with `--deny-list`, no file may contain any listed term (the privacy check for
  private profiles — the sanitiser's job, moved from the image build to here);
* a thesis-assemble profile must carry its pipeline (`pipeline/thesis-assemble.sh`):
  since 0.18.0 the worker image has none of its own;
* `fonts/` holds only font files and their licence, `fonts_license:` names the
  licence (SPDX), and the families found in the files are recorded;
* `aliases:` may not name another version's content;
* a published version never changes: the same version with other content is refused.

Signing happens on the operator's machine, never in the cluster:
`statement` prints the exact bytes, `openssl pkeyutl -sign -rawin` signs them
with the operator's Ed25519 key, and `publish --signature` (or `sign`, for a
version already published) attaches the result. See nomad/publish-profile.sh.

A new version does not become the default unless `--promote` is given (the very
first version of a profile does).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
import zipfile

from . import bundle_rules, capabilities, profile as profiles, profile_store

SUPPORTED_SCHEMA = 2           # profile.yaml schema versions this interpreter reads
VERSION_RE = re.compile(r"^[0-9][0-9A-Za-z.+-]{0,63}$")
ALIAS_RE = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,63}$")
FONT_EXTS = (".ttf", ".otf", ".ttc", ".otc")
LICENCE_RE = re.compile(r"^(OFL|LICEN[CS]E|COPYING|README)", re.I)
# Licences under which a font may sit in a store others can read.
OPEN_FONT_LICENSES = {"OFL-1.1", "Apache-2.0", "MIT", "Bitstream-Vera", "GPL-2.0-with-font-exception",
                      "LPPL-1.3c", "UFL-1.0"}
ENGINES = ("quarto-render", "thesis-assemble")


class PublishError(RuntimeError):
    pass


def _version_ok(have: str, spec: str) -> bool:
    """`>=1.7.31 <1.8` against a version string; every clause must hold."""
    def key(v: str):
        return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.+-]", v))
    for clause in spec.split():
        m = re.match(r"^(>=|<=|==|>|<)?\s*(.+)$", clause)
        op, v = (m.group(1) or "=="), m.group(2)
        a, b = key(have), key(v)
        ok = {">=": a >= b, "<=": a <= b, "==": a[:len(b)] == b, ">": a > b, "<": a < b}[op]
        if not ok:
            return False
    return True


def check(root: str, *, deny_terms: list[str] | None = None,
          capability_record: dict | None = None) -> profiles.Profile:
    """Everything that can be known before a build. Returns the profile."""
    problems: list[str] = []
    if not os.path.isfile(os.path.join(root, "profile.yaml")):
        raise PublishError(f"{root}: no profile.yaml — a published profile must describe itself")
    try:
        prof = profiles._load_dir(root, os.path.basename(root.rstrip("/")),
                                  name=os.path.basename(root.rstrip("/")))
    except profiles.ProfileError as exc:
        raise PublishError(str(exc)) from None
    if prof.engine not in ENGINES:
        problems.append(f"engine {prof.engine!r} is not one of {list(ENGINES)}")
    if not VERSION_RE.match(prof.version) or prof.version == "unversioned":
        problems.append(f"version {prof.version!r} is not a publishable version (e.g. 0.2 or 1.0.0)")
    if prof.schema_version > SUPPORTED_SCHEMA:
        problems.append(f"schema_version {prof.schema_version} is newer than this image reads "
                        f"({SUPPORTED_SCHEMA}); rebuild the worker first")
    if not re.match(r"^[a-z0-9][a-z0-9-]{0,62}$", prof.name):
        problems.append(f"name {prof.name!r}: lowercase letters, digits and dashes only")
    problems += bundle_rules.validate_schema(prof.bundle, prof.engine)
    for a in prof.aliases:
        if not ALIAS_RE.match(a) or a == prof.version:
            problems.append(f"alias {a!r}: letters, digits, '.', '+', '-'; not the version itself")
    if prof.engine == "thesis-assemble" and not os.path.isfile(
            os.path.join(root, "pipeline", "thesis-assemble.sh")):
        problems.append("a thesis-assemble profile carries its pipeline: "
                        "pipeline/thesis-assemble.sh is missing")
    problems += _check_fonts(root, prof)

    rec = capability_record if capability_record is not None else capabilities.load()
    req = prof.requires or {}
    if rec is not None:
        if not (rec.get("engines_available") or {}).get(prof.engine):
            problems.append(f"the worker image has no {prof.engine} engine")
        if req.get("quarto"):
            have = (rec.get("toolchain") or {}).get("quarto", "")
            if not have or not _version_ok(have, str(req["quarto"])):
                problems.append(f"requires quarto {req['quarto']}; the worker has {have or 'none'}")
        if req.get("fonts"):
            shipped = set().union(*[f["families"] for f in shipped_fonts(root)] or [set()])
            fh = capabilities.font_health([f for f in req["fonts"] if f not in shipped])
            missing = [f for f in fh.get("missing", []) + fh.get("unmeasured", [])]
            if missing:
                problems.append(f"requires fonts neither the worker nor fonts/ provides: {missing}")
    elif req:
        problems.append("requires: … cannot be checked without a worker capability record "
                        f"({capabilities.ENV_VAR})")

    rules = bundle_rules.effective(prof)
    ex_dir = os.path.join(root, "examples")
    for ex in sorted(os.listdir(ex_dir)) if os.path.isdir(ex_dir) else []:
        if not ex.endswith(".zip"):
            continue
        with zipfile.ZipFile(os.path.join(ex_dir, ex)) as zf:
            names = [i.filename for i in zf.infolist() if not i.is_dir()]
            total = sum(i.file_size for i in zf.infolist())
        layout = bundle_rules.layout_of(names)
        if layout == "flat":
            names = ["src/" + n for n in names]
        try:
            bundle_rules.check(names, total, rules, layout=layout)
        except bundle_rules.RuleError as exc:
            problems.append(f"examples/{ex} fails the profile's own bundle rules: {exc}")

    if deny_terms:
        hits = []
        for dirpath, _, files in os.walk(root):
            for f in files:
                p = os.path.join(dirpath, f)
                try:
                    text = open(p, "rb").read().decode("utf-8", "ignore").lower()
                except OSError:
                    continue
                if any(t.lower() in text for t in deny_terms):
                    hits.append(os.path.relpath(p, root))
        if hits:
            problems.append(f"{len(hits)} file(s) contain a denied term (privacy check): "
                            f"{sorted(hits)[:10]}")
    if problems:
        raise PublishError("; ".join(problems))
    return prof


def shipped_fonts(root: str) -> list[dict]:
    """[{file, families, sha256}] for each font file in the bundle's fonts/."""
    import hashlib
    from . import fonts
    out = []
    d = os.path.join(root, "fonts")
    for dirpath, _, files in sorted(os.walk(d)) if os.path.isdir(d) else []:
        for f in sorted(files):
            if not f.lower().endswith(FONT_EXTS):
                continue
            p = os.path.join(dirpath, f)
            try:
                fams = sorted(fonts.families_in_file(p))
            except (ValueError, OSError, IndexError, Exception):   # a corrupt file is a refusal
                fams = []
            out.append({"file": os.path.relpath(p, d), "families": fams,
                        "sha256": hashlib.sha256(open(p, "rb").read()).hexdigest()})
    return out


def _check_fonts(root: str, prof) -> list[str]:
    d = os.path.join(root, "fonts")
    if not os.path.isdir(d):
        return []
    from . import fonts
    problems = []
    stray = [os.path.relpath(os.path.join(dp, f), d) for dp, _, fs in os.walk(d) for f in fs
             if not f.lower().endswith(FONT_EXTS) and not LICENCE_RE.match(f)]
    if stray:
        problems.append(f"fonts/ holds only font files and their licence; also found {sorted(stray)[:5]}")
    shipped = shipped_fonts(root)
    if not shipped:
        problems.append("fonts/ has no .ttf/.otf/.ttc/.otc file")
    for f in shipped:
        if not f["families"]:
            problems.append(f"fonts/{f['file']}: not a readable TrueType/OpenType font")
    if not any(LICENCE_RE.match(f) and not f.upper().startswith("README") for f in os.listdir(d)):
        problems.append("fonts/ must carry its licence text (OFL.txt or LICENSE)")
    if not prof.fonts_license:
        problems.append("fonts/ is present but profile.yaml names no fonts_license (an SPDX id, e.g. OFL-1.1)")
    proprietary = sorted({fam for f in shipped for fam in f["families"]} & set(fonts.SUBSTITUTES))
    if proprietary and prof.fonts_license in OPEN_FONT_LICENSES:
        problems.append(f"fonts/ ships {proprietary}, which are not published under "
                        f"{prof.fonts_license}; name the licence that actually covers them")
    return problems


def example_trees(root: str) -> dict:
    """Each examples/*.zip's file list, so the front door can show a client the shape."""
    out = {}
    ex_dir = os.path.join(root, "examples")
    for ex in sorted(os.listdir(ex_dir)) if os.path.isdir(ex_dir) else []:
        if ex.endswith(".zip"):
            with zipfile.ZipFile(os.path.join(ex_dir, ex)) as zf:
                out[ex] = sorted(i.filename for i in zf.infolist() if not i.is_dir())
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m abcprint.publish", description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("check", "statement", "publish"):
        sp = sub.add_parser(name)
        sp.add_argument("dir")
        sp.add_argument("--deny-list", help="file of terms, one per line, that must not appear")
        sp.add_argument("--capabilities", help="worker capability record (default: $%s)" % capabilities.ENV_VAR)
        if name == "publish":
            sp.add_argument("--source", default=os.environ.get(profile_store.SOURCE_VAR))
            sp.add_argument("--promote", action="store_true", help="make this version the default")
            sp.add_argument("--signature", help="base64 Ed25519 signature over `statement`")
            sp.add_argument("--key-id", help="the signing key's id (see trusted keys)")
    sp = sub.add_parser("sign")
    sp.add_argument("ref")
    sp.add_argument("--signature", required=True)
    sp.add_argument("--key-id", required=True)
    sp.add_argument("--source", default=os.environ.get(profile_store.SOURCE_VAR))
    sp = sub.add_parser("promote")
    sp.add_argument("ref")
    sp.add_argument("--source", default=os.environ.get(profile_store.SOURCE_VAR))
    sp = sub.add_parser("list")
    sp.add_argument("--source", default=os.environ.get(profile_store.SOURCE_VAR))
    a = ap.parse_args(argv)

    try:
        if a.cmd == "statement" and "@" in a.dir and not os.path.isdir(a.dir):
            # A version already published: sign what the index recorded.
            src = getattr(a, "source", None) or os.environ.get(profile_store.SOURCE_VAR)
            if not src:
                raise PublishError(f"no {profile_store.SOURCE_VAR}")
            name, _, version = a.dir.partition("@")
            rec = ((profile_store.Store(src).index(fresh=True)["profiles"].get(name) or {})
                   .get("versions") or {}).get(version)
            if rec is None:
                raise PublishError(f"{a.dir} is not published")
            sys.stdout.buffer.write(profile_store.statement(name, version, rec["digest"]))
            return 0
        if a.cmd in ("check", "statement", "publish"):
            terms = None
            if a.deny_list:
                terms = [t.strip() for t in open(a.deny_list) if t.strip() and not t.startswith("#")]
            rec = json.load(open(a.capabilities)) if a.capabilities else None
            prof = check(a.dir, deny_terms=terms, capability_record=rec)
            digest = profile_store.dir_digest(a.dir)
            if a.cmd == "check":
                print(f"OK {prof.ref} {digest}")
                return 0
            if a.cmd == "statement":
                sys.stdout.buffer.write(profile_store.statement(prof.name, prof.version, digest))
                return 0
            if not a.source:
                raise PublishError(f"no --source and no {profile_store.SOURCE_VAR}")
            descriptor = prof.as_dict()
            for k in ("digest", "source", "published_at"):
                descriptor.pop(k, None)
            examples = example_trees(a.dir)
            if examples:
                descriptor["bundle"] = {**(descriptor.get("bundle") or {}), "examples": examples}
            shipped = shipped_fonts(a.dir)
            if shipped:
                descriptor["fonts_shipped"] = shipped
            sig = _signature(a)
            r = profile_store.Store(a.source).publish(a.dir, descriptor, promote=a.promote,
                                                      signature=sig)
            idx = profile_store.Store(a.source).index(fresh=True)["profiles"][r.name]
            print(f"PUBLISHED {r.name}@{r.version} {r.digest} -> {a.source}/{r.key}"
                  f"  (default: {idx['default']})")
            return 0
        if not a.source:
            raise PublishError(f"no --source and no {profile_store.SOURCE_VAR}")
        st = profile_store.Store(a.source)
        if a.cmd == "sign":
            name, _, version = a.ref.partition("@")
            if not version:
                raise PublishError("sign takes name@version")
            st.sign(name, version, _signature(a))
            print(f"SIGNED {name}@{version} key {a.key_id}")
            return 0
        if a.cmd == "promote":
            name, _, version = a.ref.partition("@")
            if not version:
                raise PublishError("promote takes name@version")
            st.promote(name, version)
            print(f"DEFAULT {name} -> {version}")
            return 0
        for name, e in sorted(st.index(fresh=True)["profiles"].items()):
            for v, r in sorted(e["versions"].items()):
                mark = "*" if v == e.get("default") else " "
                try:
                    sig = profile_store.check_signature(name, v, r)
                except profile_store.StoreError:
                    sig = "unchecked"
                al = ",".join((r.get("descriptor") or {}).get("aliases") or [])
                print(f"{mark} {name}@{v}  {r['digest']}  {r['engine']}  {r.get('published_at')}"
                      f"  sig:{sig}" + (f"  aliases:{al}" if al else ""))
        return 0
    except (PublishError, profile_store.StoreError) as exc:
        print(f"REFUSED: {exc}", file=sys.stderr)
        return 1


def _signature(a) -> dict | None:
    if not getattr(a, "signature", None):
        if getattr(a, "key_id", None):
            raise PublishError("--key-id without --signature")
        return None
    if not a.key_id:
        raise PublishError("--signature needs --key-id")
    return {"alg": "ed25519", "key_id": a.key_id, "sig": a.signature}


if __name__ == "__main__":
    sys.exit(main())
