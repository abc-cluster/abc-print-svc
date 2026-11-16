"""A profile's zip rules: what a client bundle for THIS template may contain.

`archive.py` keeps the safety guards every zip gets (zip slip, bombs, links,
global caps). This module interprets the profile's own `bundle:` section, so a
new template's layout is published with the template instead of coded into the
image:

    bundle:
      layouts: [garden, flat]          # garden: sources under src/; flat: a .qmd at the root
      required: [src/index.qmd]        # checked after a flat bundle is moved under src/
      allowed: ["src/**", "assets/**"] # if given, every file must match one
      forbidden: ["**/*.lua"]          # always refused, whatever `allowed` says
      limits: {max_files: 2000, max_bytes: 268435456}   # may only tighten archive.py's caps

Globs are POSIX paths; `**/` also matches at the top level, and `{a,b}` expands.
The same check runs in the front door (before admission) and in the worker, so a
bundle the front door accepts is one the worker builds.
"""
from __future__ import annotations

import fnmatch
import re

from . import archive

LAYOUTS = ("garden", "flat")

# Client bundles never bring code. An extension's Lua and a project file's
# pre/post-render hooks run in the worker; the profile's own extension is the
# only code a build runs. Applies unless a profile sets its own `forbidden`.
DEFAULT_FORBIDDEN = ["**/*.lua", "_extensions/**", "**/_extensions/**",
                     "**/_quarto.yml", "**/_quarto.yaml"]

ENGINE_DEFAULTS = {
    "quarto-render": {"layouts": ["garden", "flat"]},
    # The thesis pipeline reads fixed locations; only the garden layout is mapped onto them.
    "thesis-assemble": {"layouts": ["garden"]},
}


class RuleError(ValueError):
    pass


def effective(profile) -> dict:
    """The rules a bundle for this profile is checked against."""
    raw = dict(getattr(profile, "bundle", None) or {})
    rules = {"layouts": list(ENGINE_DEFAULTS.get(profile.engine, {}).get("layouts", LAYOUTS)),
             "required": [], "allowed": [], "forbidden": list(DEFAULT_FORBIDDEN),
             "limits": {"max_files": archive.MAX_ENTRIES, "max_bytes": archive.MAX_TOTAL_BYTES}}
    rules.update({k: v for k, v in raw.items() if k not in ("limits", "tree", "examples")})
    for k, v in (raw.get("limits") or {}).items():
        rules["limits"][k] = min(int(v), rules["limits"].get(k, int(v)))
    return rules


def validate_schema(rules: dict, engine: str) -> list[str]:
    """Problems with a profile's `bundle:` section, for the publish step."""
    problems = []
    known = {"layouts", "required", "allowed", "forbidden", "limits",
             "tree",        # the example tree a client is shown (abcprint.shape)
             "examples"}    # filled in by publish: each examples/*.zip's file list
    for k in rules:
        if k not in known:
            problems.append(f"bundle.{k}: unknown rule (known: {sorted(known)})")
    for lay in rules.get("layouts") or []:
        if lay not in LAYOUTS:
            problems.append(f"bundle.layouts: {lay!r} is not one of {list(LAYOUTS)}")
    if engine == "thesis-assemble" and "flat" in (rules.get("layouts") or []):
        problems.append("bundle.layouts: the thesis-assemble engine maps only the garden layout")
    for k in ("required", "allowed", "forbidden"):
        v = rules.get(k)
        if v is not None and not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
            problems.append(f"bundle.{k}: must be a list of path globs")
    for i, e in enumerate(rules.get("tree") or []):
        if not (isinstance(e, dict) and isinstance(e.get("path"), str)):
            problems.append(f"bundle.tree[{i}]: each entry needs a `path` (and may have role, pattern, required)")
    for k, v in (rules.get("limits") or {}).items():
        if k not in ("max_files", "max_bytes") or not isinstance(v, int) or v <= 0:
            problems.append(f"bundle.limits.{k}: must be max_files or max_bytes, a positive integer")
    return problems


def _expand(pattern: str) -> list[str]:
    m = re.search(r"\{([^{}]*)\}", pattern)
    if not m:
        return [pattern]
    out = []
    for alt in m.group(1).split(","):
        out += _expand(pattern[:m.start()] + alt + pattern[m.end():])
    return out


def matches(pattern: str, path: str) -> bool:
    for p in _expand(pattern):
        if fnmatch.fnmatchcase(path, p):
            return True
        if p.startswith("**/") and fnmatch.fnmatchcase(path, p[3:]):
            return True
        if p.endswith("/**") and (path == p[:-3] or path.startswith(p[:-2])):
            return True
    return False


def layout_of(files: list[str]) -> str | None:
    if any(f.startswith("src/") for f in files):
        return "garden"
    if any("/" not in f and f.endswith((".qmd", ".md")) for f in files):
        return "flat"
    return None


def check(files: list[str], total_bytes: int, rules: dict, *, layout: str | None) -> None:
    """Refuse a bundle that breaks the profile's rules, naming every offending file.

    `files` are the extracted paths (after a flat bundle has been moved under
    src/, so `required` reads the same for both layouts).
    """
    problems = []
    if layout is None:
        problems.append("no sources found: expected src/… (garden layout) or a .qmd/.md "
                        "at the top of the zip (flat layout)")
    elif layout not in rules["layouts"]:
        problems.append(f"this profile accepts the {' or '.join(rules['layouts'])} layout; "
                        f"the zip is {layout}" + (
                            " — put the sources under src/ (chapters as src/NN-slug.md)"
                            if layout == "flat" else ""))
    forbidden = sorted(f for f in files if any(matches(p, f) for p in rules["forbidden"]))
    if forbidden:
        problems.append(f"not accepted in a client bundle (it would run as code, or override "
                        f"the profile; put settings in quarto_metadata instead): {forbidden[:10]}")
    if rules["allowed"]:
        stray = sorted(f for f in files if f not in forbidden and
                       not any(matches(p, f) for p in rules["allowed"]))
        if stray:
            problems.append(f"not allowed by this profile's bundle rules: {stray[:10]}")
    missing = [r for r in rules["required"] if r not in files]
    if missing:
        problems.append(f"required by this profile but missing: {missing}")
    lim = rules["limits"]
    if len(files) > lim["max_files"]:
        problems.append(f"{len(files)} files; this profile allows {lim['max_files']}")
    if total_bytes > lim["max_bytes"]:
        problems.append(f"{total_bytes} bytes; this profile allows {lim['max_bytes']}")
    if problems:
        raise RuleError("; ".join(problems))
