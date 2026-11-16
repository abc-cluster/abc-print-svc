"""What this deployment can build with: health problems, toolchain and fonts.

All three answer from the worker's capability record (the front door has no
toolchain of its own). `/health` is open and says only whether the deployment
can build; the detail — versions, font resolution, the vendored pipeline — is
read-scoped at `/svc/v1/toolchain` and `/svc/v1/fonts` (and, until the v0 sunset, at
`/v0/toolchain` and `/v0/fonts`).
"""
from __future__ import annotations

from typing import Optional

from fastapi import Depends, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from abc_dispatch import ApiError

from fastapi.responses import Response

from . import auth, capabilities, profile as profiles, profile_store, shape
from .models import FontHealth, Toolchain

_bearer = HTTPBearer(
    scheme_name="abc-print-svc token", bearerFormat="abcp_…", auto_error=False,
    description=("A token minted with `python -m abcprint.tokens new <label>`. Scopes decide "
                 "what it opens: `compile`, `read`, `deliver`."))

AUTH_RESPONSES: dict = {
    401: {"description": "No token, or one this deployment does not recognise. Fix the token."},
    403: {"description": "The token is valid but lacks the scope this call needs."},
    503: {"description": "This deployment has no tokens configured, so it can authorise nobody."},
}


def scope_guard(app, authenticator):
    """`require(scope)` → a dependency that authenticates the bearer token, checks
    its scope and spends the caller's rate limit — the same gate for every route
    this service adds beside the kit's own."""
    limiter = app.state.rate_limiter

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
    return require


def health_problems() -> tuple[list[str], dict]:
    """Everything that would break a build, and which engines the worker has."""
    rec = capabilities.load()
    if rec is None:
        return ([f"no worker capability record ({capabilities.ENV_VAR}); this front door "
                 f"cannot tell what its worker can build"], {})
    tc = rec["toolchain"]
    fh = capabilities.font_health(rec["fonts"]["required"])
    problems = list(tc.get("problems") or []) + \
        ([] if fh["ok"] else [f"fonts unavailable: {fh['missing']}"])
    return problems, rec["engines_available"]


def toolchain() -> dict:
    rec = capabilities.load() or {}
    tc = rec.get("toolchain") or {}
    return {**{k: v for k, v in tc.items() if k != "problems"}, "pipeline": rec.get("pipeline")}


def fonts(family: list[str] | None) -> dict:
    rec = capabilities.load() or {}
    return capabilities.font_health(family or (rec.get("fonts") or {}).get("required")
                                    or capabilities.REQUIRED_FONTS)


def mount_v1_info(app, require) -> None:
    @app.get("/svc/v1/toolchain", tags=["discovery"], response_model=Toolchain,
             summary="What renders here: versions, and the pipeline revision",
             responses=AUTH_RESPONSES)
    def get_toolchain(p: auth.Principal = Depends(require("read"))):
        """The worker's toolchain as measured when its image was built: quarto, the
        typst it bundles (which decides pagination), pandoc, poppler, qpdf, pypdf,
        and the vendored thesis pipeline's revision. A run's manifest records what
        actually rendered that run."""
        return toolchain()

    @app.get("/svc/v1/fonts", tags=["discovery"], response_model=FontHealth,
             summary="Which family will actually render", responses=AUTH_RESPONSES)
    def get_fonts(family: list[str] | None = Query(
            default=None, description="Families to resolve. Defaults to the profiles' required set."),
            p: auth.Principal = Depends(require("read"))):
        """How each requested family resolves in the worker image — available,
        substituted (e.g. Calibri → Carlito) and whether metric-compatible. A
        family not measured at image build is listed in `unmeasured`."""
        return fonts(family)

    @app.get("/svc/v1/profiles", tags=["discovery"],
             summary="Every profile, its versions and the default an unpinned request gets",
             responses=AUTH_RESPONSES)
    def list_profiles(p: auth.Principal = Depends(require("read"))):
        """Published profiles (from the profile store) and those baked into the
        worker image. Name a version as `name@version` to pin it; a bare `name`
        gets `default`, which moves only when the operator promotes a version.
        Every run records the version and content digest it was built with."""
        return profiles_listing()

    @app.get("/svc/v1/profiles/{ref}/shape", tags=["discovery"],
             summary="What a zip for this profile should contain", responses=AUTH_RESPONSES)
    def profile_shape(ref: str, p: auth.Principal = Depends(require("read"))):
        """Ask before you zip. The layouts this profile accepts, its exact rules —
        the ones the front door enforces, so a bundle that follows them is not
        refused — and an annotated example tree (`path`, `role`, `pattern`,
        `required`). `ref` is `name` (the default version) or `name@version`."""
        return shape.shape(_profile(ref))

    @app.get("/svc/v1/profiles/{ref}/skeleton.zip", tags=["discovery"],
             summary="A starter zip in this profile's shape",
             responses={200: {"content": {"application/zip": {}}}, **AUTH_RESPONSES})
    def profile_skeleton(ref: str, p: auth.Principal = Depends(require("read"))):
        """The example tree as placeholder files that pass this profile's rules.
        Replace the contents, keep the shape."""
        prof = _profile(ref)
        return Response(shape.skeleton_zip(prof), media_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="{prof.name}-{prof.version}-skeleton.zip"'})


def _profile(ref: str):
    try:
        return profiles.load(ref)
    except profiles.ProfileError as exc:
        raise ApiError(404, "not_found", str(exc)) from None


def profiles_listing() -> dict:
    st = profile_store.default()
    out: dict[str, dict] = {}
    if st is not None:
        for name, e in st.index()["profiles"].items():
            out[name] = {"name": name, "default": e.get("default"), "source": st.url,
                         "versions": [{"version": v, "digest": r["digest"], "engine": r["engine"],
                                       "published_at": r.get("published_at"),
                                       "bundle": (r.get("descriptor") or {}).get("bundle") or {}}
                                      for v, r in sorted(e["versions"].items())]}
    for name in profiles._baked_names():
        if name in out:
            continue
        try:
            prof = profiles.load(name)
        except profiles.ProfileError as exc:
            out[name] = {"name": name, "error": str(exc)}
            continue
        out[name] = {"name": name, "default": prof.version, "source": "baked",
                     "versions": [{"version": prof.version, "digest": prof.digest,
                                   "engine": prof.engine, "published_at": None,
                                   "bundle": prof.bundle}]}
    return {"store": st.url if st is not None else None,
            "profiles": [out[k] for k in sorted(out)]}
