"""The quarto-render engine: one `quarto render` per output format.

This is the whole build for a manuscript, and it needs none of the thesis engine —
no measure pass, no page reservation, no splice, no nocite union, no front-matter
ordering. Those exist to serve inserted papers and a faculty front matter, and a
preprint has neither.

The engine composes a `_metadata.yml` beside the source from three layers, in this
order and no other (one precedence mechanism, as decided):

    profile defaults  <-  profile.quarto_metadata  <-  build quarto_metadata

The last is the advanced-user escape hatch. It is merged AFTER the profile so a
user can genuinely override, and it is screened by profile.check_quarto_metadata
first, so the escape hatch cannot re-enable code execution or client-supplied
filters.
"""
from __future__ import annotations

import os
import subprocess
import time

import yaml

from .. import profile as profiles

BUILD_TIMEOUT_S = int(os.environ.get("ABCPRINT_BUILD_TIMEOUT", "1800"))

# Profile vocabulary -> typst paper names.
PAPER_NAMES = {"letter": "us-letter", "us-letter": "us-letter",
               "legal": "us-legal", "a4": "a4", "a5": "a5", "a3": "a3"}


class EngineError(RuntimeError):
    pass


def _deep_merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _metadata_from_profile(prof: profiles.Profile, formats: list[str]) -> dict:
    """Translate the profile's own vocabulary into Quarto's.

    The profile speaks in `fonts.body.stack` and `layout.floats_in_text`; Quarto
    speaks in `mainfont` and `floatsintext`. Keeping the translation here means a
    profile never has to be written in Quarto's dialect, and a second engine can
    translate the same profile differently.
    """
    meta: dict = {}
    fonts = prof.fonts or {}

    def first(role: str) -> str | None:
        stack = ((fonts.get(role) or {}).get("stack")) or []
        return stack[0] if stack else None

    # Quarto/typst take a single family per role rather than a stack, so the
    # profile's stack contributes its first entry. The stack is still the right
    # shape in the profile: it records the intended substitution order, and the
    # manifest reports what actually resolved.
    for role, key in (("body", "mainfont"), ("sans", "sansfont"), ("mono", "monofont")):
        fam = first(role)
        if fam:
            meta[key] = fam
    if "mainfont" in meta and "sansfont" not in meta:
        meta["sansfont"] = meta["mainfont"]

    bib = prof.bibliography or {}
    if bib.get("style"):
        meta["csl"] = f"{bib['style']}.csl"

    layout = prof.layout or {}
    if "floats_in_text" in layout:
        meta["floatsintext"] = bool(layout["floats_in_text"])
    if "numbered_lines" in layout:
        meta["numbered-lines"] = bool(layout["numbered_lines"])

    page = prof.page or {}
    if page.get("size"):
        # typst names US Letter "us-letter"; a profile that says "letter" is not
        # wrong, it is speaking the profile's vocabulary. Translating here is the
        # whole point of this function — passing it through raw aborts the
        # compile with a typst paper-name error.
        meta["papersize"] = PAPER_NAMES.get(str(page["size"]).lower(), str(page["size"]))

    return meta


def compose_metadata(prof: profiles.Profile, formats: list[str],
                     user_metadata: dict | None,
                     base: dict | None = None) -> tuple[dict, list[str]]:
    """Build the effective Quarto metadata and report any refused keys.

    Exactly one precedence chain, lowest first:

        bundle _metadata.yml   what the document IS (authors, abstract, keywords)
        profile-derived        what the PROFILE governs (fonts, format, page, csl)
        profile.quarto_metadata  profile-level passthrough
        build quarto_metadata    the advanced-user escape hatch

    The profile deliberately sits above the bundle, because fonts and page setup
    are the profile's to decide; `quarto_metadata` sits above the profile, because
    an escape hatch that cannot override is not one.
    """
    refused = profiles.check_quarto_metadata(user_metadata or {})
    if refused:
        return {}, refused
    meta = dict(base or {})
    meta = _deep_merge(meta, _metadata_from_profile(prof, formats))
    meta = _deep_merge(meta, prof.quarto_metadata or {})
    meta = _deep_merge(meta, user_metadata or {})

    # Ensure every requested format is declared, WITHOUT flattening one the bundle
    # already configured. Emitting {apaquarto-typst: "default"} replaced the
    # bundle's own per-format block — which carried keep-typ and a monofont — so
    # the render silently used different fonts for code and set differently.
    # A format the bundle already describes is left exactly as it is.
    fmt_map = (prof.template or {}).get("formats") or {}
    existing = meta.get("format")
    fmt_block = dict(existing) if isinstance(existing, dict) else {}
    for f in formats:
        target = fmt_map.get(f)
        if target and target not in fmt_block:
            fmt_block[target] = "default"
    if fmt_block:
        meta["format"] = fmt_block
    return meta, []


