"""Run the pipeline over a materialised workspace.

SOURCE_DATE_EPOCH and TZ are pinned on every build. That does not make output
byte-reproducible — resource-dictionary emission order still permutes, see
abcprint.equivalence — but it removes the one source of drift that IS removable,
so two builds of one bundle differ only in ways that carry no document meaning.

Code execution is off. Quarto will run knitr/jupyter chunks given the chance, and
arbitrary markdown from a Logseq plugin is arbitrary input.
"""
from __future__ import annotations

import os
import subprocess
import time

from . import fonts, profile as profiles, toolchain
from .engines import quarto_render

# Fixed epoch so a rebuild of the same bundle carries the same timestamps. Any
# constant does; this one is arbitrary and stable.
EPOCH = os.environ.get("ABCPRINT_SOURCE_DATE_EPOCH", "1700000000")
BUILD_TIMEOUT_S = int(os.environ.get("ABCPRINT_BUILD_TIMEOUT", "1800"))


class CompileError(RuntimeError):
    pass


def _env(paths: dict) -> dict:
    env = dict(os.environ)
    env.update({
        "SOURCE_DATE_EPOCH": EPOCH,
        "TZ": "UTC",
        "THESIS_ASSEMBLE_OUT": paths["build"],
        # Quarto executes code chunks by default. A service accepting documents
        # from anyone must not.
        "QUARTO_DENO_EXTRA_OPTIONS": "--no-remote",
        "HOME": paths["repo"],
    })
    return env


def run_quarto(paths: dict, prof: profiles.Profile, *, formats: list[str],
               user_metadata: dict | None = None, entry: str = "index.qmd") -> dict:
    """The quarto-render engine, wrapped to the same result shape as the thesis one.

    `compliant` is None rather than True when the profile defines no compliance
    rules: a profile with nothing to check must not claim compliance.
    """
    res = quarto_render.run(paths, prof, formats=formats,
                            user_metadata=user_metadata, entry=entry)
    tc = toolchain.detect()
    fh = fonts.health(
        [ (((prof.fonts or {}).get(r) or {}).get("stack") or [None])[0]
          for r in ("body", "sans", "mono") ],
        [p for p in os.environ.get("ABCPRINT_FONT_PATHS", "").split(os.pathsep) if p] or None)
    return {
        "artifacts": res["artifacts"],
        "compliant": True if prof.has_compliance_rules else None,
        "return_code": 0,
        "checks": {"passed": 0, "failed": 0, "warnings": 0, "failures": [],
                   "warning_detail": [],
                   "all_clear": None,
                   "note": "this profile defines no compliance rules"}
        if not prof.has_compliance_rules else {},
        "manifest": {
            "toolchain": tc.as_dict(), "typst_mismatch": tc.typst_mismatch,
            "profile": prof.as_dict(),
            "template": prof.template,
            "effective_quarto_metadata": res["effective_metadata"],
            "fonts": {"resolutions": fh["resolutions"], "substituted": fh["substituted"]},
            "source_date_epoch": EPOCH,
            "outputs": formats,
            "build_seconds": res["build_seconds"],
        },
        "log_tail": res["log_tail"],
    }


