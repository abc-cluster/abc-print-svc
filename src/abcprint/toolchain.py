"""Toolchain identity — what actually rendered a document.

A manifest is only worth returning if it names the binaries that decided the
output. Two findings from measuring the real pipeline shape this module:

1. Quarto ships its own typst and uses it. On the reference machine
   `typst --version` on PATH reports 0.13.1 while `quarto typst --version`
   reports 0.13.0, and the PDF's /Creator records 0.13.0. Pagination is decided
   by the bundled one. Pinning a standalone typst therefore pins a binary that
   nothing reads, so `typst_bundled` is the field that matters and
   `typst_path` is recorded only to make a mismatch visible.

2. pypdf must be probed by CAPABILITY, not by version string or import. The
   merge needs PdfWriter(clone_from=...), which arrived in 3.9. An older pypdf
   imports cleanly and dies mid-merge, which is how a build shipped with 211 of
   922 internal links dangling.
"""
from __future__ import annotations

import inspect
import os
import re
import shutil
import subprocess
from dataclasses import dataclass, asdict, field


def _run(*args: str) -> str:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return ""
    return (p.stdout or p.stderr or "").strip()


def _first_version(text: str) -> str:
    m = re.search(r"\d+(?:\.\d+)+", text or "")
    return m.group(0) if m else ""


@dataclass
class Toolchain:
    quarto: str = ""
    typst_bundled: str = ""   # the one Quarto renders with — authoritative
    typst_path: str = ""      # the one on PATH — informational only
    pandoc: str = ""
    qpdf: str = ""
    poppler: str = ""
    python: str = ""
    pypdf: str = ""
    pypdf_can_merge: bool = False

    def as_dict(self) -> dict:
        return asdict(self)

    @property
    def typst_mismatch(self) -> bool:
        """True when a standalone typst shadows the bundled one at a different
        version. Not an error — the bundled one still wins — but it misleads
        anyone debugging pagination, so the manifest says so."""
        return bool(self.typst_path and self.typst_bundled
                    and self.typst_path != self.typst_bundled)

    def problems(self) -> list[str]:
        out = []
        for name in ("quarto", "pandoc", "qpdf", "python"):
            if not getattr(self, name):
                out.append(f"{name} not found")
        if not self.typst_bundled:
            out.append("quarto has no bundled typst — the PDF path will not work")
        if not self.poppler:
            out.append("poppler (pdftotext/pdfinfo) not found — checks cannot run")
        if not self.pypdf_can_merge:
            out.append(
                "pypdf missing or too old: the merge needs PdfWriter(clone_from=...) "
                "(pypdf >= 3.9). The qpdf fallback rebuilds the document without its "
                "name tree and silently dangles every internal link."
            )
        return out


def pipeline_provenance() -> dict:
    """Which revision of the vendored pipeline and profile is in this image.

    The pipeline lives in another repository and changes independently. When a
    check broadens, two documents built a week apart are verified against
    different rules, and a manifest naming only tool versions cannot show that.
    Absent file means an image built without the thesis engine.
    """
    import json
    path = os.path.join(os.environ.get("ABCPRINT_PIPELINE", "/srv/pipeline"),
                        "PROVENANCE.json")
    try:
        with open(path) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return {"revision": "not-vendored",
                "note": "this image was built without the thesis pipeline"}


def detect() -> Toolchain:
    tc = Toolchain()
    tc.quarto = _first_version(_run("quarto", "--version"))
    tc.typst_bundled = _first_version(_run("quarto", "typst", "--version"))
    if shutil.which("typst"):
        tc.typst_path = _first_version(_run("typst", "--version"))
    tc.pandoc = _first_version(_run("pandoc", "--version"))
    tc.qpdf = _first_version(_run("qpdf", "--version"))
    # poppler has no --version on stdout for pdftotext on every build; -v goes to stderr
    tc.poppler = _first_version(_run("pdftotext", "-v"))
    import sys
    tc.python = ".".join(str(x) for x in sys.version_info[:3])
    try:
        import pypdf
        from pypdf import PdfWriter
        tc.pypdf = getattr(pypdf, "__version__", "")
        tc.pypdf_can_merge = "clone_from" in inspect.signature(PdfWriter).parameters
    except Exception:
        tc.pypdf, tc.pypdf_can_merge = "", False
    return tc
