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
    cors_allowed_origins: list[str] = Field(
        default_factory=list,
        description=("Origins this deployment accepts browser calls from. `[\"*\"]` means any. "
                     "Set ABCPRINT_CORS_ORIGINS to restrict."))
    engines_available: dict = Field(
        default_factory=dict,
        description=("Which engines this image can run. `thesis-assemble` is false when the "
                     "image was built without the thesis pipeline; `quarto-render` always works."))


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


class Artifact(BaseModel):
    name: str = Field(..., description="Filename. Use it with GET /jobs/{id}/artifacts/{name}.")
    bytes: int = Field(..., description="Size of the produced file.")
    media_type: str = Field(..., description="MIME type, e.g. application/pdf.")


class Checks(BaseModel):
    passed: int | None = None
    failed: int | None = None
    warnings: int | None = None
    failures: list[str] = Field(default_factory=list,
                                description="One line per failed check, naming the rule.")
    warning_detail: list[str] = Field(default_factory=list)
    all_clear: bool | None = Field(
        None, description="null when the profile defines no compliance rules.")
    note: str | None = None


class Job(BaseModel):
    """A compilation job. Poll this until `state` is terminal.

    `state` is `queued` -> `running` -> `succeeded` | `failed`. Note that
    **`succeeded` does not mean compliant**: a document can be produced and still
    fail its checks, which is what `compliant` is for. A job only fails when no
    document was produced at all.
    """
    id: str
    state: str = Field(..., description="queued | running | succeeded | failed.")
    created: float
    started: float | None = None
    finished: float | None = None
    duration_s: float = 0.0
    options: dict = Field(default_factory=dict, description="What was requested.")
    artifacts: list[Artifact] = Field(default_factory=list)
    compliant: bool | None = Field(
        None,
        description=("Whether the document satisfies the profile's compliance rules. "
                     "**null** when the profile defines none — a profile with nothing "
                     "to check must not claim compliance."))
    manifest: dict = Field(
        default_factory=dict,
        description=("Everything that decided this output: toolchain versions, the "
                     "profile and its version, font resolutions, the pinned "
                     "SOURCE_DATE_EPOCH, and the effective Quarto metadata. Returned "
                     "WITH the artefact so a rebuild is a claim rather than a hope."))
    checks: Checks = Field(default_factory=Checks)
    error: str | None = Field(None, description="Set when `state` is failed.")
    log_tail: list[str] = Field(
        default_factory=list,
        description="Last lines of the build log. Populated on failure too.")


class JobList(BaseModel):
    jobs: list[Job] = Field(default_factory=list)


class ProfileInfo(BaseModel):
    name: str
    version: str = "unversioned"
    ref: str = Field("", description="name@version — quote this in a compile request.")
    schema_version: int = 1
    engine: str = Field(..., description="thesis-assemble | quarto-render.")
    description: str = ""
    template: dict = Field(default_factory=dict)
    page: dict = Field(default_factory=dict)
    fonts: dict = Field(default_factory=dict)
    bibliography: dict = Field(default_factory=dict)
    layout: dict = Field(default_factory=dict)
    rules: dict = Field(default_factory=dict)
    checks: dict = Field(default_factory=dict)
    open_to_build: list[str] = Field(
        default_factory=list, description="Keys a build request may override.")
    has_compliance_rules: bool = Field(
        False, description="False means jobs under this profile report compliant: null.")


class ProfileList(BaseModel):
    profiles: list[ProfileInfo] = Field(default_factory=list)
    refused_quarto_metadata_keys: dict = Field(
        default_factory=dict,
        description=("Keys refused inside `quarto_metadata`, mapped to the reason. "
                     "Each either runs code or reads a client-chosen path."))


class DeliveryResult(BaseModel):
    destination: str = Field(..., description="downloads | minio.")
    path: str | None = Field(None, description="downloads: where it landed.")
    uri: str | None = Field(None, description="minio: s3://bucket/key.")
    endpoint: str | None = None
    bytes: int
