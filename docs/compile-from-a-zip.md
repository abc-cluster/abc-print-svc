# Compiling from a zip (project-garden layout)

`POST /compile/archive` takes a whole manuscript directory as one zip, so a
project created from the **abc project-garden** template can be submitted with no
rearranging — and without an editor plugin.

## The expected shape

```
src/
  index.qmd          the document
  _metadata.yml      authors, affiliations, abstract   (optional)
  references.bib     BibTeX                            (optional)
assets/
  figures/current/   referenced as ../assets/figures/current/x.png from src/
  tables/current/
  supplemental-data/
```

This is the garden manuscript layout as it ships. Zipping the `manuscript/`
folder itself is fine: a single wrapping directory is stripped, and only when
every entry shares it.

```bash
cd writeup/manuscript
zip -qr /tmp/manuscript.zip .

curl -s -X POST http://localhost:8080/compile/archive \
  -F "bundle=@/tmp/manuscript.zip" \
  -F "profile=biorxiv-dev" \
  -F "outputs=pdf,docx" | jq
```

Returns **202** with a job id; poll `/jobs/{id}` exactly as for the other
endpoints.

## Structure is preserved, not flattened

This is the reason the endpoint exists. A garden manuscript references its
figures as `../assets/figures/current/x.png` **from `src/`** — the asset sits
beside the source directory, not inside it. Flattening the archive would make
every one of those references unresolvable, so the archive is extracted as-is and
the profile is layered around it.

The multipart endpoints (`/compile/manuscript`) take document-relative paths
instead and require a self-contained bundle. Both are valid; the zip is the one
that matches an existing project on disk.

## Which document gets rendered

`src/index.qmd` by default. Otherwise a bare `index.qmd`, otherwise a sole
`.qmd` anywhere in the archive. If several `.qmd` files exist and none is
`index.qmd`, the request is **refused** rather than guessed at — rendering the
wrong document silently is worse than asking. Name one explicitly:

```bash
  -F "entry=chapter-two.qmd"      # relative to src/
```

The job echoes what it found, so a mis-shaped archive is visible immediately:

```bash
curl -s localhost:8080/jobs/$JOB | jq .options.archive
# { "entry": "src/index.qmd", "entry_present": true, "stripped_prefix": "manuscript",
#   "counts": { "sources": 3, "figures": 1, "tables": 1, "supplemental": 0 } }
```

## What is refused, and why

An archive from a client is untrusted input, so these are **refused** rather than
skipped — a skipped entry is a silent partial extraction, which is the failure
mode this service exists to avoid:

| refused | because |
|---|---|
| a `..` component | zip slip: escapes the extraction root |
| an absolute path or drive letter | same |
| a symlink | a link to `/etc/passwd` turns a later read into arbitrary file access |
| any non-regular entry | device and FIFO entries have no business in a manuscript |
| more than 5,000 entries | decompression bomb |
| expanding beyond 512 MB total, or 128 MB in one entry | same; enforced on bytes **written**, because a header can lie about its size |

Each is a **400** naming the offending entry:

```json
{ "detail": "refused entry '../../pwned.txt': '..' component" }
```

Limits are configurable per deployment — `ABCPRINT_ZIP_MAX_ENTRIES`,
`ABCPRINT_ZIP_MAX_BYTES`, `ABCPRINT_ZIP_MAX_ENTRY_BYTES`.

## What the profile still owns

The profile's `_extensions` is always the service's own, never the archive's: a
client-supplied Quarto extension is executable input. Anything else the profile
ships (the CSL, for instance) is placed beside the document **only if the archive
did not bring its own**, so a project that pins its own citation style keeps it.

## Try it

`examples/garden-manuscript-demo/` is the fixture in this layout, with the figure
referenced through `../assets/`:

```bash
cd examples/garden-manuscript-demo && zip -qr /tmp/demo.zip . && cd -
curl -s -X POST localhost:8080/compile/archive -F "bundle=@/tmp/demo.zip" -F "profile=biorxiv-dev" | jq
```

