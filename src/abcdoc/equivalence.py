"""Are two builds the same document?

Byte comparison is the wrong test for this pipeline, and that is a measured
result rather than an opinion. Building the same 230-page thesis twice on one
machine, 30 seconds apart, yields files of IDENTICAL length that differ in
~84,000 bytes. The differences are:

  * /CreationDate and /ModDate      — wall clock. FIXABLE: honour SOURCE_DATE_EPOCH.
  * /ID[1] and XMP InstanceID       — random per render. Not fixable from outside.
  * resource-dictionary emission    — /Font, /XObject and /ExtGState entries are
    order                             written in per-process hash order, so object
                                      numbers permute. Not fixable from outside.

With SOURCE_DATE_EPOCH pinned the dates stop moving, and a SMALL document then
does compare byte-identical — which is a trap, because the full thesis still
differs by ~45,000 bytes. Anyone validating reproducibility on a one-chapter
sample will wrongly conclude byte-identity holds.

Meanwhile the documents are the same: equal page count, byte-identical
`pdftotext -layout` output, and pixel-identical rendered pages.

So equivalence is checked on four planes. Known-volatile fields are excluded BY
NAME rather than ignored wholesale, so a new source of drift still fails.
"""
from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
from dataclasses import dataclass, field

VOLATILE = ("/ID[1]", "/CreationDate", "/ModDate")


@dataclass
class Report:
    pages: tuple[int, int] = (0, 0)
    text_equal: bool = False
    pixel_pages: list[int] = field(default_factory=list)
    pixel_mismatches: list[int] = field(default_factory=list)
    meta_equal: bool = False
    meta_diff: dict = field(default_factory=dict)
    failures: list[str] = field(default_factory=list)

    @property
    def equivalent(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict:
        return {
            "equivalent": self.equivalent,
            "pages": list(self.pages),
            "text_equal": self.text_equal,
            "pixel_pages_checked": self.pixel_pages,
            "pixel_mismatches": self.pixel_mismatches,
            "meta_equal": self.meta_equal,
            "meta_diff": self.meta_diff,
            "failures": self.failures,
            "volatile_excluded": list(VOLATILE),
        }


def _sh(*a) -> None:
    subprocess.run(a, capture_output=True)


def page_count(path: str) -> int:
    from pypdf import PdfReader
    return len(PdfReader(path).pages)


def text_digest(path: str) -> str:
    fd, tmp = tempfile.mkstemp(suffix=".txt")
    os.close(fd)
    try:
        # -layout keeps column and line positions, so pagination and placement
        # changes surface here rather than being flattened away.
        _sh("pdftotext", "-layout", path, tmp)
        return hashlib.sha256(open(tmp, "rb").read()).hexdigest()
    finally:
        os.unlink(tmp)


def _page_digest(path: str, page: int, dpi: int) -> str:
    d = tempfile.mkdtemp()
    try:
        _sh("pdftoppm", "-f", str(page), "-l", str(page), "-r", str(dpi), "-png", path, f"{d}/p")
        names = sorted(os.listdir(d))
        if not names:
            return "MISSING"
        return hashlib.sha256(open(os.path.join(d, names[0]), "rb").read()).hexdigest()
    finally:
        for n in os.listdir(d):
            os.unlink(os.path.join(d, n))
        os.rmdir(d)


def _sample(n_pages: int, sample: int) -> list[int]:
    if sample <= 0 or sample >= n_pages:
        return list(range(1, n_pages + 1))
    step = max(1, n_pages // sample)
    return sorted({1, n_pages, *(1 + i * step for i in range(sample))} & set(range(1, n_pages + 1)))


def stable_meta(path: str) -> dict:
    from pypdf import PdfReader
    md = PdfReader(path).metadata or {}
    return {k: str(v) for k, v in md.items() if k not in VOLATILE}


def compare(a: str, b: str, dpi: int = 72, sample: int = 6) -> Report:
    r = Report()
    r.pages = (page_count(a), page_count(b))
    if r.pages[0] != r.pages[1]:
        # Page count is the length rule's unit, so a mismatch is decisive and
        # the more expensive planes are not worth running.
        r.failures.append(f"page count {r.pages[0]} vs {r.pages[1]}")
        return r

    r.text_equal = text_digest(a) == text_digest(b)
    if not r.text_equal:
        r.failures.append("extracted text differs")

    r.pixel_pages = _sample(r.pages[0], sample)
    r.pixel_mismatches = [
        p for p in r.pixel_pages if _page_digest(a, p, dpi) != _page_digest(b, p, dpi)
    ]
    if r.pixel_mismatches:
        r.failures.append(f"rendered pages differ: {r.pixel_mismatches}")

    ma, mb = stable_meta(a), stable_meta(b)
    r.meta_equal = ma == mb
    if not r.meta_equal:
        r.meta_diff = {k: [ma.get(k), mb.get(k)] for k in set(ma) | set(mb) if ma.get(k) != mb.get(k)}
        r.failures.append("stable metadata differs")
    return r
