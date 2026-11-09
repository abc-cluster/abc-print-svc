"""abc-print-svc on abc-dispatch.

Two tiers, one codebase:

* the FRONT DOOR (this module's ``build()``) validates, admits and queues
  requests, and dispatches each run as a Nomad job. It carries no profile, no
  pipeline and no toolchain;
* the WORKER (``python -m abc_dispatch.worker --kind abcprint.service:make_kind``)
  runs one compilation inside the image that does carry them — the way Nextflow
  runs each task in its own container.

Three kinds, because they are three different things with three request schemas:
``thesis`` (the thesis-assemble engine), ``manuscript`` (quarto-render) and
``equivalence`` (are two builds the same document). The native API is the kit's
``/v1/{kind}/runs``.

The 0.14 API — ``/compile``, ``/compile/archive``, ``/compile/manuscript``,
``/jobs``, artefacts, delivery, ``/profiles``, ``/equivalence`` and the health
endpoints — is kept, request for request, because an editor plugin and scripts
call it. It is a second front onto the SAME runs: a multipart request is stored
as uploads and created as a run through the same validation, admission and
executor as the native API, so a job id is a run id and both views agree.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import re
import shutil
import tempfile
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal, Mapping, Optional

from fastapi import Depends, File, Form, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, model_validator

from abc_dispatch import ApiError, Cancelled, ExecutorFailure, Kind, KindContext, KindResult, State
from abc_dispatch.admission import Admission
from abc_dispatch.app import ServiceSettings, create_app
from abc_dispatch.cli import run_tool
from abc_dispatch.executor import InProcessExecutor, NomadExecutor, Sweeper
from abc_dispatch.identity import Authenticator, Caller, _bearer
from abc_dispatch.ids import new_id
from abc_dispatch.nomad import NomadClient
from abc_dispatch.ratelimit import Rate, RateLimiter, RateLimits
from abc_dispatch.states import ACTIVE, TERMINAL
from abc_dispatch.storage import LocalStorage
from abc_dispatch.store import RunRecord, Store, UploadRecord

from . import (archive, auth, capabilities, compile as compiler, delivery, equivalence,
               identity, profile as profiles, workspace)
from .runner import TimedOut

__version__ = "0.15.0"

GRACE_S = float(os.environ.get("ABCPRINT_GRACE_S", "30"))
KINDS = ("thesis", "manuscript", "equivalence")


# ── requests ────────────────────────────────────────────────────────────────

class FileRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    upload: str = Field(..., description="An `upload_id` from `POST /v1/uploads`.")
    name: Optional[str] = Field(
        None, description="Where the file goes in the document, e.g. `01-introduction.md` "
                          "or `figures/current/F1.1.svg`. Subpaths are kept for figures. "
                          "Defaults to the uploaded file's name.")


class _Sources(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bundle: Optional[str] = Field(
        None, description="An `upload_id` of a .zip in the project-garden layout "
                          "(`src/…`, `assets/figures/…`). Give this OR the file lists.")
    sources: list[FileRef] = Field(default_factory=list, description="Markdown / .qmd files.")
    figures: list[FileRef] = Field(default_factory=list, description="Images the sources reference.")
    bibliography: list[FileRef] = Field(
        default_factory=list, description="BibTeX. `references.bib` is the main one; "
                                          "`_<area>-refs-<date>.bib` sit beside the chapters.")

    @model_validator(mode="after")
    def _one_shape(self):
        if bool(self.bundle) == bool(self.sources):
            raise ValueError("give either `bundle` (a zip) or `sources` (and optional "
                             "figures/bibliography), not both and not neither")
        if self.bundle and (self.figures or self.bibliography):
            raise ValueError("a `bundle` carries its own figures and bibliography")
        return self


class ThesisRequest(_Sources):
    """A thesis build with the thesis-assemble engine."""
    profile: str = Field("su-fmhs", description="A thesis-assemble profile, e.g. `su-fmhs`.")
    inserted: list[FileRef] = Field(
        default_factory=list, description="Publisher PDFs spliced in whole (manuscript-based chapters).")
    chapters: Optional[list[str]] = Field(
        None, description='Chapter prefixes to build, e.g. ["01","04"]. Omit to build all.',
        examples=[["01"]])
    copy: Literal["examination", "submission", "library"] = Field(
        "examination", description="The submission copy adds the Afrikaans Opsomming; the "
                                   "library deposit adds the branded title frame as well.")
    checks: bool = Field(True, description="Run the verification suite. Verification is the product.")

    @model_validator(mode="after")
    def _inserted_needs_files(self):
        if self.bundle and self.inserted:
            raise ValueError("a `bundle` carries its own inserted papers")
        return self


class ManuscriptRequest(_Sources):
    """A manuscript render with the quarto-render engine."""
    profile: str = Field("biorxiv-dev", description="A quarto-render profile, e.g. `biorxiv-dev`.")
    entry: Optional[str] = Field(
        None, description="Document to render, relative to `src/`. Defaults to `index.qmd`.")
    outputs: list[Literal["pdf", "docx"]] = Field(["pdf"], min_length=1)
    quarto_metadata: dict = Field(
        default_factory=dict,
        description="ADVANCED. Merged over the profile's metadata. Keys that execute code or "
                    "read a client-chosen path are refused by name (see the profiles list).")


class EquivalenceRequest(BaseModel):
    """Are two builds the same document? Four planes, strictest last."""
    model_config = ConfigDict(extra="forbid")
    a: str = Field(..., description="`upload_id` of the first PDF.")
    b: str = Field(..., description="`upload_id` of the second PDF.")
    dpi: int = Field(72, ge=36, le=300, description="Raster DPI. Higher is stricter and slower.")
    sample: int = Field(6, ge=0, le=2000,
                        description="Pages to rasterise, spread across the document. 0 = every page.")


# ── preparing a workspace — identical in the front door and the worker ─────
#
# The front door runs these with assets=False, into a scratch directory, purely
# to refuse a bad bundle before it is admitted. The worker runs them with
# assets=True to build the real workspace. Same code, so a bundle the front door
# accepts is a bundle the worker can build.

class _Refused(ValueError):
    pass


def _named(refs: list[FileRef], role: str, inputs: Mapping[str, Path]):
    for i, ref in enumerate(refs):
        path = inputs[f"{role}.{i}"]
        yield (ref.name or path.name), path


def _identity(paths: dict) -> dict:
    fp = os.path.join(paths["thesis"], identity.FILENAME)
    if not os.path.isfile(fp):
        return {"source": "template defaults", "keys": []}
    return {"source": identity.FILENAME, "keys": sorted(identity.read(fp))}


def prepare_thesis(req: ThesisRequest, inputs: Mapping[str, Path], root: str, *,
                   assets: bool, profile_dir: str | None = None) -> tuple[dict, dict]:
    try:
        archive_desc = None
        if req.bundle:
            paths, mapping, manifest = workspace.create_from_archive(
                root, str(inputs["bundle"]), assets=assets, profile_dir=profile_dir)
            archive_desc = workspace.describe_thesis(manifest, mapping)
        else:
            paths = workspace.create(root, assets=assets, profile_dir=profile_dir)
            for name, p in _named(req.sources, "sources", inputs):
                workspace.write_source(paths, name, p.read_bytes())
            for name, p in _named(req.figures, "figures", inputs):
                workspace.write_asset(paths, "figure", name, p.read_bytes())
            for name, p in _named(req.inserted, "inserted", inputs):
                workspace.write_asset(paths, "inserted", name, p.read_bytes())
            for name, p in _named(req.bibliography, "bibliography", inputs):
                workspace.write_asset(paths, "bib", name or "references.bib", p.read_bytes())
        found = workspace.detected_chapters(paths)
        ident = _identity(paths)
    except (archive.ArchiveError, workspace.BundleError, identity.IdentityError) as exc:
        raise _Refused(str(exc)) from None
    if req.chapters:
        missing = [c for c in req.chapters if c not in found]
        if missing:
            where = "archive" if req.bundle else "bundle"
            raise _Refused(f"requested chapters not in the {where}: {missing}; found {found}")
    return paths, {"detected_chapters": found, "identity": ident, "archive": archive_desc}


def prepare_manuscript(req: ManuscriptRequest, inputs: Mapping[str, Path], root: str, *,
                       assets: bool, profile_dir: str = "") -> tuple[dict, dict]:
    refused = profiles.check_quarto_metadata(req.quarto_metadata)
    if refused:
        raise _Refused("quarto_metadata rejected: " + "; ".join(refused))
    try:
        archive_desc = None
        if req.bundle:
            paths, manifest = workspace.create_quarto_from_archive(
                root, profile_dir, str(inputs["bundle"]), assets=assets)
            entry = workspace.resolve_entry(root, manifest, req.entry)
            archive_desc = archive.describe(manifest, f"src/{entry}")
        else:
            paths = workspace.create_quarto(root, profile_dir, assets=assets)
            for name, p in _named(req.sources, "sources", inputs):
                workspace.write_source(paths, name, p.read_bytes())
            for name, p in _named(req.figures, "figures", inputs):
                workspace.write_asset(paths, "figure", name, p.read_bytes())
            for name, p in _named(req.bibliography, "bibliography", inputs):
                workspace.write_asset(paths, "bib", name or "references.bib", p.read_bytes())
            entry = req.entry or "index.qmd"
    except (archive.ArchiveError, workspace.BundleError) as exc:
        raise _Refused(str(exc)) from None
    return paths, {"entry": entry, "archive": archive_desc}


# ── kinds ───────────────────────────────────────────────────────────────────

def _resolve_inputs(req: _Sources, resolve, roles: tuple[str, ...]) -> dict[str, Path]:
    inputs: dict[str, Path] = {}
    if req.bundle:
        inputs["bundle"] = resolve(req.bundle)
    for role in roles:
        for i, ref in enumerate(getattr(req, role)):
            inputs[f"{role}.{i}"] = resolve(ref.upload)
    return inputs


def _invalid(msg: str) -> ApiError:
    return ApiError(422, "invalid_request", msg, problems=[msg])


def _engine_ready(engine: str) -> None:
    rec = capabilities.load()
    avail = (rec or {}).get("engines_available") or capabilities.engines_available()
    if not avail.get(engine):
        raise ApiError(503, "engine_unavailable",
                       f"the worker image was built without the {engine} engine "
                       f"(see docs/building.md)")


def _profile_for(ref: str, engine: str, other_kind: str):
    try:
        prof = profiles.load(ref)
    except profiles.ProfileError as exc:
        raise _invalid(str(exc))
    if prof.engine != engine:
        raise _invalid(f"profile {prof.ref} uses the {prof.engine!r} engine; "
                       f"use a {other_kind} run for that one")
    return prof


def _tool_runner(ctx: KindContext):
    """The runner the engines call, built on run_tool: a cancelled run stops
    its build, and the output streams into the run log while it is kept whole
    for the check-report parser."""
    def run(cmd, *, cwd, env, timeout_s):
        lines: list[str] = []

        def tee(line: str) -> None:
            lines.append(line)
            ctx.log(line)

        res = run_tool(cmd, dataclasses.replace(ctx, log=tee), cwd=Path(cwd),
                       timeout_s=timeout_s, grace_s=GRACE_S, env=env)
        if res.status == "timed_out":
            raise TimedOut()
        return (res.returncode if res.returncode is not None else 1), "\n".join(lines)
    return run


def _publish(build_dir: str, artifacts: list[dict], outdir: Path) -> list[Path]:
    out = []
    for a in artifacts:
        src = os.path.join(build_dir, os.path.basename(a["name"]))
        shutil.copy2(src, outdir / os.path.basename(a["name"]))
        out.append(Path(os.path.basename(a["name"])))
    return out


class ThesisKind(Kind):
    name = "thesis"
    version = __version__
    summary = "Assemble a thesis with the thesis-assemble engine and verify it against its profile."
    request_model = ThesisRequest
    consume_inputs = True       # sources are a student's unpublished text

    def options(self) -> dict:
        return {"profiles": [n for n, p in _profile_index().items()
                             if p.get("engine") == "thesis-assemble"]}

    def validate(self, request: ThesisRequest, resolve_upload):
        prof = _profile_for(request.profile, "thesis-assemble", "manuscript")
        _engine_ready(prof.engine)
        inputs = _resolve_inputs(request, resolve_upload,
                                 ("sources", "figures", "inserted", "bibliography"))
        scratch = tempfile.mkdtemp(prefix="abcprint-check-")
        try:
            prepare_thesis(request, inputs, scratch, assets=False)
        except _Refused as exc:
            raise _invalid(str(exc))
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        return inputs

    def execute(self, ctx: KindContext) -> KindResult:
        req: ThesisRequest = ctx.request
        prof = profiles.load(req.profile)
        options = {"profile": prof.ref, "engine": prof.engine, "copy": req.copy,
                   "checks": req.checks}
        try:
            paths, info = prepare_thesis(req, ctx.inputs, str(ctx.workdir / "ws"), assets=True,
                                         profile_dir=profiles._resolve_dir(req.profile))
        except _Refused as exc:
            raise ExecutorFailure(str(exc), outcome={"options": options})
        options.update(chapters=req.chapters or info["detected_chapters"],
                       identity=info["identity"])
        if info["archive"] is not None:
            options["archive"] = info["archive"]
        try:
            result = compiler.run(paths, req.chapters, copy=req.copy, run_checks=req.checks,
                                  runner=_tool_runner(ctx))
        except Cancelled:
            raise
        except Exception as exc:
            raise ExecutorFailure(str(exc), outcome={
                "options": options, "log_tail": getattr(exc, "log_tail", []) or [],
                "detected_chapters": info["detected_chapters"]})
        outputs = _publish(paths["build"], result["artifacts"], ctx.outdir)
        return KindResult(outputs=outputs, outcome={
            "compliant": result.get("compliant"), "checks": result["checks"],
            "manifest": result["manifest"], "artifacts": result["artifacts"],
            "log_tail": result["log_tail"], "options": options,
            "detected_chapters": info["detected_chapters"]})


class ManuscriptKind(Kind):
    name = "manuscript"
    version = __version__
    summary = "Render a manuscript with the quarto-render engine."
    request_model = ManuscriptRequest
    consume_inputs = True

    def options(self) -> dict:
        return {"profiles": [n for n, p in _profile_index().items()
                             if p.get("engine") == "quarto-render"],
                "refused_quarto_metadata_keys": profiles.REFUSED_METADATA}

    def validate(self, request: ManuscriptRequest, resolve_upload):
        prof = _profile_for(request.profile, "quarto-render", "thesis")
        _engine_ready(prof.engine)
        inputs = _resolve_inputs(request, resolve_upload, ("sources", "figures", "bibliography"))
        scratch = tempfile.mkdtemp(prefix="abcprint-check-")
        try:
            prepare_manuscript(request, inputs, scratch, assets=False)
        except _Refused as exc:
            raise _invalid(str(exc))
        finally:
            shutil.rmtree(scratch, ignore_errors=True)
        return inputs

    def execute(self, ctx: KindContext) -> KindResult:
        req: ManuscriptRequest = ctx.request
        prof = profiles.load(req.profile)
        options = {"profile": prof.ref, "engine": prof.engine, "outputs": req.outputs,
                   "quarto_metadata_keys": sorted(req.quarto_metadata)}
        try:
            paths, info = prepare_manuscript(req, ctx.inputs, str(ctx.workdir / "ws"),
                                             assets=True,
                                             profile_dir=profiles._resolve_dir(req.profile))
        except _Refused as exc:
            raise ExecutorFailure(str(exc), outcome={"options": options})
        options["entry"] = info["entry"]
        if info["archive"] is not None:
            options["archive"] = info["archive"]
        try:
            result = compiler.run_quarto(paths, prof, formats=list(req.outputs),
                                         user_metadata=req.quarto_metadata, entry=info["entry"],
                                         runner=_tool_runner(ctx))
        except Cancelled:
            raise
        except Exception as exc:
            raise ExecutorFailure(str(exc), outcome={
                "options": options, "log_tail": getattr(exc, "log_tail", []) or []})
        outputs = _publish(paths["build"], result["artifacts"], ctx.outdir)
        return KindResult(outputs=outputs, outcome={
            "compliant": result.get("compliant"), "checks": result["checks"],
            "manifest": result["manifest"], "artifacts": result["artifacts"],
            "log_tail": result["log_tail"], "options": options})


class EquivalenceKind(Kind):
    name = "equivalence"
    version = __version__
    summary = "Decide whether two PDFs are the same document, on four planes."
    request_model = EquivalenceRequest
    consume_inputs = True

    def validate(self, request: EquivalenceRequest, resolve_upload):
        return {"a": resolve_upload(request.a), "b": resolve_upload(request.b)}

    def execute(self, ctx: KindContext) -> KindResult:
        req: EquivalenceRequest = ctx.request
        try:
            rep = equivalence.compare(str(ctx.inputs["a"]), str(ctx.inputs["b"]),
                                      dpi=req.dpi, sample=req.sample)
        except Exception as exc:
            raise ExecutorFailure(f"could not read one of the PDFs: {exc}")
        return KindResult(outcome=rep.as_dict())


def _profile_index() -> dict:
    out = {}
    for name in profiles.available():
        try:
            out[name] = profiles.load(name).as_dict()
        except profiles.ProfileError as exc:
            out[name] = {"name": name, "error": str(exc)}
    return out


def make_kinds() -> list[Kind]:
    return [ThesisKind(), ManuscriptKind(), EquivalenceKind()]


def make_kind() -> Kind:
    """The worker's factory. One parameterized job serves all three kinds, so the
    kind comes from the dispatch's own `kind` meta — which the front door sets
    and the job declares meta_required."""
    name = os.environ.get("NOMAD_META_kind", "")
    for k in make_kinds():
        if k.name == name:
            return k
    raise SystemExit(f"NOMAD_META_kind is {name!r}; expected one of {KINDS}")


# ── authentication ──────────────────────────────────────────────────────────
#
# The abcp_ tokens and their scopes are unchanged, so every holder keeps working.
# The kit's identity model has no scopes, so the scope each request needs is
# decided here from the route it is calling.

def _native_scope(request: Request) -> str:
    path, method = request.url.path, request.method.upper()
    if path.startswith("/v1/equivalence/"):
        return "read"           # /equivalence was a read-scope call in 0.14
    if method == "GET":
        return "read"
    if path.endswith("/tickets"):
        return "read"
    return "compile"


def _err(status: int, tag: str, msg: str, headers: dict | None = None, **extra) -> ApiError:
    # `detail` too: 0.14 clients read FastAPI's {"detail": …}, and the envelope
    # allows additional fields.
    return ApiError(status, tag, msg, headers=headers, detail=msg, **extra)


class PrintTokens(Authenticator):
    """abcp_ bearer tokens with scopes, as configured in ABC_PRINT_SVC_TOKEN."""

    def __init__(self, table: dict, disabled: bool, **kw):
        super().__init__(**kw)
        self.table = table
        self.disabled = disabled
        self.mode = "disabled" if disabled else ("token" if table else "no-tokens")

    def presented(self, request: Request) -> bool:
        return _bearer(request) is not None

    def principal(self, request: Request) -> auth.Principal:
        if self.disabled:
            return auth.ANONYMOUS
        if not self.table:
            raise _err(503, "auth_not_configured",
                       f"this deployment has no tokens configured, so it can authorise "
                       f"nobody. Set {auth.ENV_VAR} (see docs/authentication.md).")
        header = request.headers.get("authorization")
        p = auth.verify(auth.bearer(header), self.table)
        if p is None:
            raise _err(401, "unauthenticated",
                       "a bearer token is required: `Authorization: Bearer abcp_…`",
                       headers={"WWW-Authenticate": "Bearer"})
        return p

    @staticmethod
    def require(p: auth.Principal, scope: str) -> None:
        if not p.may(scope):
            raise _err(403, "forbidden", f"this token does not carry the {scope!r} scope "
                                         f"(it has: {', '.join(p.scopes)})")

    async def authenticate(self, request: Request) -> Caller:
        p = self.principal(request)
        self.require(p, _native_scope(request))
        return self.caller(p.label)

    def state(self) -> dict:
        if self.disabled:
            return {"mode": "disabled", "tokens": 0,
                    "warning": f"{auth.DISABLE_VAR}=disabled — every caller is 'anonymous' "
                               f"and shares one job view. Never use this on a reachable deployment."}
        if not self.table:
            return {"mode": "no-tokens", "tokens": 0,
                    "warning": f"{auth.ENV_VAR} is empty, so every request except /health and "
                               f"the docs is refused. Mint one with "
                               f"`python -m abcprint.tokens new <label>`."}
        return {"mode": "token", "tokens": len(self.table)}


# ── configuration ───────────────────────────────────────────────────────────

def _num(env: Mapping[str, str], key: str, default):
    return type(default)(env.get(key, default))


def _store(env: Mapping[str, str], data: Path) -> Store:
    mode = env.get("ABCPRINT_STORE", "sqlite")
    if mode == "sqlite":
        return Store(Path(env.get("ABCPRINT_STORE_PATH", data / "dispatch.db")))
    if mode == "memory":
        return Store()
    raise ValueError(f"ABCPRINT_STORE must be sqlite or memory; got {mode!r}")


def _executor(env: Mapping[str, str], store: Store, storage: LocalStorage, max_running: int):
    retention_s = _num(env, "ABCPRINT_ARTIFACT_TTL_S", 21600.0)
    mode = env.get("ABCPRINT_EXECUTOR", "in-process")
    if mode == "in-process":
        # Only meaningful in the worker image, which has the toolchain: tests and
        # a single-host development run.
        return InProcessExecutor(store, storage, max_running=max_running, retention_s=retention_s)
    if mode == "nomad":
        nomad = NomadClient(env["ABCPRINT_NOMAD_ADDR"], token=_secret(env, "ABCPRINT_NOMAD_TOKEN"),
                            namespace=env.get("ABCPRINT_DISPATCH_NAMESPACE", "svc-print"))
        return NomadExecutor(
            store, storage, nomad, parent_job=env.get("ABCPRINT_DISPATCH_JOB", "abc-print-run"),
            max_running=max_running, retention_s=retention_s,
            dispatch_root=Path(env["ABCPRINT_DISPATCH_ROOT"]),
            static_meta={"jurisdiction": env["ABCPRINT_JURISDICTION"],
                         "project_id": env.get("ABCPRINT_PROJECT_ID", "abc-print-svc"),
                         "trigger_source": "api",
                         "cost_center": env.get("ABCPRINT_COST_CENTER", "mbhg-bioinformatics")},
            poll_s=_num(env, "ABCPRINT_DISPATCH_POLL_S", 2.0),
            placement_timeout_s=_num(env, "ABCPRINT_PLACEMENT_TIMEOUT_S", 600.0))
    raise ValueError(f"ABCPRINT_EXECUTOR must be in-process or nomad; got {mode!r}")


def _secret(env: Mapping[str, str], key: str) -> str | None:
    """A secret from `<KEY>_FILE` (a rendered template) or, failing that, `<KEY>`."""
    path = env.get(f"{key}_FILE")
    if path:
        return Path(path).read_text().strip() or None
    return env.get(key) or None


def _rate_limits(env: Mapping[str, str]) -> RateLimits:
    if env.get("ABCPRINT_RATE_LIMITS", "on").lower() in ("off", "0", "false"):
        return RateLimits.disabled()
    proxies = tuple(p.strip() for p in env.get("ABCPRINT_TRUSTED_PROXIES", "").split(",") if p.strip())
    d = RateLimits()
    return RateLimits(per_ip=d.per_ip, auth_failures=d.auth_failures, caller_read=d.caller_read,
                      caller_write=d.caller_write, trusted_proxies=proxies)


DESCRIPTION = """Document compilation and compliance verification for thesis-style documents.

