"""HTTP surface over the library.

One library, two access modes. Every endpoint is a thin shell over a function in
abcprint.* that is equally callable in-process, so the CLI added later is a third
shell over the same code rather than a reimplementation.

Docs are served at /redoc (reference) and /docs (try-it-out). Both are vendored
into the image rather than loaded from a CDN, because the renderer is intended
to run with no network egress and documentation that blanks out in the
deployment it documents is not documentation.
"""
from __future__ import annotations

import os
import shutil
import tempfile

import json
import shutil as _shutil
import tempfile as _tempfile

from fastapi import FastAPI, UploadFile, File, HTTPException, Query, Form, BackgroundTasks
from fastapi.responses import JSONResponse, HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from . import compile as compiler, delivery, equivalence, fonts, jobs, profile as profiles, toolchain, workspace
from .models import (DeliveryResult, Equivalence, FontHealth, Health, Job as JobModel,
                     JobList, ProfileList, Toolchain as ToolchainModel)

REQUIRED_FONTS = ["Calibri", "Cambria", "Georgia", "Times New Roman", "Arial", "Helvetica Neue"]
FONT_PATHS = [p for p in os.environ.get("ABCPRINT_FONT_PATHS", "").split(os.pathsep) if p]
VENDOR_DIR = os.environ.get("ABCPRINT_VENDOR", "/srv/vendor")

DESCRIPTION = """
Document compilation and compliance verification for thesis-style documents.

**The encoded rulebook is the product; the renderer is delivery.** A student can
already turn text into a PDF. What costs them weeks near submission is satisfying
a faculty rulebook that nobody has written down in executable form.

### Why there is no byte-comparison endpoint

Byte equality is the wrong test here, and that is measured rather than assumed.
Building the same 230-page thesis twice on one machine, 30 seconds apart, gives
two files of *identical length* that differ in roughly **84,000 bytes**:

| source | fixable? |
|---|---|
| `/CreationDate`, `/ModDate` | yes — honour `SOURCE_DATE_EPOCH` |
| `/ID[1]`, XMP `InstanceID` | no — random per render |
| `/Font`, `/XObject`, `/ExtGState` emission order | no — per-process hash order |

Those documents are nonetheless the same: equal page count, byte-identical
`pdftotext -layout` output, pixel-identical pages. So `POST /equivalence` decides
sameness on four planes instead, excluding volatile fields **by name** so that a
new source of drift still fails.

*Trap:* with `SOURCE_DATE_EPOCH` pinned, a **small** document does compare
byte-identical, while the full thesis still differs by ~45,000 bytes. Validating
reproducibility on a one-chapter sample gives the wrong answer.

### Fonts

The template names six proprietary families. The image ships metric-compatible
libre substitutes so it works out of the box without redistributing anything;
licensed originals mounted at `/usr/local/share/fonts/licensed` win automatically.
Because the faculty length rule is measured in **pages**, substitution is a
compliance event and is always declared, never silent.
"""

TAGS = [
    {"name": "compile", "description":
        "Push sources, get a document. This is the surface an editor plugin (Logseq, or "
        "anything else that holds the text) talks to: POST the markdown, poll the job, "
        "then either download the artefact or have the service deliver it."},
    {"name": "delivery", "description":
        "Where a finished artefact goes: the user's object-store prefix, or a mounted "
        "downloads directory."},
    {"name": "health", "description": "Is this deployment able to build at all? Check before blaming a document."},
    {"name": "verification", "description": "Decide whether two builds are the same document."},
]

app = FastAPI(
    title="abc-print-svc",
    version="0.1.0",
    description=DESCRIPTION,
    openapi_tags=TAGS,
    license_info={"name": "Apache-2.0"},
    docs_url=None,     # replaced below with offline-capable equivalents
    redoc_url=None,
)

# CORS. A plugin running inside an editor is a browser context, so without this
# every call is blocked before it reaches a handler — the request never appears in
# the service log, which makes it look like the service is down rather than
# refusing.
#
# The default is permissive because this service has no authentication and is
# expected to run locally, where the alternative is that nothing works out of the
# box. An internet-facing deployment should set ABCPRINT_CORS_ORIGINS to an
# explicit list; note that with `*` any page the user visits can drive the service.
# Credentials are never allowed, which the CORS spec requires alongside `*` anyway.
CORS_ORIGINS = [o.strip() for o in
                os.environ.get("ABCPRINT_CORS_ORIGINS", "*").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition"],
)

