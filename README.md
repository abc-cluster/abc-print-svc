# abc-print-svc

Document compilation and compliance verification for thesis-style documents.

**The encoded rulebook is the product; the renderer is delivery.** A student can
already turn text into a PDF. What costs them weeks near submission is satisfying
a faculty rulebook that nobody has written down in executable form.

Available as a **library** and an **HTTP API** over the same functions; a CLI is
planned as a third shell over the same code, not a reimplementation.

---

## Status

Slice 1 — containerise the pipeline and establish what reproducibility means
here. The render endpoint is **not implemented yet**; what ships is the
toolchain/font/verification half, which is what slice 1 needed to answer.

## Quick start

```bash
docker run --rm -p 8080:8080 ghcr.io/abc-cluster/abc-print-svc:0.1.0
```

| surface | where |
|---|---|
| API reference (ReDoc) | http://localhost:8080/redoc |
| Try it out (Swagger UI) | http://localhost:8080/docs |
| OpenAPI schema | http://localhost:8080/openapi.json |

Both doc UIs are **vendored into the image**, not loaded from a CDN, because the
renderer is meant to run with no network egress and documentation that blanks out
in the deployment it documents is not documentation.

As a library:

```python
from abcprint import toolchain, fonts, equivalence

toolchain.detect().problems()                 # [] when the deployment can build
fonts.health(["Calibri", "Cambria"])          # what will actually render
equivalence.compare("a.pdf", "b.pdf")         # are these the same document?
```

## Endpoints

| method | path | purpose |
|---|---|---|
| GET | `/health` | Can this deployment build? **503** when not. |
| GET | `/toolchain` | What rendered this — the manifest half. |
| GET | `/fonts` | Which family will actually render, and is it a substitution? |
| POST | `/equivalence` | Are two builds the same document? |

---

## What slice 1 established

### Byte-identical output is not achievable, and byte comparison is the wrong test

Building the same 230-page thesis twice **on one machine, 30 seconds apart**
produces two files of *identical length* differing in ~**84,000 bytes**:

| source of difference | fixable? |
|---|---|
| `/CreationDate`, `/ModDate` | **yes** — honour `SOURCE_DATE_EPOCH` (verified) |
| `/ID[1]`, XMP `InstanceID` | no — random per render |
| `/Font`, `/XObject`, `/ExtGState` emission order | no — per-process hash order in typst |

The documents are nonetheless the same: equal page count, byte-identical
`pdftotext -layout` output, and **pixel-identical** rendered pages.

So equivalence is decided on four planes — pages, text, pixels, stable metadata —
with volatile fields excluded **by name**, so a *new* source of drift still fails.

> **Trap.** With `SOURCE_DATE_EPOCH` pinned, a **small** document *does* compare
> byte-identical, while the full thesis still differs by ~45,000 bytes. Validating
> reproducibility on a one-chapter sample gives the wrong answer.

> **Trap.** `/ID[0]` (`DocumentID`) looks like a content fingerprint and is not:
> the 230-page thesis and a one-chapter subset share the same value. It cannot be
> used as a reproducibility oracle.

### Quarto pins typst; a standalone typst is a decoy

`quarto typst --version` reports **0.13.0** while `typst --version` on PATH reports
**0.13.1**, and the PDF's `/Creator` records 0.13.0. **Pagination is decided by the
bundled one.** Pinning a standalone typst pins a binary nothing reads, so the image
ships none and `/toolchain` reports `typst_mismatch` when one shadows the bundled
version.

### Fonts

The template names **Calibri, Cambria, Georgia, Times New Roman, Arial, Helvetica
Neue** — all Microsoft's, Monotype's or Apple's, none redistributable in an image.

The image ships **metric-compatible libre substitutes** so it works out of the box
without redistributing anything:

| requested | ships as | metric-compatible |
|---|---|---|
| Calibri | Carlito | yes |
| Cambria | Caladea | yes |
| Georgia | Gelasio | yes |
| Times New Roman | Tinos | yes |
| Arial | Arimo | yes |
| Helvetica Neue | Arimo | **no** — approximation, flagged as such |

Licensed originals mounted at `/usr/local/share/fonts/licensed` win automatically:

```bash
docker run --rm -p 8080:8080 \
  -v /path/to/licensed/fonts:/usr/local/share/fonts/licensed:ro \
  ghcr.io/abc-cluster/abc-print-svc:0.1.0
```

Because the faculty length rule is measured in **pages**, a substitution is a
compliance event, not a cosmetic one. `on_substitution` defaults to `fail`, and a
substitution is always declared. Whether a given substitution preserves pagination
is a *measurable* question — render both ways and compare with `/equivalence`.

**Open — needs SU IT:** whether the site licence covers a server rendering for many
users. If not, the libre set is the fallback and the length rule must be checked
against a locally rendered copy.

---

## Build

```bash
./docker/vendor-pipeline.sh          # vendor the pipeline scripts into the context
docker build --platform linux/amd64 -t abc-print-svc:0.1.0 -f Containerfile .
```

The pipeline scripts live in the dissertation repo today; extracting them is
slice 2's work, and `docker/vendor-pipeline.sh` is the single place that knows
where they came from.

Font and documentation downloads are **fatal on failure** by design. An earlier
revision swallowed them with `|| echo NOTE` and produced an image that built
"successfully" with Georgia missing — the same shape of defect as the qpdf merge
fallback, where a warning nobody reads stands in for a failure.

## Not in scope

An Overleaf. A general LaTeX service. A Zotero integration — citations arrive as
`.bib`. Client-supplied executable filters, ever. A replacement for the local
write loop; sub-second iteration will always beat a cluster round trip.
