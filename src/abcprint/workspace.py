"""Materialise a repo-shaped workspace from an uploaded bundle.

The pipeline was written against a checkout and reads fixed locations under it:
writeup/thesis, writeup/figures, writeup/inserted-papers, writeup/references.bib,
writeup/thesis-quarto (the profile) and writeup/bin (the scripts). Rather than
rewrite the pipeline to take a bundle — which is slice 2 — the service builds the
layout the pipeline already expects.

Client-supplied names are the untrusted part here, so every write is confined to
the workspace and anything escaping it is refused.
"""
from __future__ import annotations

import os
import re
import shutil

PROFILE_DIR = os.environ.get("ABCPRINT_PROFILE", "/srv/profiles/su-fmhs")
PIPELINE_DIR = os.environ.get("ABCPRINT_PIPELINE", "/srv/pipeline")

# A chapter is selected by numeric prefix; the front matter is everything else.
CHAPTER_RE = re.compile(r"^(\d{2})-")
SAFE_NAME = re.compile(r"^[A-Za-z0-9 ._-]+$")
# Per-area bibliographies the pipeline auto-includes from the thesis dir.
BIB_PER_AREA = re.compile(r"^_.*-refs-.*\.bib$")


class BundleError(ValueError):
    pass


def _safe_join(root: str, name: str) -> str:
    base = os.path.basename(name or "")
    if not base or base in (".", "..") or not SAFE_NAME.match(base):
        raise BundleError(f"unsafe filename: {name!r}")
    dest = os.path.abspath(os.path.join(root, base))
    if os.path.commonpath([os.path.abspath(root), dest]) != os.path.abspath(root):
        raise BundleError(f"filename escapes its directory: {name!r}")
    return dest


def _safe_join_rel(root: str, name: str) -> str:
    """Join a client-supplied RELATIVE path under root, refusing any escape.

    Each component is validated individually, so `a/b/c.svg` is allowed while
    `../`, an absolute path, or an odd component is not.
    """
    raw = (name or "").replace("\\", "/").strip()
    if not raw or raw.startswith("/"):
        raise BundleError(f"unsafe path: {name!r}")
    parts = [p for p in raw.split("/") if p not in ("", ".")]
    if not parts or any(p == ".." or not SAFE_NAME.match(p) for p in parts):
        raise BundleError(f"unsafe path: {name!r}")
    dest = os.path.abspath(os.path.join(root, *parts))
    if os.path.commonpath([os.path.abspath(root), dest]) != os.path.abspath(root):
        raise BundleError(f"path escapes its directory: {name!r}")
    return dest


def create(root: str) -> dict:
    """Build the directory skeleton and return the paths the caller writes into."""
    paths = {
        "repo": root,
        "thesis": os.path.join(root, "writeup", "thesis"),
        "figures": os.path.join(root, "writeup", "figures"),
        "inserted": os.path.join(root, "writeup", "inserted-papers"),
        "bin": os.path.join(root, "writeup", "bin"),
        "quarto": os.path.join(root, "writeup", "thesis-quarto"),
        "build": os.path.join(root, "writeup", "_build"),
    }
    paths["bib_dir"] = os.path.join(root, "writeup")
    paths["bib_per_area_dir"] = paths["thesis"]
    for p in paths.values():
        os.makedirs(p, exist_ok=True)

    if not os.path.isdir(PROFILE_DIR):
        raise BundleError(
            f"profile not found at {PROFILE_DIR}; the image was built without one")
    # dirs_exist_ok so the skeleton above does not collide with the profile copy
    shutil.copytree(PROFILE_DIR, paths["quarto"], dirs_exist_ok=True)

    if not os.path.isdir(PIPELINE_DIR):
        raise BundleError(f"pipeline scripts not found at {PIPELINE_DIR}")
    shutil.copytree(PIPELINE_DIR, paths["bin"], dirs_exist_ok=True)
    for f in os.listdir(paths["bin"]):
        fp = os.path.join(paths["bin"], f)
        if os.path.isfile(fp):
            os.chmod(fp, 0o755)
    return paths


def write_source(paths: dict, name: str, data: bytes) -> str:
    dest = _safe_join(paths["thesis"], name)
    with open(dest, "wb") as fh:
        fh.write(data)
    return os.path.basename(dest)