def run(paths: dict, chapters: list[str] | None, copy: str = "examination",
        run_checks: bool = True) -> dict:
    """Invoke thesis-assemble.sh and return artefacts, checks and a manifest."""
    # Three copies of one profile, differing only in front matter. The examination copy
    # carries neither the Afrikaans Opsomming nor the branded title frame; the submission
    # copy adds the Opsomming; the branded frame belongs to the library deposit alone.
    # The pipeline withholds the Opsomming while its source section is still a stub, so
    # asking for a submission or library copy cannot put a FIXME on a front-matter page.
    #
    # Validated before the pipeline is looked for, so a caller who mistypes the copy is
    # told that rather than that the image has no pipeline vendored into it.
    flag = {"examination": None, "submission": "--submission", "library": "--library"}
    if copy not in flag:
        raise CompileError(
            f"unknown copy {copy!r}; expected 'examination', 'submission' or 'library'")

    script = os.path.join(paths["bin"], "thesis-assemble.sh")
    if not os.path.isfile(script):
        raise CompileError(f"pipeline script missing: {script}")

    cmd = ["bash", script]
    if flag[copy]:
        cmd.append(flag[copy])
    if not run_checks:
        cmd.append("--no-check")
    if chapters:
        cmd.extend(chapters)

    started = time.time()
    try:
        proc = subprocess.run(cmd, cwd=paths["repo"], env=_env(paths),
                              capture_output=True, text=True, timeout=BUILD_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        raise CompileError(f"build exceeded {BUILD_TIMEOUT_S}s and was killed")

    log = (proc.stdout or "") + (proc.stderr or "")
    lines = log.strip().splitlines()

    # A non-zero exit means one of two very different things and they must not be
    # conflated: the RENDER failed and there is no document, or the document was
    # produced and a VERIFICATION CHECK failed. The pipeline says so itself —
    # "the pdf was still written" — and discarding the artefact in the second case
    # throws away both the document and the evidence of what is wrong with it.
    # Which happened is decided by whether artefacts exist, below.
    artifacts = []
    for name in sorted(os.listdir(paths["build"])):
        fp = os.path.join(paths["build"], name)
        if os.path.isfile(fp) and name.lower().endswith((".pdf", ".docx")):
            artifacts.append({"name": name, "bytes": os.path.getsize(fp),
                              "media_type": "application/pdf" if name.endswith(".pdf")
                              else "application/vnd.openxmlformats-officedocument.wordprocessingml.document"})
    if not artifacts:
        err = CompileError(
            f"render failed (rc={proc.returncode}) and produced no PDF or DOCX; last lines:\n"
            + "\n".join(lines[-25:]))
        err.log_tail = lines[-60:]
        raise err

    tc = toolchain.detect()
    fh = fonts.health(
        ["Calibri", "Cambria", "Georgia", "Times New Roman", "Arial", "Helvetica Neue"],
        [p for p in os.environ.get("ABCPRINT_FONT_PATHS", "").split(os.pathsep) if p] or None)

    checks = _parse_checks(log)
    return {
        "artifacts": artifacts,
        # The document exists; whether it is COMPLIANT is a separate answer that
        # travels with it rather than being collapsed into job success.
        "compliant": checks["all_clear"],
        "return_code": proc.returncode,
        "checks": checks,
        "manifest": {
            # Everything that decided this output, returned WITH it, so a rebuild
            # is a meaningful claim rather than a hope.
            "toolchain": tc.as_dict(),
            "typst_mismatch": tc.typst_mismatch,
            "fonts": {"resolutions": fh["resolutions"], "substituted": fh["substituted"]},
            "source_date_epoch": EPOCH,
            "copy": copy,
            "chapters": chapters or "all",
            "build_seconds": round(time.time() - started, 1),
        },
        "log_tail": log.strip().splitlines()[-40:],
    }


def _parse_checks(log: str) -> dict:
    """Summarise the pipeline's own check output.

    Verification is the product, so the check report travels with the artefact
    rather than being left in a build log nobody reads.
    """
    passed = failed = warned = 0
    failures, warnings = [], []
    for raw in log.splitlines():
        ln = raw.replace("\x1b[32m", "").replace("\x1b[31m", "") \
                .replace("\x1b[33m", "").replace("\x1b[0m", "").strip()
        if ln.startswith("PASS"):
            passed += 1
        elif ln.startswith("WARN"):
            warned += 1
            warnings.append(ln[4:].strip())
        elif ln.startswith("FAIL"):
            failed += 1
            failures.append(ln[4:].strip())
    return {"passed": passed, "failed": failed, "warnings": warned,
            "failures": failures, "warning_detail": warnings,
            "all_clear": failed == 0 and passed > 0}
