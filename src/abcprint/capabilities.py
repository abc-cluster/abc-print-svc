"""What the worker image can do, recorded once, when it is built.

The service is two images. The WORKER carries quarto, typst, the fonts, the
profiles and the thesis pipeline, and is what a dispatched run executes in. The
FRONT DOOR carries none of that: it validates, admits and queues requests, and
dispatches each one as a Nomad job that runs the worker. Keeping the compilation
assets out of the front door is the point — on 2026-11-08 private render logs
shipped inside the public API image because the profile was vendored into it.

But the front door still has to answer questions about the worker: which profiles
exist and which engine each selects, whether the thesis pipeline is present,
which toolchain will render, how each font family resolves. Those are properties
of the worker IMAGE, fixed when it is built, so they are measured there:

    python3 -m abcprint.capabilities > /srv/capabilities.json

and copied into the front-door image from the same build. The two images are
built together from one Containerfile and tagged together, so the record and the
worker it describes cannot drift.

Fonts are the one caveat: an operator may mount licensed originals into the
worker at run time, and those win. The record says what the image resolves on its
own; each run's manifest says what actually rendered, and that is authoritative.
"""
from __future__ import annotations

import json
import os
import sys

from . import fonts, profile as profiles, toolchain

# The families the SU/FMHS template names. Kept here, not in the API module, so
# the worker can measure them without importing the web stack.
REQUIRED_FONTS = ["Calibri", "Cambria", "Georgia", "Times New Roman", "Arial", "Helvetica Neue"]

ENV_VAR = "ABCPRINT_CAPABILITIES"


def _font_paths() -> list[str] | None:
    return [p for p in os.environ.get("ABCPRINT_FONT_PATHS", "").split(os.pathsep) if p] or None


def engines_available() -> dict:
    pipeline = os.environ.get("ABCPRINT_PIPELINE", "/srv/pipeline")
    return {"quarto-render": True,
            "thesis-assemble": os.path.isfile(os.path.join(pipeline, "thesis-assemble.sh"))}


def measure() -> dict:
    """Measure the image this runs in. Only meaningful inside the worker image."""
    out_profiles: dict = {}
    families = set(REQUIRED_FONTS)
    for name in profiles.available():
        try:
            prof = profiles.load(name)
        except profiles.ProfileError as exc:
            out_profiles[name] = {"name": name, "error": str(exc)}
            continue
        out_profiles[name] = prof.as_dict()
        for role in (prof.fonts or {}).values():
            # Profiles name a role's font three ways: a family, a list of
            # families, or a mapping with a `stack`.
            if isinstance(role, str):
                stack = [role]
            elif isinstance(role, list):
                stack = role
            else:
                stack = (role or {}).get("stack") or []
            families.update(f for f in stack if isinstance(f, str))
    tc = toolchain.detect()
    return {
        "schema": 1,
        "profiles": out_profiles,
        "refused_quarto_metadata_keys": profiles.REFUSED_METADATA,
        "engines_available": engines_available(),
        "toolchain": {**tc.as_dict(), "typst_mismatch": tc.typst_mismatch,
                      "problems": tc.problems()},
        "pipeline": toolchain.pipeline_provenance(),
        "fonts": {"required": REQUIRED_FONTS,
                  "health": fonts.health(sorted(families), _font_paths())},
    }


_CACHE: dict | None = None


def load() -> dict | None:
    """The record, in the front door. None when it is absent — the front door
    then reports itself unable to build rather than guessing."""
    global _CACHE
    if _CACHE is None:
        path = os.environ.get(ENV_VAR, "")
        if not path or not os.path.isfile(path):
            return None
        with open(path) as fh:
            _CACHE = json.load(fh)
    return _CACHE


def font_health(families: list[str]) -> dict:
    """fonts.health() over the record: the same shape, for the families measured.

    A family that was not measured when the worker was built is reported as such
    rather than guessed at.
    """
    rec = load() or {}
    measured = (rec.get("fonts") or {}).get("health") or {}
    by_family = {r.get("requested"): r for r in measured.get("resolutions", [])}
    resolutions, missing, unmeasured = [], [], []
    for fam in families:
        r = by_family.get(fam)
        if r is None:
            unmeasured.append(fam)
            continue
        resolutions.append(r)
        if fam in (measured.get("missing") or []):
            missing.append(fam)
    out = {**{k: v for k, v in measured.items() if k not in ("resolutions", "missing", "ok")},
           "resolutions": resolutions, "missing": missing,
           "ok": not missing and not unmeasured,
           "source": "measured in the worker image at build time; a run's manifest "
                     "records what actually rendered"}
    if "substituted" in measured:
        # "Requested->Resolved" strings, as fonts.health() writes them.
        out["substituted"] = [s for s in measured["substituted"]
                              if s.split("->", 1)[0] in families]
    if unmeasured:
        out["unmeasured"] = unmeasured
    return out


if __name__ == "__main__":
    json.dump(measure(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