def write_asset(paths: dict, kind: str, name: str, data: bytes) -> str:
    if kind not in ("figure", "inserted", "bib"):
        raise BundleError(f"unknown asset kind {kind!r}")
    if kind == "figure":
        target = paths["figures"]
    elif kind == "inserted":
        target = paths["inserted"]
    else:
        # Bibliographies land in two different places and the name decides which.
        # The pipeline reads writeup/references.bib plus every
        # writeup/thesis/_*-refs-*.bib, so a per-area bib written next to
        # references.bib would simply never be picked up — the citations it
        # carries would go missing with nothing but a citeproc warning.
        base = os.path.basename(name or "")
        # Where a bibliography belongs is ENGINE-specific: the thesis pipeline
        # reads writeup/references.bib plus writeup/thesis/_*-refs-*.bib, while
        # the quarto engine resolves everything relative to the entry document.
        # Hard-coding the thesis layout put references.bib somewhere the quarto
        # render could not see it, and the failure surfaced only as a lua
        # traceback from inside a citation filter.
        main_dir = paths.get("bib_dir") or os.path.join(paths["repo"], "writeup")
        per_area_dir = paths.get("bib_per_area_dir") or paths["thesis"]
        target = per_area_dir if BIB_PER_AREA.match(base) else main_dir
    os.makedirs(target, exist_ok=True)
    # Figures may be referenced through a SUBPATH — the thesis has e.g.
    # figures/F6.2-metro-src/F6.2-metro-v4.typst.svg — so flattening to a
    # basename makes those unresolvable and the render dies on a missing file.
    # Relative structure is preserved for figures; everything else is flat.
    dest = _safe_join_rel(target, name) if kind == "figure" else _safe_join(target, name)
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(dest, "wb") as fh:
        fh.write(data)
    return os.path.relpath(dest, target)


def detected_chapters(paths: dict) -> list[str]:
    """Chapter prefixes present in the workspace, in order.

    Note the pipeline picks the SHORTEST filename per prefix, so a
    `06-x-supplement.md` alongside `06-x.md` builds the supplement. That belongs
    to the pipeline's selection rule; this only reports which prefixes exist.
    """
    seen = []
    for f in sorted(os.listdir(paths["thesis"])):
        m = CHAPTER_RE.match(f)
        if m and m.group(1) not in seen:
            seen.append(m.group(1))
    return seen


def create_quarto(root: str, profile_dir: str) -> dict:
    """Workspace for the quarto-render engine.

    Far simpler than the thesis layout, because the engine is: everything the
    render needs sits beside the entry document, and the profile's vendored
    extension is copied in so `--to apaquarto-typst` resolves without a network
    fetch or a client-supplied path.
    """
    paths = {
        "repo": root,
        "src": os.path.join(root, "src"),
        # Figures are written RELATIVE TO THE ENTRY DOCUMENT, because that is how
        # a document references them. Combined with the traversal guard — which
        # refuses any `..` component — this means a bundle can only reference
        # figures inside its own directory, i.e. it must be self-contained. A
        # bundle reaching out to ../assets/... is not portable, and the service
        # declining to render it is the correct answer rather than a limitation.
        #
        # The profile's _extensions is the one exception and sits at the project
        # root, because apaquarto emits ../_extensions/... from the document. That
        # is the extension's own convention, not the bundle's.
        "figures": os.path.join(root, "src"),
        "build": os.path.join(root, "_build"),
    }
    # The quarto engine resolves bibliographies relative to the entry document.
    paths["bib_dir"] = paths["src"]
    paths["bib_per_area_dir"] = paths["src"]
    # The quarto engine reads bibliographies and inserts from src/ directly.
    paths["thesis"] = paths["src"]
    paths["inserted"] = paths["src"]
    for p in paths.values():
        os.makedirs(p, exist_ok=True)

    if not os.path.isdir(profile_dir):
        raise BundleError(f"profile directory not found: {profile_dir}")
    for item in os.listdir(profile_dir):
        if item == "profile.yaml":
            continue
        # _extensions goes to the project ROOT (apaquarto emits ../_extensions/…);
        # everything else the profile ships sits beside the document.
        dest_base = root if item == "_extensions" else paths["src"]
        s_, d_ = os.path.join(profile_dir, item), os.path.join(dest_base, item)
        if os.path.isdir(s_):
            shutil.copytree(s_, d_, dirs_exist_ok=True)
        else:
            shutil.copy2(s_, d_)
    return paths