---

## The thesis engine takes the same zip — and maps it

A `thesis-assemble` profile (`su-fmhs`) accepts the same archive on the same
endpoint, but does the **opposite** thing with it. The quarto arm preserves the
layout because the document's own relative references are what must keep
working. The thesis pipeline reads **fixed locations** under `writeup/`, and a
chapter references its figures as `figures/F1.1-x.png` — not
`../assets/figures/current/F1.1-x.png`. So here the garden layout is *mapped*:

| in the archive | where it lands | why |
|---|---|---|
| `src/NN-slug.md` | `writeup/thesis/` | chapters, selected by numeric prefix |
| `src/<Other>.md` | `writeup/thesis/` | front matter, read by name (`Dedication.md`, `Acknowledgements.md`, `Declaration.md`, `Declaration-of-contribution.md`, `Abbreviations.md`, `Research-outputs.md`, `Prologue.md`) |
| `src/references.bib` | `writeup/references.bib` | the main bibliography |
| `src/_<area>-refs-<date>.bib` | `writeup/thesis/` | per-area bibliographies, auto-included from there |
| `assets/figures/current/**` | `writeup/figures/**` | subpaths preserved — a chapter can reference `figures/F6.2-src/F6.2-v4.svg` |
| `assets/inserted-papers/**` | `writeup/inserted-papers/` | publisher PDFs spliced in whole |

```bash
cd my-thesis            # src/ + assets/ as above
zip -qr /tmp/thesis.zip .

curl -s -X POST http://localhost:8080/compile/archive \
  -F "bundle=@/tmp/thesis.zip" \
  -F "profile=su-fmhs" \
  -F "copy=examination" \
  -F "checks=true" | jq .options.archive
```

`chapters`, `copy` and `checks` apply on this arm, exactly as on `POST /compile`.
`entry`, `outputs` and `quarto_metadata` do not — the thesis engine builds the
whole document and emits a PDF.

### Nothing is placed where the pipeline cannot read it

A mapping can fail silently in a way a preserved layout cannot, so two cases are
**refusals** rather than best-effort placements:

- **A `.bib` named neither `references.bib` nor `_<area>-refs-<date>.bib`.** The
  pipeline reads those two shapes and nothing else, so any other name would be
  written somewhere and never opened — losing every citation it carries, with
  nothing but a citeproc warning to show for it.
- **A source in a subdirectory of `src/`.** `writeup/thesis` is flat; a nested
  chapter would simply never be picked up.

And everything that *is* placed is reported, so where a file went is never a
guess:

```json
"archive": {
  "layout": "project-garden",
  "placed": {"sources": 17, "bibliography": 16, "figures": 15, "inserted": 5},
  "ignored": ["README.md"],
  "bibliography": ["writeup/references.bib", "writeup/thesis/_ch01-refs-2026-09-23.bib", "…"]
}
```

`assets/figures/archive/` and `assets/figures/raw/` are listed under `ignored`
rather than built with: those are the garden template's own words for "not the
live set". `assets/supplemental-data/` is ignored too — the thesis pipeline reads
nothing there.

### Front matter attaches to the chapter carrying the abstracts

If you build a subset that excludes the introduction (`-F chapters=09`), the
front-matter pages render the template's placeholders. That is the pipeline's own
gating — the abstracts live in `01-introduction.md` as `0-EN`/`0-AF` — not a
mapping failure. A full build picks the pages up from the archive.

### Verified against the multipart path

The zip arm and the multipart `POST /compile` were run on the same document, the
same image and the same profile, and compared with `POST /equivalence` at 110 DPI
with **every page rasterised**: equal page count, equal extracted text, 224 of
224 pages pixel-identical, equal metadata. Submitting a zip is a different way in,
not a different document.

Try it on a fixture that needs nothing private: `examples/garden-thesis-demo/`.
