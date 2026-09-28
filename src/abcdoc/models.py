"""Response models.

These exist to make the generated OpenAPI document worth reading: ReDoc renders
field descriptions and examples, so a person doing manual testing can see what a
field MEANS rather than guessing from its name. Examples are taken from real
runs against the 230-page reference thesis, not invented.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class Toolchain(BaseModel):
    quarto: str = Field("", description="Quarto version. Pinning Quarto is what pins typst.")
    typst_bundled: str = Field(
        "", description="The typst Quarto renders with. AUTHORITATIVE for pagination.")
    typst_path: str = Field(
        "", description="A standalone typst on PATH, if any. Informational only — nothing reads it.")
    pandoc: str = Field("", description="Used for DOCX, citeproc and markdown slicing.")
    qpdf: str = Field("", description="Page surgery and the merge fallback.")
    poppler: str = Field("", description="pdftotext/pdfinfo/pdftoppm; backs every check.")
    python: str = ""
    pypdf: str = Field("", description="Version string only; capability is what matters.")
    pypdf_can_merge: bool = Field(
        False,
        description=("True when PdfWriter accepts clone_from (pypdf >= 3.9). If false, the merge "
                     "falls back to qpdf, which rebuilds the document without its name tree and "
                     "silently dangles every internal link."))
    typst_mismatch: bool = Field(
        False,
        description=("True when a standalone typst shadows the bundled one at a different version. "
                     "Not an error — the bundled one still renders — but it misleads anyone "
                     "debugging pagination."))

    model_config = {"json_schema_extra": {"example": {
        "quarto": "1.7.31", "typst_bundled": "0.13.0", "typst_path": "",
        "pandoc": "3.7.0.1", "qpdf": "11.3.0", "poppler": "22.12.0",
        "python": "3.11.2", "pypdf": "6.19.0", "pypdf_can_merge": True,
        "typst_mismatch": False}}}


class FontResolution(BaseModel):
    requested: str = Field(..., description="Family the profile asked for.")
    resolved: str = Field("", description="Family that will actually render.")
    substituted: bool = Field(False, description="True when resolved != requested.")
    metric_compatible: bool = Field(
        False,
        description=("True when the substitute is designed to match advance widths, so line breaks "
                     "normally hold. It does NOT guarantee identical rendering: hinting, kerning "
                     "and hyphenation can still differ."))
    available: bool = Field(False, description="False when neither the family nor a substitute is installed.")


class FontHealth(BaseModel):
    ok: bool = Field(..., description="False when any required family is unavailable.")
    font_paths: list[str] = Field(default_factory=list, description="Directories searched, in precedence order.")
    missing: list[str] = Field(default_factory=list)
    substituted: list[str] = Field(default_factory=list, description="requested->resolved pairs.")
    resolutions: list[FontResolution] = Field(default_factory=list)

    model_config = {"json_schema_extra": {"example": {
        "ok": True,
        "font_paths": ["/usr/local/share/fonts/licensed", "/usr/local/share/fonts/ofl"],
        "missing": [], "substituted": ["Calibri->Carlito", "Cambria->Caladea"],
        "resolutions": [{"requested": "Calibri", "resolved": "Carlito", "substituted": True,
                         "metric_compatible": True, "available": True}]}}}


class Health(BaseModel):
    ok: bool = Field(..., description="False when anything would break a build. Returns HTTP 503.")
    problems: list[str] = Field(default_factory=list, description="Human-readable, each naming the rule broken.")
    toolchain: Toolchain
    typst_mismatch: bool = False
    fonts: FontHealth


class Equivalence(BaseModel):
    equivalent: bool = Field(..., description="True when the two builds are the same document.")
    pages: list[int] = Field(..., description="Page count of each input. The length rule's unit.")
    text_equal: bool = Field(..., description="`pdftotext -layout` digests match; carries pagination and position.")
    pixel_pages_checked: list[int] = Field(default_factory=list)
    pixel_mismatches: list[int] = Field(
        default_factory=list,
        description="Pages whose rendering differs. Catches glyph/layout drift text cannot see.")
    meta_equal: bool = True
    meta_diff: dict = Field(default_factory=dict)
    failures: list[str] = Field(default_factory=list)
    volatile_excluded: list[str] = Field(
        default_factory=list,
        description=("Fields excluded BY NAME because they vary per render. Excluding by name rather "
                     "than ignoring metadata wholesale means a NEW source of drift still fails."))

    model_config = {"json_schema_extra": {"example": {
        "equivalent": True, "pages": [230, 230], "text_equal": True,
        "pixel_pages_checked": [1, 39, 77, 115, 153, 191, 230], "pixel_mismatches": [],
        "meta_equal": True, "meta_diff": {}, "failures": [],
        "volatile_excluded": ["/ID[1]", "/CreationDate", "/ModDate"]}}}
