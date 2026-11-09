"""The 0.14 API, kept request for request, as a second front onto dispatch runs.

An editor plugin and scripts call /compile, /jobs/{id} and the artefact URLs, so
those must keep working unchanged. None of it has its own machinery any more: a
multipart upload is stored as dispatch uploads, the run is created through the
kind's own validation and the SAME admission and executor as /v1/{kind}/runs,
and a job is a view of a run.

What a 0.14 client can observe, mapped:

    job.id            run_id
    job.state         queued <- queued | initializing
                      running <- running | canceling
                      succeeded <- complete
                      failed <- executor_error | system_error | canceled | preempted
    job.compliant, checks, manifest, artifacts, log_tail   <- run.outcome
    job.error         <- run.state_reason, when failed
    job.options       what was requested; `identity`, `archive` and the resolved
                      `chapters` are filled in once the worker has built the
                      workspace (0.14 had them from submission; the 202 still does)

Status codes are 0.14's: a refused bundle is 400 (the native API says 422), a
missing scope 403, an expired artefact 410, a full queue 429 with Retry-After.
Bodies carry the kit's envelope (`error`, `msg`, `status_code`) AND `detail`,
which is what a 0.14 client reads.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from pathlib import Path
from typing import Mapping, Optional

import yaml
from fastapi import Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from pydantic import ValidationError

from abc_dispatch import ApiError, State
from abc_dispatch.ids import new_id
from abc_dispatch.states import TERMINAL
from abc_dispatch.store import RunRecord, UploadRecord

from . import auth, capabilities, delivery, profile as profiles, workspace
from .models import (DeliveryResult, Equivalence, FontHealth, Health, Job as JobModel,
                     JobList, ProfileList, Toolchain as ToolchainModel)
from .service import (EquivalenceRequest, FileRef, ManuscriptRequest, ThesisRequest, _Refused,
                      _err, prepare_manuscript, prepare_thesis)

_STATE = {
    State.QUEUED: "queued", State.INITIALIZING: "queued",
    State.RUNNING: "running", State.CANCELING: "running",
    State.COMPLETE: "succeeded",
}
_DISK_NAME = re.compile(r"[^A-Za-z0-9._-]")

_bearer = HTTPBearer(
    scheme_name="abc-print-svc token", bearerFormat="abcp_…", auto_error=False,
    description=("A token minted with `python -m abcprint.tokens new <label>`. Scopes decide "
                 "what it opens: `compile`, `read`, `deliver`."))

AUTH_RESPONSES: dict = {
    401: {"description": "No token, or one this deployment does not recognise. Fix the token."},
    403: {"description": "The token is valid but lacks the scope this call needs."},
    503: {"description": "This deployment has no tokens configured, so it can authorise nobody."},
}
COMPILE_RESPONSES: dict = {
    **AUTH_RESPONSES,
    429: {"description": "Too many builds are already admitted. Honour `Retry-After`."},
}


def job_json(run: RunRecord, extra: dict | None = None) -> dict:
    out = run.outcome or {}
    state = _STATE.get(run.state, "failed")
    req = run.request or {}
    options = dict(out.get("options") or _requested_options(run.kind, req))
    finished = run.finished_at
    started = run.started_at
    body = {
        "id": run.run_id, "state": state, "owner": run.owner.principal,
        "created": run.created_at, "started": started, "finished": finished,
        "duration_s": round((finished or time.time()) - (started or run.created_at), 1),
        "options": options,
        "artifacts": out.get("artifacts", []) if run.state == State.COMPLETE else [],
        "compliant": out.get("compliant"),
        "manifest": out.get("manifest", {}),
        "checks": out.get("checks", {}),
        "error": run.state_reason if state == "failed" else None,
        # On failure fall back to the live log, so a run that died before the
        # engine reported still says something.
        "log_tail": ((out.get("log_tail") or list(run.log_lines)[-60:])
                     if state == "failed" else out.get("log_tail", [])),
    }
    if extra:
        body.update(extra)
    return body


def _ref(name: str | None) -> str | None:
    """The profile as 0.14 reported it: resolved to name@version."""
    try:
        return profiles.load(name).ref if name else name
    except profiles.ProfileError:
        return name


def _requested_options(kind: str, req: dict) -> dict:
    if kind == "thesis":
        return {"profile": _ref(req.get("profile")), "engine": "thesis-assemble",
                "chapters": req.get("chapters"), "copy": req.get("copy"),
                "checks": req.get("checks")}
    return {"profile": _ref(req.get("profile")), "engine": "quarto-render",
            "entry": req.get("entry") or "index.qmd", "outputs": req.get("outputs"),
            "quarto_metadata_keys": sorted(req.get("quarto_metadata") or {})}


def mount_legacy(app, *, store, storage, admission, executor, kinds: dict, authenticator,
                 settings, cors_origins: list[str], env: Mapping[str, str]) -> None:
    limiter = app.state.rate_limiter
    eq_wait_s = float(env.get("ABCPRINT_EQUIVALENCE_WAIT_S", "600"))

    def client_ip(request: Request) -> str:
        return getattr(request.state, "client_ip", None) or limiter.client_ip(
            request.client.host if request.client else None, request.headers.get("x-forwarded-for"))

    def require(scope: str):
        def dep(request: Request,
                _: Optional[HTTPAuthorizationCredentials] = Depends(_bearer)) -> auth.Principal:
            ip = client_ip(request)
            limiter.auth_failures.blocked(ip)
            try:
                p = authenticator.principal(request)
            except ApiError as e:
                if e.status == 401:
                    limiter.auth_failures.spend(ip)
                raise
            authenticator.require(p, scope)
            limiter.for_caller(request.method).take(p.label)
            return p
        return dep

    # --- uploads and submission, through the kit's own pieces ----------------

    async def stage(principal: str, up: UploadFile, role: str) -> tuple[UploadRecord, str]:
        """Store one multipart file as a dispatch upload. Returns it and the name the
        document gives it (which may hold a subpath, so it is not the disk name)."""
        given = up.filename or ""
        disk = (_DISK_NAME.sub("_", os.path.basename(given)) or role).lstrip("._-") or role
        upload_id = new_id("upl")
        dest = storage.upload_path(principal, upload_id, disk[:120])
        n, sha = storage.save_stream(dest, up.file, settings.max_upload_bytes)
        now = time.time()
        rec = UploadRecord(upload_id, principal, dest.name, dest, n, sha, now,
                           now + settings.upload_ttl_s)
        store.add_upload(rec)
        return rec, given

    def drop(uploads: list[UploadRecord]) -> None:
        for up in uploads:
            if store.pop_upload(up.upload_id) is not None:
                storage.delete(up.path.parent, reason="upload of a refused 0.14 request",
                               upload_id=up.upload_id)

    def submit(principal: auth.Principal, kind_name: str, body, uploads: list[UploadRecord]) -> RunRecord:
        """What POST /v1/{kind}/runs does, for a request built here."""
        kind = kinds[kind_name]
        caller = authenticator.caller(principal.label)
        by_path: dict[str, str] = {}

        def resolve(ref: str) -> Path:
            up = store.get_upload(ref, caller.principal)
            if up is None:
                raise _err(400, "invalid_request", f"unknown upload {ref!r}")
            by_path[str(up.path)] = ref
            return up.path

        request_json = body.model_dump(mode="json")
        try:
            inputs = kind.validate(body, resolve)
            with store.lock:
                admission.check(caller.principal)
                run = RunRecord(
                    run_id=new_id("run"), kind=kind.name, owner=caller, state=State.QUEUED,
                    request=request_json,
                    request_hash=hashlib.sha256(json.dumps(request_json, sort_keys=True)
                                                .encode()).hexdigest(),
                    created_at=time.time(),
                    input_map={k: by_path[str(p)] for k, p in inputs.items() if str(p) in by_path})
                store.add_run(run)
        except ApiError as e:
            drop(uploads)
            # 0.14 answered a refused bundle with 400; everything else keeps its status.
            status = 400 if e.status == 422 else e.status
            raise _err(status, e.error, e.msg, headers=e.headers,
                       **{k: v for k, v in e.extra.items() if k != "detail"})
        except BaseException:
            drop(uploads)
            raise
        executor.submit(run, kind, body, inputs)
        return run

    def precheck(prepare, req, uploads_by_key: dict[str, Path]) -> dict:
        """The 0.14 202 carried detected_chapters and the archive mapping; work them
        out the same way the kind's validation does."""
        scratch = tempfile.mkdtemp(prefix="abcprint-precheck-")
        try:
            return prepare(req, uploads_by_key, scratch, assets=False)[1]
        except _Refused:
            return {}
        finally:
            import shutil
            shutil.rmtree(scratch, ignore_errors=True)

    def owned_job(job_id: str, principal: auth.Principal) -> RunRecord:
        run = store.get_run(job_id, principal.label)
        if run is None or run.kind == "equivalence":
            raise _err(404, "not_found", "no such job")
        return run

    def artifact_path(run: RunRecord, name: str) -> str:
        if run.state != State.COMPLETE:
            raise _err(409, "not_succeeded", f"job is {_STATE.get(run.state, 'failed')}, not succeeded")
        if run.deleted_at is not None or run.expired or \
                (run.expires_at is not None and run.expires_at <= time.time()):
            raise _err(410, "run_expired", "job workspace has been reclaimed")
        out = next((o for o in run.outputs if o.name == os.path.basename(name)), None)
        if out is None:
            known = sorted(o.name for o in run.outputs)
            raise _err(404, "not_found", f"no such artefact; this job produced {known}")
        if not Path(out.path).is_file():
            raise _err(404, "not_found", "artefact is gone")
        return str(out.path)

    def accepted(run: RunRecord, extra: dict | None = None) -> JSONResponse:
        return JSONResponse(job_json(run, extra), status_code=202,
                            headers={"Location": f"/jobs/{run.run_id}"})

    # --- health and discovery -------------------------------------------------

    @app.get("/", include_in_schema=False)
    def index():
        return {"service": "abc-print-svc", "version": settings.service_version,
                "reference": "/redoc", "try_it_out": "/docs", "schema": "/openapi.json"}

    @app.get("/health", tags=["health"], response_model=Health,
             summary="Can this deployment build?",
             responses={503: {"description": "Deployment cannot build; `problems` names each reason."}})
    def health():
        rec = capabilities.load()
        if rec is None:
            problems = [f"no worker capability record ({capabilities.ENV_VAR}); this front door "
                        f"cannot tell what its worker can build"]
            tc, fh, engines = {}, {"ok": False}, {}
        else:
            fh = capabilities.font_health(rec["fonts"]["required"])
            tc = rec["toolchain"]
            engines = rec["engines_available"]
            problems = list(tc.get("problems") or []) + \
                ([] if fh["ok"] else [f"fonts unavailable: {fh['missing']}"])
        totals = store.totals()
        body = {"ok": not problems, "problems": problems,
                "toolchain": {k: v for k, v in tc.items() if k not in ("problems", "typst_mismatch")},
                "typst_mismatch": tc.get("typst_mismatch"), "fonts": fh,
                "cors_allowed_origins": cors_origins, "engines_available": engines,
                "auth": authenticator.state(),
                "capacity": {"max_renders": admission.max_running,
                             "max_queued": admission.max_queued, **totals},
                "executor": executor.name, "store": store.kind}
        return JSONResponse(body, status_code=200 if not problems else 503)

    @app.get("/toolchain", tags=["health"], response_model=ToolchainModel,
             summary="What rendered this document?", responses=AUTH_RESPONSES)
    def get_toolchain(p: auth.Principal = Depends(require("read"))):
        rec = capabilities.load() or {}
        tc = rec.get("toolchain") or {}
        return {**{k: v for k, v in tc.items() if k != "problems"},
                "pipeline": rec.get("pipeline")}

    @app.get("/fonts", tags=["health"], response_model=FontHealth,
             summary="Which family will actually render?", responses=AUTH_RESPONSES)
    def get_fonts(family: list[str] | None = Query(
            default=None, description="Families to resolve. Defaults to the profile's required set."),
            p: auth.Principal = Depends(require("read"))):
        rec = capabilities.load() or {}
        return capabilities.font_health(family or (rec.get("fonts") or {}).get("required")
                                        or capabilities.REQUIRED_FONTS)

    @app.get("/profiles", tags=["compile"], response_model=ProfileList,
             summary="Profiles this deployment offers", responses=AUTH_RESPONSES)
    def list_profiles(p: auth.Principal = Depends(require("read"))):
        out = []
        for name in profiles.available():
            try:
                out.append(profiles.load(name).as_dict())
            except profiles.ProfileError as exc:
                out.append({"name": name, "error": str(exc)})
        return {"profiles": out, "refused_quarto_metadata_keys": profiles.REFUSED_METADATA}

    # --- compile ----------------------------------------------------------------

    def parse_meta(raw: str | None) -> dict:
        if not raw:
            return {}
        try:
            meta = yaml.safe_load(raw) or {}
        except Exception as exc:
            raise _err(400, "invalid_request", f"quarto_metadata is not valid YAML: {exc}")
        if not isinstance(meta, dict):
            raise _err(400, "invalid_request", "quarto_metadata must be a YAML mapping")
        return meta

    def chapter_list(raw: str | None) -> list[str] | None:
        return [c.strip() for c in raw.split(",") if c.strip()] if raw else None

    async def stage_role(p, files: list[UploadFile], role: str, uploads: list, keyed: dict) -> list[FileRef]:
        refs = []
        for i, up in enumerate(files):
            rec, given = await stage(p.label, up, role)
            uploads.append(rec)
            keyed[f"{role}.{i}"] = rec.path
            refs.append(FileRef(upload=rec.upload_id, name=given or None))
        return refs

    def build_request(model, **kw):
        """The request model, or a 400 naming each field that is wrong.

        One deliberate difference from 0.14: an invalid `copy` used to be accepted
        with 202 and fail in the worker; it is now refused here, before admission,
        with the allowed values in the message.
        """
        try:
            return model(**kw)
        except ValidationError as exc:
            problems = [f"{'.'.join(str(p) for p in e['loc']) or 'request'}: {e['msg']}"
                        for e in exc.errors()]
            raise _err(400, "invalid_request", "; ".join(problems), problems=problems)

    @app.post("/compile", tags=["compile"], status_code=202,
              summary="Submit sources for compilation (thesis-assemble)",
              responses={202: {"description": "Accepted. Poll `/jobs/{id}`."},
                         400: {"description": "The bundle was rejected; the message names why."},
                         **COMPILE_RESPONSES})
    async def post_compile(
        sources: list[UploadFile] = File(..., description="Markdown files. Chapters are `NN-slug.md`."),
        figures: list[UploadFile] = File(default=[]),
        inserted: list[UploadFile] = File(default=[]),
        bibliography: list[UploadFile] = File(default=[]),
        chapters: str | None = Form(default=None, description='e.g. "01,04,07". Omit for all.'),
        copy: str = Form("examination", description='"examination", "submission" or "library".'),
        checks: bool = Form(True),
        p: auth.Principal = Depends(require("compile")),
    ):
        if not sources:
            raise _err(400, "invalid_request", "no sources supplied")
        uploads: list[UploadRecord] = []
        keyed: dict[str, Path] = {}
        try:
            req = build_request(
                ThesisRequest, sources=await stage_role(p, sources, "sources", uploads, keyed),
                figures=await stage_role(p, figures, "figures", uploads, keyed),
                inserted=await stage_role(p, inserted, "inserted", uploads, keyed),
                bibliography=await stage_role(p, bibliography, "bibliography", uploads, keyed),
                chapters=chapter_list(chapters), copy=copy, checks=checks)
        except BaseException:
            drop(uploads)
            raise
        info = precheck(prepare_thesis, req, keyed)
        run = submit(p, "thesis", req, uploads)
        return accepted(run, {"detected_chapters": info.get("detected_chapters", []),
                              "options": {**job_json(run)["options"],
                                          "chapters": req.chapters or info.get("detected_chapters"),
                                          "identity": info.get("identity")}})

    @app.post("/compile/manuscript", tags=["compile"], status_code=202,
              summary="Compile a manuscript (quarto-render engine)",
              responses={202: {"description": "Accepted. Poll `/jobs/{id}`."},
                         400: {"description": "Bundle or quarto_metadata rejected."},
                         **COMPILE_RESPONSES})
    async def post_compile_manuscript(
        sources: list[UploadFile] = File(..., description="The entry `.qmd` plus any included files."),
        figures: list[UploadFile] = File(default=[]),
        bibliography: list[UploadFile] = File(default=[]),
        profile: str = Form("biorxiv-dev"),
        entry: str = Form("index.qmd"),
        outputs: str = Form("pdf", description="Comma-separated: pdf, docx."),
        quarto_metadata: str | None = Form(default=None),
        p: auth.Principal = Depends(require("compile")),
    ):
        meta = parse_meta(quarto_metadata)
        uploads: list[UploadRecord] = []
        keyed: dict[str, Path] = {}
        try:
            req = build_request(
                ManuscriptRequest, profile=profile, entry=entry,
                outputs=[f.strip() for f in outputs.split(",") if f.strip()], quarto_metadata=meta,
                sources=await stage_role(p, sources, "sources", uploads, keyed),
                figures=await stage_role(p, figures, "figures", uploads, keyed),
                bibliography=await stage_role(p, bibliography, "bibliography", uploads, keyed))
        except BaseException:
            drop(uploads)
            raise
        return accepted(submit(p, "manuscript", req, uploads))

    @app.post("/compile/archive", tags=["compile"], status_code=202,
              summary="Compile from a zip archive (project-garden layout)",
              responses={202: {"description": "Accepted. Poll `/jobs/{id}`."},
                         400: {"description": "The archive was refused; the message names why."},
                         **COMPILE_RESPONSES})
    async def post_compile_archive(
        bundle: UploadFile = File(..., description="A .zip of the manuscript directory."),
        profile: str = Form("biorxiv-dev", description="e.g. `biorxiv-dev@0.1` or `su-fmhs`."),
        entry: str | None = Form(default=None),
        outputs: str = Form("pdf"),
        quarto_metadata: str | None = Form(default=None),
        chapters: str | None = Form(default=None),
        copy: str = Form("examination"),
        checks: bool = Form(True),
        p: auth.Principal = Depends(require("compile")),
    ):
        try:
            prof = profiles.load(profile)
        except profiles.ProfileError as exc:
            raise _err(400, "invalid_request", str(exc))
        if prof.engine not in ("quarto-render", "thesis-assemble"):
            raise _err(400, "invalid_request", f"profile {prof.ref} uses the {prof.engine!r} engine")
        meta = parse_meta(quarto_metadata) if prof.engine == "quarto-render" else {}
        rec, _ = await stage(p.label, bundle, "bundle")
        keyed = {"bundle": rec.path}
        try:
            if prof.engine == "thesis-assemble":
                req = build_request(ThesisRequest, profile=profile, bundle=rec.upload_id,
                                    chapters=chapter_list(chapters), copy=copy, checks=checks)
                info = precheck(prepare_thesis, req, keyed)
                run = submit(p, "thesis", req, [rec])
                return accepted(run, {
                    "detected_chapters": info.get("detected_chapters", []),
                    "options": {**job_json(run)["options"],
                                "chapters": req.chapters or info.get("detected_chapters"),
                                "identity": info.get("identity"), "archive": info.get("archive")}})
            req = build_request(ManuscriptRequest, profile=profile, bundle=rec.upload_id,
                                entry=entry, quarto_metadata=meta,
                                outputs=[f.strip() for f in outputs.split(",") if f.strip()])
            info = precheck(prepare_manuscript, req, keyed)
            run = submit(p, "manuscript", req, [rec])
            return accepted(run, {"options": {**job_json(run)["options"],
                                              "entry": info.get("entry"),
                                              "archive": info.get("archive")}})
        except BaseException:
            drop([rec])
            raise

    @app.get("/jobs", tags=["compile"], response_model=JobList, summary="Recent jobs",
             responses=AUTH_RESPONSES)
    def list_jobs(limit: int = Query(20, ge=1, le=200), p: auth.Principal = Depends(require("read"))):
        runs: list[RunRecord] = []
        for kind in ("thesis", "manuscript"):
            page, _ = store.list_runs(p.label, kind, page_size=limit)
            runs += page
        runs.sort(key=lambda r: r.created_at, reverse=True)
        return {"jobs": [job_json(r) for r in runs[:limit]]}

    @app.get("/jobs/{job_id}", tags=["compile"], response_model=JobModel,
             summary="Job state, manifest and check report", responses=AUTH_RESPONSES)
    def get_job(job_id: str, p: auth.Principal = Depends(require("read"))):
        return job_json(owned_job(job_id, p))

    @app.get("/jobs/{job_id}/artifacts/{name}", tags=["compile"], summary="Download an artefact",
             responses={200: {"content": {"application/octet-stream": {}}},
                        409: {"description": "The job has not succeeded."},
                        410: {"description": "The job's workspace has been reclaimed."},
                        **AUTH_RESPONSES})
    def get_artifact(job_id: str, name: str, p: auth.Principal = Depends(require("read"))):
        path = artifact_path(owned_job(job_id, p), name)
        return FileResponse(path, filename=os.path.basename(path))

    # --- delivery ---------------------------------------------------------------

    @app.get("/delivery/destinations", tags=["delivery"],
             summary="Which destinations this deployment can actually use", responses=AUTH_RESPONSES)
    def delivery_destinations(p: auth.Principal = Depends(require("deliver"))):
        return {
            "downloads": {"available": delivery.downloads_available(), "path": delivery.DOWNLOADS_DIR,
                          "hint": 'mount with -v "$HOME/Downloads":' + delivery.DOWNLOADS_DIR},
            "minio": {"configured": delivery.minio_configured(),
                      "hint": "set ABCPRINT_S3_ENDPOINT, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY"},
            "pdf_password_protection": {"implemented": False, "note": "deferred by decision"},
        }

    @app.post("/jobs/{job_id}/deliver", tags=["delivery"], response_model=DeliveryResult,
              summary="Deliver an artefact",
              responses={503: {"description": "That destination is not configured here."},
                         **AUTH_RESPONSES})
    def deliver(job_id: str,
                artifact: str = Form(...), destination: str = Form(...),
                bucket: str | None = Form(None), key: str | None = Form(None),
                subdir: str | None = Form(None), filename: str | None = Form(None),
                p: auth.Principal = Depends(require("deliver"))):
        src = artifact_path(owned_job(job_id, p), artifact)
        try:
            if destination == "downloads":
                return delivery.to_downloads(src, filename or artifact, subdir, owner=p.label)
            if destination == "minio":
                if not (bucket and key):
                    raise _err(400, "invalid_request", "minio delivery needs both bucket and key")
                return delivery.to_minio(src, bucket, key, owner=p.label)
            raise _err(400, "invalid_request",
                       f'unknown destination {destination!r}; expected "downloads" or "minio"')
        except delivery.DeliveryError as exc:
            raise _err(503, "destination_unavailable", str(exc))

    # --- equivalence: a run, answered synchronously as 0.14 did -----------------

    @app.post("/equivalence", tags=["verification"], response_model=Equivalence,
              summary="Are these two builds the same document?",
              responses={422: {"description": "One of the uploads could not be read as a PDF."},
                         **AUTH_RESPONSES})
    async def post_equivalence(
        a: UploadFile = File(..., description="First PDF."),
        b: UploadFile = File(..., description="Second PDF. May share a filename with the first."),
        dpi: int = Query(72, ge=36, le=300), sample: int = Query(6, ge=0, le=2000),
        p: auth.Principal = Depends(require("read")),
    ):
        ra, _ = await stage(p.label, a, "a")
        rb, _ = await stage(p.label, b, "b")
        req = EquivalenceRequest(a=ra.upload_id, b=rb.upload_id, dpi=dpi, sample=sample)
        run = submit(p, "equivalence", req, [ra, rb])
        import asyncio
        deadline = time.monotonic() + eq_wait_s
        while run.state not in TERMINAL:
            if time.monotonic() > deadline:
                raise _err(504, "timed_out",
                           f"the comparison did not finish within {eq_wait_s:g}s; it is run "
                           f"{run.run_id} at /v1/equivalence/runs/{run.run_id}")
            await asyncio.sleep(0.5)
            run = store.get_run(run.run_id, p.label) or run
        if run.state == State.COMPLETE:
            return run.outcome
        if run.state == State.EXECUTOR_ERROR:
            raise _err(422, "invalid_request", run.state_reason or "could not read one of the PDFs")
        raise _err(503, "comparison_failed", run.state_reason or f"the comparison {run.state.value}")