if os.path.isdir(VENDOR_DIR):
    app.mount("/vendor", StaticFiles(directory=VENDOR_DIR), name="vendor")


def _doc_page(title: str, script: str, body: str) -> HTMLResponse:
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8">
<title>{title}</title><meta name="viewport" content="width=device-width,initial-scale=1">
<style>body{{margin:0}}</style></head><body>{body}<script src="{script}"></script></body></html>""")


@app.get("/redoc", include_in_schema=False)
def redoc():
    """Reference documentation, served from the image so it works with no egress."""
    local = os.path.join(VENDOR_DIR, "redoc.standalone.js")
    src = "/vendor/redoc.standalone.js" if os.path.exists(local) else \
        "https://cdn.redoc.ly/redoc/latest/bundles/redoc.standalone.js"
    return _doc_page("abc-print-svc — API reference", src,
                     '<redoc spec-url="/openapi.json"></redoc>')


@app.get("/docs", include_in_schema=False)
def docs():
    """Swagger UI — the try-it-out surface for manual testing."""
    local = os.path.join(VENDOR_DIR, "swagger-ui-bundle.js")
    if os.path.exists(local):
        js, css = "/vendor/swagger-ui-bundle.js", "/vendor/swagger-ui.css"
    else:
        js = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui-bundle.js"
        css = "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css"
    return HTMLResponse(f"""<!doctype html><html><head><meta charset="utf-8">
