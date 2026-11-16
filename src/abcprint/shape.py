"""The ideal shape of a client zip for a profile, so a client asks instead of guessing.

`GET /svc/v1/profiles/{ref}/shape` answers with the layouts the profile accepts,
its exact rules (required / allowed / forbidden / limits — the same ones the front
door enforces) and an annotated example tree. `…/skeleton.zip` is that tree as a
starter zip whose every file passes the rules.

The tree comes from the profile when its publisher described one (`bundle.tree`
in profile.yaml, or the file list of its `examples/*.zip`, recorded at publish);
otherwise from the engine's own layout, which is what the workspace code maps.
"""
from __future__ import annotations

import io
import zipfile

from . import bundle_rules

LAYOUT_TEXT = {
    "garden": "Sources under src/, figures under assets/figures/current/ (the project-garden "
              "layout). Documents reference figures relative to themselves, e.g. "
              "../assets/figures/current/F1.svg.",
    "flat": "Everything at the top of the zip, the document included; it is placed under src/ "
            "before the build, so relative references between your files still hold.",
}

# What each engine reads, as an annotated example. `pattern` says how a name may vary.
ENGINE_TREES = {
    "quarto-render": [
        {"path": "src/index.qmd", "role": "entry document", "required": True,
         "pattern": "src/<name>.qmd — name it with `entry` if it is not index.qmd"},
        {"path": "src/_metadata.yml", "role": "authors, affiliations, abstract, keywords"},
        {"path": "src/references.bib", "role": "bibliography"},
        {"path": "assets/figures/current/figure-1.svg", "role": "figure",
         "pattern": "assets/figures/current/<name>.{svg,png,jpg,pdf}"},
        {"path": "assets/tables/current/table-1.csv", "role": "table data"},
    ],
    "thesis-assemble": [
        {"path": "src/01-introduction.md", "role": "chapter", "required": True,
         "pattern": "src/NN-slug.md — NN orders the chapters; build a subset with `chapters`"},
        {"path": "src/02-methods.md", "role": "chapter", "pattern": "src/NN-slug.md"},
        {"path": "src/thesis.yml", "role": "identity: author, degree, faculty, supervisor, dates"},
        {"path": "src/Acknowledgements.md", "role": "front matter",
         "pattern": "src/<Section>.md — e.g. Dedication.md, Abstract.md"},
        {"path": "src/references.bib", "role": "main bibliography"},
        {"path": "src/_intro-refs-2026-11.bib", "role": "per-area bibliography",
         "pattern": "src/_<area>-refs-<date>.bib — any other .bib name is refused"},
        {"path": "assets/figures/current/F1.1-overview.svg", "role": "figure",
         "pattern": "assets/figures/current/<name> — referenced as figures/<name> from a chapter"},
        {"path": "assets/inserted-papers/paper-1.pdf", "role": "published paper spliced in whole"},
    ],
}

PLACEHOLDER = {
    ".qmd": "---\ntitle: \"Title\"\n---\n\nText.\n",
    ".md": "# Heading\n\nText.\n",
    ".yml": "# see the profile's documentation for the keys this file takes\n",
    ".bib": "@misc{example, title = {Example}, year = {2026}}\n",
    ".svg": "<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10'/>\n",
    ".csv": "a,b\n1,2\n",
}


def tree_for(profile) -> tuple[list[dict], str]:
    raw = profile.bundle or {}
    if raw.get("tree"):
        return list(raw["tree"]), "profile"
    if raw.get("examples"):
        first = sorted(raw["examples"])[0]
        return [{"path": p} for p in raw["examples"][first]], f"examples/{first}"
    return [dict(e) for e in ENGINE_TREES.get(profile.engine, [])], "engine"


def shape(profile) -> dict:
    rules = bundle_rules.effective(profile)
    tree, origin = tree_for(profile)
    req = set(rules["required"])
    for e in tree:
        if e["path"] in req:
            e["required"] = True
    # A path the rules demand but the example does not show is still listed.
    tree += [{"path": p, "required": True} for p in sorted(req - {e["path"] for e in tree})]
    return {
        "profile": profile.ref, "digest": profile.digest, "engine": profile.engine,
        "layouts": {k: LAYOUT_TEXT[k] for k in rules["layouts"]},
        "rules": {k: rules[k] for k in ("required", "allowed", "forbidden", "limits")},
        "tree": tree, "tree_from": origin,
        "submit": {"upload": "POST /v1/uploads (one zip)",
                   "run": f"POST /v1/{'thesis' if profile.engine == 'thesis-assemble' else 'manuscript'}"
                          f"/runs {{\"bundle\": \"upl_…\", \"profile\": \"{profile.ref}\"}}"},
    }


def skeleton_zip(profile) -> bytes:
    """The example tree as a zip that passes the profile's own rules."""
    rules = bundle_rules.effective(profile)
    tree, _ = tree_for(profile)
    paths = [e["path"] for e in tree] + [p for p in rules["required"]]
    keep = []
    for p in dict.fromkeys(paths):
        if any(bundle_rules.matches(f, p) for f in rules["forbidden"]):
            continue
        if rules["allowed"] and not any(bundle_rules.matches(a, p) for a in rules["allowed"]):
            continue
        keep.append(p)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in keep:
            ext = "." + p.rsplit(".", 1)[-1] if "." in p.rsplit("/", 1)[-1] else ""
            if ext == ".pdf":
                continue    # no placeholder PDF: a fake one would only mislead a build
            zf.writestr(p, PLACEHOLDER.get(ext, ""))
    return buf.getvalue()
