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

PROFILE_ROOT = os.environ.get("ABCPRINT_PROFILES", "/srv/profiles")

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
                "has_compliance_rules": self.has_compliance_rules}


def _resolve_dir(ref: str) -> str:
    """Resolve a profile reference to a directory, by NAME — never by path.

    `profile: ../../anything` would be a file-read primitive, so references are
    looked up in the registry and anything containing a separator is refused.
    """
    name = ref.split("@", 1)[0]
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise ProfileError(f"invalid profile reference {ref!r}")
    path = os.path.join(PROFILE_ROOT, name)
    if not os.path.isdir(path):
        raise ProfileError(
            f"unknown profile {name!r}; available: {', '.join(available()) or 'none'}")
    return path


def available() -> list[str]:
    if not os.path.isdir(PROFILE_ROOT):
        return []
    return sorted(d for d in os.listdir(PROFILE_ROOT)
                  if os.path.isdir(os.path.join(PROFILE_ROOT, d)))


def load(ref: str) -> Profile:
    d = _resolve_dir(ref)
    path = os.path.join(d, "profile.yaml")
    if not os.path.isfile(path):
        # A legacy profile directory with no profile.yaml is the SU/FMHS one,
        # whose rulebook still lives inside its template. Describe it honestly
        # rather than inventing a schema it does not have.
        return Profile(name=os.path.basename(d), version="unversioned",
                       engine="thesis-assemble",
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
        name=raw.get("name", os.path.basename(d)), version=got or "unversioned",
        engine=raw["engine"], schema_version=int(raw.get("schema_version", 1)),
        description=raw.get("description", ""), template=raw.get("template", {}) or {},
        page=raw.get("page", {}) or {}, fonts=raw.get("fonts", {}) or {},
        bibliography=raw.get("bibliography", {}) or {},
        layout=raw.get("layout", {}) or {}, rules=raw.get("rules", {}) or {},
        checks=raw.get("checks", {}) or {},
        open_to_build=raw.get("open_to_build", []) or [],
        quarto_metadata=raw.get("quarto_metadata", {}) or {}, raw=raw)


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