**The encoded rulebook is the product; the renderer is delivery.**

Each build runs as its own Nomad job, in the worker image that carries the
toolchain, the fonts and the profiles; this front door only validates, queues and
reports. Two ways in, onto the same runs:

* the **native API** — upload with `POST /v1/uploads`, then `POST /v1/thesis/runs`
  or `POST /v1/manuscript/runs`, poll `…/status`, read `outcome` (`compliant`,
  `checks`, `manifest`), fetch `outputs`;
* the **0.14 API** — `POST /compile`, `/compile/archive`, `/compile/manuscript`,
  poll `GET /jobs/{id}`, download `/jobs/{id}/artifacts/{name}`. A job id is a
  run id.

Tokens start `abcp_` and carry scopes: `read`, `compile`, `deliver`.
`succeeded` (0.14) / `complete` (native) does **not** mean compliant: a document
can be produced and still fail its checks, which is what `compliant` is for.

Byte comparison is deliberately not offered: two builds of a 230-page thesis
differ in ~84,000 bytes while being the same document, so `/equivalence` decides
on four planes — page count, text, pixels, stable metadata."""


def build(env: Mapping[str, str] = os.environ):
    try:
        table = auth.load(dict(env))
    except auth.AuthError as exc:      # fatal: a broken token table must not come up healthy
        raise RuntimeError(f"{auth.ENV_VAR} is malformed: {exc}") from None
    authenticator = PrintTokens(table, auth.disabled(dict(env)))

    data = Path(env.get("ABCPRINT_DATA_DIR", "/srv/work/dispatch"))
    storage = LocalStorage(data)
    store = _store(env, data)
    max_running = _num(env, "ABCPRINT_MAX_RENDERS", 2)
    admission = Admission(store, max_running=max_running,
                          max_queued=_num(env, "ABCPRINT_MAX_QUEUED", 8),
                          typical_run_s=_num(env, "ABCPRINT_TYPICAL_RUN_S", 60.0))
    executor = _executor(env, store, storage, max_running)
    sweeper = Sweeper(store, storage, interval_s=_num(env, "ABCPRINT_SWEEP_S", 300.0))
    kinds = make_kinds()
    vendor = env.get("ABCPRINT_VENDOR", "/srv/vendor")
    has_vendor = os.path.isfile(os.path.join(vendor, "swagger-ui-bundle.js"))
    settings = ServiceSettings(
        service_id="org.abc-cluster.print", service_name="abc-print-svc",
        service_version=__version__, description=DESCRIPTION,
        git_commit=env.get("ABCPRINT_GIT_COMMIT", ""), build_time=env.get("ABCPRINT_BUILD_TIME", ""),
        upload_ttl_s=_num(env, "ABCPRINT_UPLOAD_TTL_S", 3600.0),
        max_upload_bytes=_num(env, "ABCPRINT_MAX_UPLOAD_BYTES", 512 * 1024 * 1024),
        # Served from the image: the documentation must work with no egress.
        **({"swagger_js_url": "/vendor/swagger-ui-bundle.js",
            "swagger_css_url": "/vendor/swagger-ui.css",
            "redoc_js_url": "/vendor/redoc.standalone.js"} if has_vendor else {}))

    @asynccontextmanager
    async def lifespan(_app):
        sweeper.start()
        yield
        sweeper.stop()
        executor.shutdown()

    app = create_app(kinds=kinds, authenticator=authenticator, store=store, storage=storage,
                     admission=admission, executor=executor, settings=settings, lifespan=lifespan,
                     rate_limiter=RateLimiter(_rate_limits(env)))
    origins = [o.strip() for o in env.get("ABCPRINT_CORS_ORIGINS", "*").split(",") if o.strip()]
    app.add_middleware(CORSMiddleware, allow_origins=origins, allow_credentials=False,
                       allow_methods=["GET", "POST", "DELETE", "OPTIONS"], allow_headers=["*"],
                       expose_headers=["Content-Disposition", "Location", "Retry-After"])
    if has_vendor:
        app.mount("/vendor", StaticFiles(directory=vendor), name="vendor")

    from .legacy import mount_legacy
    mount_legacy(app, store=store, storage=storage, admission=admission, executor=executor,
                 kinds={k.name: k for k in kinds}, authenticator=authenticator,
                 settings=settings, cors_origins=origins, env=env)
    app.state.store, app.state.executor = store, executor
    return app