def run(paths: dict, prof: profiles.Profile, *, formats: list[str],
        user_metadata: dict | None = None, entry: str = "index.qmd") -> dict:
    src_dir = paths["src"]
    root = paths["repo"]
    src = os.path.join(src_dir, entry)
    if not os.path.isfile(src):
        raise EngineError(
            f"entry document {entry!r} not found; the bundle carried: "
            f"{sorted(f for f in os.listdir(src_dir) if f.endswith('.qmd'))}")

    # A bundle may carry its own _metadata.yml, and the nf-nomad manuscript does:
    # it holds the author block, affiliations, ORCIDs and the author note, none of
    # which the profile knows or should know. Overwriting it lost the authors and
    # apaquarto refused to render. It is therefore the BASE of the chain, not a
    # casualty of it.
    bundle_meta: dict = {}
    bundle_path = os.path.join(src_dir, "_metadata.yml")
    if os.path.isfile(bundle_path):
        try:
            with open(bundle_path) as fh:
                bundle_meta = yaml.safe_load(fh) or {}
        except yaml.YAMLError as exc:
            raise EngineError(f"the bundle's _metadata.yml is not valid YAML: {exc}")
        if not isinstance(bundle_meta, dict):
            raise EngineError("the bundle's _metadata.yml must be a mapping")
        # The bundle is client-supplied, so it gets the same screening as the
        # advanced-user escape hatch. Otherwise a refused key simply moves house.
        refused_bundle = profiles.check_quarto_metadata(bundle_meta)
        if refused_bundle:
            raise EngineError("the bundle's _metadata.yml was rejected:\n  "
                              + "\n  ".join(r.replace("quarto_metadata.", "_metadata.yml:")
                                             for r in refused_bundle))

    meta, refused = compose_metadata(prof, formats, user_metadata, base=bundle_meta)
    if refused:
        raise EngineError("quarto_metadata rejected:\n  " + "\n  ".join(refused))

    with open(bundle_path, "w") as fh:
        yaml.safe_dump(meta, fh, sort_keys=False, allow_unicode=True)

    fmt_map = (prof.template or {}).get("formats") or {}
    unknown = [f for f in formats if f not in fmt_map]
    if unknown:
        raise EngineError(
            f"profile {prof.ref} cannot produce {unknown}; it offers {sorted(fmt_map)}")

    env = dict(os.environ)
    env.update({"SOURCE_DATE_EPOCH": os.environ.get("ABCPRINT_SOURCE_DATE_EPOCH", "1700000000"),
                "TZ": "UTC", "HOME": paths["repo"],
                # Belt and braces: the metadata guard refuses `execute`, and this
                # stops a stray chunk in the source from running anyway.
                "QUARTO_DENO_EXTRA_OPTIONS": "--no-remote"})

    # Render INSIDE src/ rather than to an output directory outside it. typst
    # refuses to read a file outside its project root, and apaquarto's typst
    # output references ../_extensions/apaquarto/ORCID-iD_icon-vector.svg — so an
    # external output dir puts the extension out of reach and the render dies on
    # "access denied". Artefacts are collected out afterwards.
    # Output and the project file belong at the ROOT, not inside src/. apaquarto
    # emits ../_extensions/... relative to the document, so the extension has to
    # sit one level ABOVE the entry document — which is exactly how the source
    # manuscript is laid out (manuscript/_extensions + manuscript/src/index.qmd).
    out_dir = os.path.join(root, "_out")
    os.makedirs(out_dir, exist_ok=True)

    # Declare a Quarto PROJECT rooted at src/. Quarto derives the typst project
    # root from it, and typst refuses to read outside that root — so without this
    # the root collapses to the output directory and apaquarto's
    # ../_extensions/apaquarto/ORCID-iD_icon-vector.svg becomes unreadable.
    #
    # Note `project` is a refused key in client-supplied metadata, because it can
    # carry pre-render/post-render shell hooks. The engine writing its own is not
    # the same thing: this file is ours, fixed, and carries no hooks.
    with open(os.path.join(root, "_quarto.yml"), "w") as fh:
        yaml.safe_dump({"project": {"output-dir": "_out"}}, fh, sort_keys=False)

    started, log_all = time.time(), []
    for f in formats:
        cmd = ["quarto", "render", os.path.join("src", entry), "--to", fmt_map[f],
               "--output-dir", "_out", "--no-execute"]
        try:
            proc = subprocess.run(cmd, cwd=root, env=env, capture_output=True,
                                  text=True, timeout=BUILD_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            raise EngineError(f"render of {f} exceeded {BUILD_TIMEOUT_S}s")
        log_all += [f"$ {' '.join(cmd)}"] + ((proc.stdout or "") + (proc.stderr or "")).splitlines()
        if proc.returncode != 0:
            err = EngineError(f"quarto render --to {fmt_map[f]} failed (rc={proc.returncode}); "
                              "last lines:\n" + "\n".join(log_all[-25:]))
            err.log_tail = log_all[-60:]
            raise err

    # Collect artefacts out of src/_out into the job's build directory, which is
    # what the artefact endpoint serves from.
    # Quarto mirrors the input path under the output dir, so rendering
    # src/index.qmd lands the PDF at _out/src/index.pdf. Walk rather than
    # listdir, or the artefact is "produced" and invisible.
    import shutil as _sh
    for dirpath, _dirs, files in os.walk(out_dir):
        for name in files:
            if name.lower().endswith((".pdf", ".docx", ".html")):
                _sh.copy2(os.path.join(dirpath, name),
                          os.path.join(paths["build"], name))

    artifacts = []
    for name in sorted(os.listdir(paths["build"])):
        fp = os.path.join(paths["build"], name)
        if os.path.isfile(fp) and name.lower().endswith((".pdf", ".docx", ".html")):
            artifacts.append({"name": name, "bytes": os.path.getsize(fp),
                              "media_type": {"pdf": "application/pdf",
                                             "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                                             "html": "text/html"}[name.rsplit(".", 1)[-1].lower()]})
    if not artifacts:
        raise EngineError("quarto reported success but produced no output file")

    return {"artifacts": artifacts, "effective_metadata": meta,
            "build_seconds": round(time.time() - started, 1),
            "log_tail": log_all[-40:]}
