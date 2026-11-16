"""Profiles: the rulebook as data, and the escape hatch that must not undo it.

A profile selects an ENGINE and configures it. That is not the shape this started
with — the assumption was one engine with a swappable presentation layer — but the
bioRxiv path needs none of the thesis engine (no measure pass, no page reservation,
no splice, no nocite union, no front-matter ordering), so selecting is the honest
verb.

`quarto_metadata` is a deliberate escape hatch for advanced users: arbitrary keys
merged into the Quarto metadata the render sees. Arbitrary is the point and also the
danger, so a refused set is enforced rather than documented. Everything in that set
either executes code or reads a path the client chose, which are the two things the
threat model says are never configurable.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

import yaml

from . import profile_store

PROFILE_ROOT = os.environ.get("ABCPRINT_PROFILES", "/srv/profiles")
# The front door's view of the profiles: the descriptors exported from the worker
# image when it was built (abcprint.capabilities), and nothing else. The front door
# validates requests against a profile but never renders with one, so it carries
# no profile directory — and so no template or front matter that could leak.
REGISTRY_FILE = os.environ.get("ABCPRINT_PROFILE_REGISTRY", "")

# Keys refused inside `quarto_metadata`, with the reason each one is refused.
# Checked against the top-level key AND nested under `format:`, because Quarto
# accepts most of these in both places.
REFUSED_METADATA: dict[str, str] = {
    "execute":            "runs code chunks",
    "engine":             "selects a code-execution engine (knitr/jupyter)",
    "jupyter":            "runs code chunks",
    "knitr":              "runs code chunks",
    "filters":            "runs client-supplied Lua",
    "shortcodes":         "runs client-supplied Lua",
    "pre-render":         "runs a shell command",
    "post-render":        "runs a shell command",
    "project":            "can carry pre-render/post-render hooks",
    "template":           "reads a client-chosen template path",
    "template-partials":  "reads client-chosen template paths",
    "include-in-header":  "injects a client-chosen file",
    "include-before-body": "injects a client-chosen file",
    "include-after-body": "injects a client-chosen file",
    "resource-path":      "widens what the render may read",
    "pdf-engine":         "selects an arbitrary binary",
    "pdf-engine-opt":     "passes arbitrary flags to the engine",
    "output-file":        "controls where output is written",
    "output-dir":         "controls where output is written",
    "extension-dir":      "reads a client-chosen extension path",
}


class ProfileError(ValueError):
    pass


@dataclass
class Profile:
    name: str
    version: str
    engine: str
    schema_version: int = 1
    description: str = ""
    template: dict = field(default_factory=dict)
    page: dict = field(default_factory=dict)
    fonts: dict = field(default_factory=dict)
    bibliography: dict = field(default_factory=dict)
    layout: dict = field(default_factory=dict)
    rules: dict = field(default_factory=dict)
    checks: dict = field(default_factory=dict)
    open_to_build: list = field(default_factory=list)
    quarto_metadata: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict)
    # Where this version came from, and the content digest that identifies it.
    bundle: dict = field(default_factory=dict)        # the client-zip rules (bundle_rules)
    requires: dict = field(default_factory=dict)
    digest: str = ""
    source: str = "baked"                             # "baked" or the store's URL
    published_at: str | None = None

    @property
    def ref(self) -> str:
        return f"{self.name}@{self.version}"

    @property
    def has_compliance_rules(self) -> bool:
        """A profile with no rules must not report `compliant: true`.

        biorxiv-dev defines no compliance surface by design, so its jobs report
        compliance as null. Saying `true` because there was nothing to check would
        be a claim the profile cannot support.
        """
        return bool(self.rules)

    def as_dict(self) -> dict:
        return {"name": self.name, "version": self.version, "ref": self.ref,
                "schema_version": self.schema_version, "engine": self.engine,
                "description": self.description, "template": self.template,
                "page": self.page, "fonts": self.fonts,
                "bibliography": self.bibliography, "layout": self.layout,
                "rules": self.rules, "checks": self.checks,
                "open_to_build": self.open_to_build,
                "has_compliance_rules": self.has_compliance_rules,
                "bundle": self.bundle, "requires": self.requires,
                "digest": self.digest, "source": self.source,
                "published_at": self.published_at}

    def provenance(self) -> dict:
        """What a run records about the rulebook it was built with."""
        return {"name": self.name, "version": self.version, "ref": self.ref,
                "digest": self.digest, "source": self.source,
                "published_at": self.published_at, "engine": self.engine,
                "schema_version": self.schema_version}


def _resolve_dir(ref: str) -> str:
    """Resolve a profile reference to a directory, by NAME — never by path.

    `profile: ../../anything` would be a file-read primitive, so references are
    looked up in the registry and anything containing a separator is refused.
    """
    name = _checked_name(ref)
    st = profile_store.default()
    if st is not None:
        r = st.resolve(ref)
        if r is not None:
            try:
                return st.materialize(r)
            except profile_store.StoreError as exc:
                raise ProfileError(str(exc)) from None
    path = os.path.join(PROFILE_ROOT, name)
    if not os.path.isdir(path):
        raise ProfileError(
            f"unknown profile {name!r}; available: {', '.join(available()) or 'none'}")
    return path


def _registry() -> dict | None:
    if not REGISTRY_FILE:
        return None
    import json
    with open(REGISTRY_FILE) as fh:
        return json.load(fh)["profiles"]


def _checked_name(ref: str) -> str:
    name = ref.split("@", 1)[0]
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ProfileError(f"invalid profile reference {ref!r}")
    return name


def _baked_names() -> list[str]:
    reg = _registry()
    if reg is not None:
        return sorted(reg)
    if not os.path.isdir(PROFILE_ROOT):
        return []
    return sorted(d for d in os.listdir(PROFILE_ROOT)
                  if os.path.isdir(os.path.join(PROFILE_ROOT, d)))


def available() -> list[str]:
    """Published profiles and the ones baked into the image, by name."""
    st = profile_store.default()
    published = st.names() if st is not None else []
    return sorted(set(published) | set(_baked_names()))


def _from_descriptor(ref: str, d: dict) -> Profile:
    if "error" in d:
        raise ProfileError(d["error"])
    want = ref.split("@", 1)[1] if "@" in ref else None
    # `unversioned` was what the legacy SU/FMHS profile reported before 0.17.0; a
    # client still pinning it gets the same profile, now with its real version.
    if want and want != d["version"] and not (want == "unversioned" and d["version"].startswith("git-")):
        raise ProfileError(
            f"profile {ref!r} requested but the registry holds version {d['version']!r}")
    fields = ("schema_version", "description", "template", "page", "fonts", "bibliography",
              "layout", "rules", "checks", "open_to_build", "bundle", "requires", "digest",
              "source", "published_at")
    return Profile(name=d["name"], version=d["version"], engine=d["engine"],
                   **{k: d[k] for k in fields if k in d and d[k] is not None})


def load(ref: str) -> Profile:
    """A profile by reference: `name` (its default version) or `name@version`.

    A published bundle answers first. In the front door (which has a registry and
    no profile directories) that is the index's descriptor; in the worker it is
    the fetched, digest-verified bundle itself. Otherwise the profile baked into
    the image answers, as before.
    """
    name = _checked_name(ref)
    st = profile_store.default()
    if st is not None:
        try:
            r = st.resolve(ref)
        except profile_store.StoreError as exc:
            raise ProfileError(f"profile store unavailable: {exc}") from None
        if r is not None:
            meta = {"digest": r.digest, "source": r.source,
                    "published_at": r.entry.get("published_at")}
            if _registry() is not None:
                return _from_descriptor(f"{r.name}@{r.version}", {**r.entry["descriptor"], **meta})
            try:
                d = st.materialize(r)
            except profile_store.StoreError as exc:
                raise ProfileError(str(exc)) from None
            prof = _load_dir(d, f"{r.name}@{r.version}", name=r.name)
            prof.digest, prof.source, prof.published_at = meta["digest"], meta["source"], meta["published_at"]
            return prof
    reg = _registry()
    if reg is not None:
        if name not in reg:
            raise ProfileError(
                f"unknown profile {name!r}; available: {', '.join(sorted(reg)) or 'none'}")
        return _from_descriptor(ref, reg[name])
    d = _resolve_dir(ref)
    prof = _load_dir(d, ref, name=os.path.basename(d))
    prof.digest = _baked_digest(d)
    return prof


_DIGESTS: dict[str, str] = {}


def _baked_digest(d: str) -> str:
    """A baked profile cannot change while the process runs: hash it once."""
    if d not in _DIGESTS:
        _DIGESTS[d] = profile_store.dir_digest(d)
    return _DIGESTS[d]


def _legacy_version(d: str) -> str:
    """A version for a profile with no profile.yaml: the revision it was vendored from."""
    import json
    try:
        with open(os.path.join(d, "PROVENANCE.json")) as fh:
            rev = json.load(fh).get("revision")
        return f"git-{rev}" if rev else "unversioned"
    except (OSError, ValueError):
        return "unversioned"


def _load_dir(d: str, ref: str, *, name: str) -> Profile:
    path = os.path.join(d, "profile.yaml")
    if not os.path.isfile(path):
        # A legacy profile directory with no profile.yaml is the SU/FMHS one,
        # whose rulebook still lives inside its template. Describe it honestly
        # rather than inventing a schema it does not have; its version is the
        # revision it was vendored from, so a build can still say which one.
        version = _legacy_version(d)
        want = ref.split("@", 1)[1] if "@" in ref else None
        if want and want not in (version, "unversioned"):
            raise ProfileError(f"profile {ref!r} requested but the image holds version {version!r}")
        return Profile(name=name, version=version, engine="thesis-assemble",
                       description="legacy profile; rules still embedded in the template")
    with open(path) as fh:
        raw = yaml.safe_load(fh) or {}
    if "engine" not in raw:
        raise ProfileError(f"profile {ref!r} does not declare an engine")
    want = ref.split("@", 1)[1] if "@" in ref else None
    got = str(raw.get("version", ""))
    if want and got and want != got:
        raise ProfileError(
            f"profile {ref!r} requested but the registry holds version {got!r}")
    return Profile(
        name=raw.get("name", name), version=got or "unversioned",
        engine=raw["engine"], schema_version=int(raw.get("schema_version", 1)),
        description=raw.get("description", ""), template=raw.get("template", {}) or {},
        page=raw.get("page", {}) or {}, fonts=raw.get("fonts", {}) or {},
        bibliography=raw.get("bibliography", {}) or {},
        layout=raw.get("layout", {}) or {}, rules=raw.get("rules", {}) or {},
        checks=raw.get("checks", {}) or {},
        open_to_build=raw.get("open_to_build", []) or [],
        quarto_metadata=raw.get("quarto_metadata", {}) or {}, raw=raw,
        bundle=raw.get("bundle", {}) or {}, requires=raw.get("requires", {}) or {})


def stage_for_worker(ref: str) -> None:
    """Make a pinned, published version readable by the worker (front door only).

    A no-op without a store or without a mirror: baked profiles are already in
    the worker image, and an in-process build reads the store directly.
    """
    st, mir = profile_store.default(), profile_store.mirror()
    if st is None or mir is None:
        return
    try:
        r = st.resolve(ref)
        if r is not None:
            st.mirror_into(r, mir)
    except profile_store.StoreError as exc:
        raise ProfileError(f"could not stage profile {ref} for the worker: {exc}") from None


def check_quarto_metadata(meta: dict) -> list[str]:
    """Return the reasons a `quarto_metadata` block is refused, if any.

    Checked at the top level and one level under `format:`, because Quarto accepts
    most of these in both. Refusal is by key name rather than by value: a key like
    `filters` is dangerous whatever it is set to.
    """
    problems: list[str] = []
    if not isinstance(meta, dict):
        return ["quarto_metadata must be a mapping"]

    def scan(d: dict, where: str) -> None:
        for k in d:
            reason = REFUSED_METADATA.get(str(k))
            if reason:
                problems.append(f"{where}{k}: refused because it {reason}")

    scan(meta, "quarto_metadata.")
    fmt = meta.get("format")
    if isinstance(fmt, dict):
        for fname, fbody in fmt.items():
            if isinstance(fbody, dict):
                scan(fbody, f"quarto_metadata.format.{fname}.")
    return problems