<title>abc-print-svc — try it out</title><link rel="stylesheet" href="{css}">
<meta name="viewport" content="width=device-width,initial-scale=1"></head><body>
<div id="ui"></div><script src="{js}"></script>
<script>SwaggerUIBundle({{url:'/openapi.json',dom_id:'#ui'}});</script></body></html>""")


@app.get("/", include_in_schema=False)
def index():
    return {"service": "abc-print-svc", "version": "0.1.0",
            "reference": "/redoc", "try_it_out": "/docs", "schema": "/openapi.json"}


def _engines_available() -> dict:
    """Which engines this image can actually run.

    The thesis engine needs the pipeline scripts vendored at build time; an image
    built without them can still run the quarto-render engine. Reporting that is
    better than accepting a thesis job and failing it 30 seconds later.
    """
    pipeline = os.environ.get("ABCPRINT_PIPELINE", "/srv/pipeline")
    return {
        "quarto-render": True,
        "thesis-assemble": os.path.isfile(os.path.join(pipeline, "thesis-assemble.sh")),
    }


@app.get("/health", tags=["health"], response_model=Health,
         summary="Can this deployment build?",
         responses={503: {"description": "Deployment cannot build; `problems` names each reason."}})
def health():
    """Liveness plus the two things that actually break a deployment: a toolchain
    that cannot merge, and fonts that are not there.

    Returns **503** when either fails. A missing font directory is meant to fail
    the *service's* health check rather than a student's build, so an operator
    who mounted nothing finds out at deploy time and not from twenty students at
    a submission deadline.
    """
    tc = toolchain.detect()
    fh = fonts.health(REQUIRED_FONTS, FONT_PATHS or None)
    problems = tc.problems() + ([] if fh["ok"] else [f"fonts unavailable: {fh['missing']}"])
    body = {"ok": not problems, "problems": problems, "toolchain": tc.as_dict(),
            "typst_mismatch": tc.typst_mismatch, "fonts": fh,
            "cors_allowed_origins": CORS_ORIGINS,
            "engines_available": _engines_available()}
    return JSONResponse(body, status_code=200 if not problems else 503)


@app.get("/toolchain", tags=["health"], response_model=ToolchainModel,
         summary="What rendered this document?")
def get_toolchain():
    """The manifest half that names the binaries deciding the output.

    `typst_bundled` is authoritative: Quarto renders with its own typst, so
    pinning a standalone typst pins a binary nothing reads.
    """
    tc = toolchain.detect()
    return {**tc.as_dict(), "typst_mismatch": tc.typst_mismatch}


@app.get("/fonts", tags=["health"], response_model=FontHealth,
         summary="Which family will actually render?")
def get_fonts(family: list[str] | None = Query(
        default=None, description="Families to resolve. Defaults to the profile's required set.")):
    """Resolve each requested family to the one that will render, and say whether
    that is a substitution and whether the substitute is metric-compatible."""
    return fonts.health(family or REQUIRED_FONTS, FONT_PATHS or None)


@app.post("/equivalence", tags=["verification"], response_model=Equivalence,
          summary="Are these two builds the same document?",
          responses={422: {"description": "One of the uploads could not be read as a PDF."}})
async def post_equivalence(
    a: UploadFile = File(..., description="First PDF."),
    b: UploadFile = File(..., description="Second PDF. May share a filename with the first."),
    dpi: int = Query(72, ge=36, le=300, description="Raster DPI. Higher is stricter and slower."),
    sample: int = Query(6, ge=0, le=2000,
                        description="Pages to rasterise, spread across the document. 0 rasterises every page."),
):
    """Compare on four planes, strictest last: page count, extracted text with
    layout, rendered pixels, and the metadata that should be stable.

    Byte comparison is deliberately not offered — see the service description.
    """
    tmp = tempfile.mkdtemp(prefix="abcprint-eq-")
    try:
        paths = []
        for slot, up in (("a", a), ("b", b)):
            # Each upload goes in its OWN directory. Writing both into one
            # directory under their client-supplied basename collides whenever
            # the two builds share a filename — which is the NORMAL case here,
            # since comparing two builds of one document means comparing two
            # files called thesis-assembled.pdf. That collision made the
            # endpoint compare the second file with itself and report every
            # such pair as equivalent.
            slot_dir = os.path.join(tmp, slot)
            os.makedirs(slot_dir, exist_ok=True)
            dest = os.path.join(slot_dir, os.path.basename(up.filename or "in.pdf"))
            if os.path.commonpath([slot_dir, os.path.abspath(dest)]) != slot_dir:
                raise HTTPException(400, "invalid filename")
            with open(dest, "wb") as fh:
                shutil.copyfileobj(up.file, fh)
            paths.append(dest)
        try:
            rep = equivalence.compare(paths[0], paths[1], dpi=dpi, sample=sample)
        except Exception as exc:
            raise HTTPException(422, f"could not read one of the PDFs: {exc}")
        return rep.as_dict()
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ── compile ─────────────────────────────────────────────────────────────────

def _run_job_quarto(job_id: str, root: str, profile_ref: str, formats: list,
                    user_metadata: dict, entry: str) -> None:
    job = jobs.STORE.get(job_id)
    if job is None:
        return
    job.state, job.started = jobs.State.RUNNING, __import__("time").time()
    try:
        prof = profiles.load(profile_ref)
        paths = {"repo": root, "src": os.path.join(root, "src"),
                 "figures": os.path.join(root, "src", "figures"),
                 "build": os.path.join(root, "_build")}
        result = compiler.run_quarto(paths, prof, formats=formats,
                                     user_metadata=user_metadata, entry=entry)
        job.artifacts = result["artifacts"]
        job.compliant = result.get("compliant")
        job.manifest = result["manifest"]
        job.checks = result["checks"]
        job.log_tail = result["log_tail"]
        job.state = jobs.State.SUCCEEDED
    except Exception as exc:
        job.error = str(exc)
        job.log_tail = getattr(exc, "log_tail", []) or []
        job.state = jobs.State.FAILED
    finally:
        job.finished = __import__("time").time()


def _run_job(job_id: str, root: str, chapters, copy: str, run_checks: bool) -> None:
    """Background worker. Failures are recorded on the job, never swallowed."""
    job = jobs.STORE.get(job_id)
    if job is None:
        return
    job.state, job.started = jobs.State.RUNNING, __import__("time").time()
    try:
        paths = {"repo": root, "thesis": os.path.join(root, "writeup", "thesis"),
                 "figures": os.path.join(root, "writeup", "figures"),
                 "inserted": os.path.join(root, "writeup", "inserted-papers"),
                 "bin": os.path.join(root, "writeup", "bin"),
                 "quarto": os.path.join(root, "writeup", "thesis-quarto"),
                 "build": os.path.join(root, "writeup", "_build")}
        result = compiler.run(paths, chapters, copy=copy, run_checks=run_checks)
        job.artifacts = result["artifacts"]
        job.compliant = result.get("compliant")
        job.manifest = result["manifest"]
        job.checks = result["checks"]
        job.log_tail = result["log_tail"]
        job.state = jobs.State.SUCCEEDED
    except Exception as exc:
        job.error = str(exc)
        job.log_tail = getattr(exc, "log_tail", []) or []
        job.state = jobs.State.FAILED
    finally:
        job.finished = __import__("time").time()


@app.post("/compile", tags=["compile"], status_code=202,
          summary="Submit sources for compilation",
          responses={202: {"description": "Accepted. Poll `/jobs/{id}`."},
                     400: {"description": "The bundle was rejected; the message names why."}})
async def post_compile(
    background: BackgroundTasks,
    sources: list[UploadFile] = File(
        ..., description="Markdown files. Chapter files are named `NN-slug.md`; anything "
                         "else is treated as front matter."),
    figures: list[UploadFile] = File(default=[], description="Images referenced by the sources."),
    inserted: list[UploadFile] = File(
        default=[], description="Publisher PDFs spliced in whole, for manuscript-based chapters."),
    bibliography: list[UploadFile] = File(
        default=[], description="BibTeX files. `references.bib` is the main one; files named "
                                "`_<area>-refs-<date>.bib` are placed beside the chapters, where "
                                "the pipeline auto-includes them. Citations arrive as BibTeX; "
                                "that boundary is settled."),
    chapters: str | None = Form(
        default=None, description='Chapter prefixes to build, comma-separated, e.g. "01,04,07". '
                                  "Omit to build every chapter found."),
    copy: str = Form("examination", description='"examination" or "submission". The submission copy '
                                               "adds the branded title frame and the Afrikaans Opsomming."),
    checks: bool = Form(True, description="Run the verification suite. Leave on: verification is the product."),
):
    """Accept a bundle and start a build.

    The build is multi-pass — render once to measure where inserted papers land,
    re-render with that many pages reserved, then splice — so this returns **202**
    with a job id rather than blocking. A single synchronous `POST /compile`
    taking a markdown string could not express it.
    """
    if not sources:
        raise HTTPException(400, "no sources supplied")
    if not _engines_available()["thesis-assemble"]:
        raise HTTPException(
            503,
            "this image was built without the thesis pipeline, so the "
            "thesis-assemble engine is unavailable. Use POST /compile/manuscript, "
            "or rebuild with the pipeline vendored (see docs/building.md).")
    root = _tempfile.mkdtemp(prefix="abcprint-job-")
    try:
        paths = workspace.create(root)
        for up in sources:
            workspace.write_source(paths, up.filename, await up.read())
        for up in figures:
            workspace.write_asset(paths, "figure", up.filename, await up.read())
        for up in inserted:
            workspace.write_asset(paths, "inserted", up.filename, await up.read())
        for up in bibliography:
            workspace.write_asset(paths, "bib", up.filename or "references.bib", await up.read())
        found = workspace.detected_chapters(paths)
        wanted = [c.strip() for c in chapters.split(",") if c.strip()] if chapters else None
        if wanted:
            missing = [c for c in wanted if c not in found]
            if missing:
                raise HTTPException(400, f"requested chapters not in the bundle: {missing}; found {found}")
    except workspace.BundleError as exc:
        _shutil.rmtree(root, ignore_errors=True)
        raise HTTPException(400, str(exc))
    except HTTPException:
        _shutil.rmtree(root, ignore_errors=True)
        raise

    job = jobs.STORE.create({"chapters": wanted or found, "copy": copy, "checks": checks})
    job.workdir = root
    background.add_task(_run_job, job.id, root, wanted, copy, checks)
    return JSONResponse({**job.as_dict(), "detected_chapters": found}, status_code=202)


@app.get("/jobs", tags=["compile"], response_model=JobList, summary="Recent jobs")
def list_jobs(limit: int = Query(20, ge=1, le=200)):
    """Most recent jobs, newest first.

    The store is in-process and bounded, so jobs do not survive a restart of the
    service. A client that needs durable history should record the manifest it
    gets back rather than relying on this.
    """
    return {"jobs": [j.as_dict() for j in jobs.STORE.list(limit)]}


@app.get("/jobs/{job_id}", tags=["compile"], response_model=JobModel,
         summary="Job state, manifest and check report")
def get_job(job_id: str):
    """The manifest travels with the result: toolchain, font resolutions and the
    pinned epoch, so a rebuild is a meaningful claim. The check report travels
    with it too, rather than being left in a log nobody reads."""
    job = jobs.STORE.get(job_id)
    if job is None:
        raise HTTPException(404, "no such job")
    return job.as_dict()


@app.get("/jobs/{job_id}/artifacts/{name}", tags=["compile"],
         summary="Download an artefact",
         responses={200: {"content": {"application/octet-stream": {}},
                          "description": "The file."},
                    409: {"description": "The job has not succeeded."},
                    410: {"description": "The job's workspace has been reclaimed."}})
def get_artifact(job_id: str, name: str):
    """Stream one artefact by the `name` given in the job's `artifacts` list.

    Available only once the job has succeeded. A name that is not in that list is
    a 404 whose message names what the job did produce.
    """
    job = jobs.STORE.get(job_id)
    if job is None:
        raise HTTPException(404, "no such job")
    if job.state is not jobs.State.SUCCEEDED:
        raise HTTPException(409, f"job is {job.state.value}, not succeeded")
    path = _artifact_path(job, name)
    return FileResponse(path, filename=os.path.basename(path))


def _artifact_path(job, name: str) -> str:
    if not job.workdir:
        raise HTTPException(410, "job workspace has been reclaimed")
    known = {a["name"] for a in job.artifacts}
    if name not in known:
        raise HTTPException(404, f"no such artefact; this job produced {sorted(known)}")
    # The two engines lay out their workspaces differently; take whichever
    # build directory this job actually produced.
    build = os.path.join(job.workdir, "writeup", "_build")
    if not os.path.isdir(build):
        build = os.path.join(job.workdir, "_build")
    path = os.path.abspath(os.path.join(build, os.path.basename(name)))
    if os.path.commonpath([os.path.abspath(build), path]) != os.path.abspath(build) \
            or not os.path.isfile(path):
        raise HTTPException(404, "artefact is gone")
    return path


# ── delivery ────────────────────────────────────────────────────────────────

@app.get("/delivery/destinations", tags=["delivery"],
         summary="Which destinations this deployment can actually use")
def delivery_destinations():
    """Report configuration rather than let a delivery fail later with a surprise.

    PDF password protection is deliberately NOT implemented yet; it will attach
    here, at the delivery boundary.
    """
    return {
        "downloads": {"available": delivery.downloads_available(), "path": delivery.DOWNLOADS_DIR,
                      "hint": 'mount with -v "$HOME/Downloads":' + delivery.DOWNLOADS_DIR},
        "minio": {"configured": delivery.minio_configured(),
                  "hint": "set ABCPRINT_S3_ENDPOINT, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY"},
        "pdf_password_protection": {"implemented": False, "note": "deferred by decision"},
    }


@app.post("/jobs/{job_id}/deliver", tags=["delivery"], response_model=DeliveryResult,
          summary="Deliver an artefact",
          responses={503: {"description": "That destination is not configured on this deployment; "
                                          "check GET /delivery/destinations first."}})
def deliver(
    job_id: str,
    artifact: str = Form(..., description="Artefact name from the job's `artifacts` list."),
    destination: str = Form(..., description='"downloads" or "minio".'),
    bucket: str | None = Form(None, description="minio: target bucket."),
    key: str | None = Form(None, description="minio: object key, e.g. users/<you>/thesis.pdf"),
    subdir: str | None = Form(None, description="downloads: optional subfolder."),
    filename: str | None = Form(None, description="Override the delivered filename."),
):
    """Send a finished artefact to its destination.

    Two destinations. `downloads` copies into a directory the operator mounted
    into the container; `minio` puts the file at `s3://<bucket>/<key>` in the
    user's own prefix. Check `GET /delivery/destinations` first — an unconfigured
    destination returns **503** naming what the operator must set, rather than
    failing after the fact.

    PDF password protection is **not implemented**. It will attach here, at this
    boundary, when it is; until then a delivered file is not protected and a
    client should not imply otherwise.
    """
    job = jobs.STORE.get(job_id)
    if job is None:
        raise HTTPException(404, "no such job")
    if job.state is not jobs.State.SUCCEEDED:
        raise HTTPException(409, f"job is {job.state.value}, not succeeded")
    src = _artifact_path(job, artifact)
    try:
        if destination == "downloads":
            return delivery.to_downloads(src, filename or artifact, subdir)
        if destination == "minio":
            if not (bucket and key):
                raise HTTPException(400, "minio delivery needs both bucket and key")
            return delivery.to_minio(src, bucket, key)
        raise HTTPException(400, f'unknown destination {destination!r}; expected "downloads" or "minio"')
    except delivery.DeliveryError as exc:
        raise HTTPException(503, str(exc))


# ── profiles ────────────────────────────────────────────────────────────────

@app.get("/profiles", tags=["compile"], response_model=ProfileList,
         summary="Profiles this deployment offers")
def list_profiles():
    """A profile selects an ENGINE and configures it.

    `has_compliance_rules: false` means jobs under that profile report
    `compliant: null` — a profile with nothing to check must not claim compliance.
    """
    out = []
    for name in profiles.available():
        try:
            out.append(profiles.load(name).as_dict())
        except profiles.ProfileError as exc:
            out.append({"name": name, "error": str(exc)})
    return {"profiles": out,
            "refused_quarto_metadata_keys": profiles.REFUSED_METADATA}


@app.post("/compile/manuscript", tags=["compile"], status_code=202,
          summary="Compile a manuscript (quarto-render engine)",
          responses={202: {"description": "Accepted. Poll `/jobs/{id}`."},
                     400: {"description": "Bundle or quarto_metadata rejected; the message names why."}})
async def post_compile_manuscript(
    background: BackgroundTasks,
    sources: list[UploadFile] = File(
        ..., description="The entry `.qmd` plus any included files."),
    figures: list[UploadFile] = File(default=[], description="Figures, subpaths preserved."),
    bibliography: list[UploadFile] = File(default=[], description="BibTeX files."),
    profile: str = Form("biorxiv-dev", description="Profile reference, e.g. `biorxiv-dev@0.1`."),
    entry: str = Form("index.qmd", description="The document to render."),
    outputs: str = Form("pdf", description="Comma-separated: pdf, docx."),
    quarto_metadata: str | None = Form(
        default=None,
        description=(
            "ADVANCED. A YAML mapping merged into the `_metadata.yml` the render sees, "
            "after the profile's own keys, so it genuinely overrides. Keys that execute "
            "code or read a client-chosen path are refused by name — see "
            "`refused_quarto_metadata_keys` on GET /profiles. The effective metadata is "
            "returned in the job manifest, so what was merged is never a guess.")),
):
    """Render a manuscript with the quarto-render engine.

    Separate from `/compile` because it is a different engine, not a different
    option: there is no measure pass, no page reservation and no splice here.
    """
    try:
        prof = profiles.load(profile)
    except profiles.ProfileError as exc:
        raise HTTPException(400, str(exc))
    if prof.engine != "quarto-render":
        raise HTTPException(
            400, f"profile {prof.ref} uses the {prof.engine!r} engine; "
                 f"use POST /compile for that one")

    meta: dict = {}
    if quarto_metadata:
        try:
            import yaml as _yaml
            meta = _yaml.safe_load(quarto_metadata) or {}
        except Exception as exc:
            raise HTTPException(400, f"quarto_metadata is not valid YAML: {exc}")
        if not isinstance(meta, dict):
            raise HTTPException(400, "quarto_metadata must be a YAML mapping")
        refused = profiles.check_quarto_metadata(meta)
        if refused:
            # Refused up front rather than at render time: the user gets the reason
            # in milliseconds instead of after a build.
            raise HTTPException(400, "quarto_metadata rejected: " + "; ".join(refused))

    formats = [f.strip() for f in outputs.split(",") if f.strip()]
    root = _tempfile.mkdtemp(prefix="abcprint-ms-")
    try:
        paths = workspace.create_quarto(root, profiles._resolve_dir(profile))
        for up in sources:
            workspace.write_source(paths, up.filename, await up.read())
        for up in figures:
            workspace.write_asset(paths, "figure", up.filename, await up.read())
        for up in bibliography:
            workspace.write_asset(paths, "bib", up.filename or "references.bib", await up.read())
    except workspace.BundleError as exc:
        _shutil.rmtree(root, ignore_errors=True)
        raise HTTPException(400, str(exc))

    job = jobs.STORE.create({"profile": prof.ref, "engine": prof.engine,
                             "entry": entry, "outputs": formats,
                             "quarto_metadata_keys": sorted(meta)})
    job.workdir = root
    background.add_task(_run_job_quarto, job.id, root, profile, formats, meta, entry)
    return JSONResponse(job.as_dict(), status_code=202)
