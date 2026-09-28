"""Font resolution, substitution policy, and service health.

Why this module exists at all: the faculty length rule is measured in PAGES, so
a font substitution is a compliance event, not a cosmetic one. A 61-page chapter
that silently becomes 58 pages on the server is worse than a build that refuses
to run.

The template names six proprietary families — Calibri, Cambria, Georgia, Times
New Roman, Arial, Helvetica Neue. None may be redistributed in an image, so the
image ships METRIC-COMPATIBLE open substitutes and the licensed originals are
mounted when the operator has a licence that covers server rendering.

"Metric-compatible" means advance widths match, so line breaks normally hold.
It does not mean the rendering is identical: hinting, kerning pairs and
hyphenation can still differ. Whether pagination actually survives a given
substitution is a measurable question, answered by rendering both ways and
comparing with abcprint.equivalence — never assumed.
"""
from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, asdict

# Open, redistributable stand-ins. The first five pairs are designed to be
# metric-compatible with their proprietary counterpart; Helvetica Neue has no
# metric-compatible libre equivalent, so it is an approximation and is marked
# as such rather than quietly treated like the others.
SUBSTITUTES: dict[str, tuple[str, bool]] = {
    # requested          (shipped substitute, metric_compatible)
    "Calibri":           ("Carlito", True),
    "Cambria":           ("Caladea", True),
    "Times New Roman":   ("Tinos", True),
    "Arial":             ("Arimo", True),
    "Georgia":           ("Gelasio", True),
    "Helvetica Neue":    ("Arimo", False),
}

# Families that are themselves open and ship as the real thing, not a stand-in.
SHIPPED_FREE = {"JetBrains Mono", "JetBrainsMono", "Monaspace Argon", "DejaVu Sans"}


@dataclass
class Resolution:
    requested: str
    resolved: str
    substituted: bool
    metric_compatible: bool
    available: bool

    def as_dict(self) -> dict:
        return asdict(self)


def installed_families(font_paths: list[str] | None = None) -> set[str]:
    """Families the renderer can actually see.

    Asks Quarto's own typst, because that is the binary that will render. A
    family present on the host but invisible to typst is not available.
    """
    env = dict(os.environ)
    if font_paths:
        env["TYPST_FONT_PATHS"] = os.pathsep.join(font_paths)
    try:
        p = subprocess.run(["quarto", "typst", "fonts"],
                           capture_output=True, text=True, timeout=60, env=env)
    except (OSError, subprocess.SubprocessError):
        return set()
    # `quarto typst fonts` writes the family list to STDERR, not stdout. Reading
    # only stdout yields an empty set, which would make the service report no
    # fonts at all and fail every build's health check.
    return {ln.strip() for ln in ((p.stdout or "") + "\n" + (p.stderr or "")).splitlines()
            if ln.strip()}


def resolve(requested: list[str], font_paths: list[str] | None = None) -> list[Resolution]:
    have = installed_families(font_paths)
    out: list[Resolution] = []
    for fam in requested:
        if fam in have:
            out.append(Resolution(fam, fam, False, True, True))
            continue
        sub, metric = SUBSTITUTES.get(fam, (None, False))
        if sub and sub in have:
            out.append(Resolution(fam, sub, True, metric, True))
        else:
            out.append(Resolution(fam, sub or "", bool(sub), metric, False))
    return out


def enforce(resolutions: list[Resolution], on_substitution: str = "fail") -> list[str]:
    """Apply the build's substitution policy.

    Returns fatal messages. `on_substitution` is "fail" by default because a
    length rule measured in pages cannot tolerate a silent substitution; "warn"
    is for drafts, where pagination does not yet matter.
    """
    fatal: list[str] = []
    for r in resolutions:
        if not r.available:
            fatal.append(
                f"font {r.requested!r} is unavailable and no substitute is installed"
            )
        elif r.substituted and on_substitution == "fail":
            how = "metric-compatible" if r.metric_compatible else "NOT metric-compatible"
            fatal.append(
                f"font {r.requested!r} resolved to {r.resolved!r} ({how}); "
                f"pagination may differ. Mount the licensed font, or set "
                f"on_substitution='warn' to accept this."
            )
    return fatal


def health(required: list[str], font_paths: list[str] | None = None) -> dict:
    """Service-level font health.

    A missing font directory must fail the SERVICE's health check rather than a
    student's build, so this is reported at start-up and on /health, separately
    from any one job.
    """
    res = resolve(required, font_paths)
    missing = [r.requested for r in res if not r.available]
    subbed = [f"{r.requested}->{r.resolved}" for r in res if r.substituted and r.available]
    return {
        "ok": not missing,
        "font_paths": font_paths or [],
        "missing": missing,
        "substituted": subbed,
        "resolutions": [r.as_dict() for r in res],
    }
