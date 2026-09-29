# Compiling a manuscript through the API

For a manuscript or preprint, using the `quarto-render` engine. For a thesis with
spliced publisher PDFs, see [compile-a-hybrid-thesis.md](compile-a-hybrid-thesis.md)
— that is a different engine, not a different option.

## What the engine does

One `quarto render` per requested format. There is no measure pass, no page
reservation and no splice, because a preprint has no inserted papers and no faculty
front matter.

## The bundle

Self-contained, laid out relative to the entry document:

```
index.qmd          the manuscript
_metadata.yml      authors, affiliations, ORCIDs, abstract, keywords (optional)
references.bib     BibTeX
figures/           referenced as figures/<name>.png from the document
```

Paths may not escape the document's directory; a `..` component is refused. A
bundle that reaches outside itself is not portable.

## Compile

```bash
curl -s -X POST http://localhost:8080/compile/manuscript \
  -F "sources=@index.qmd" \
  -F "sources=@_metadata.yml" \
  -F "bibliography=@references.bib" \
  $(for f in figures/*.png; do printf ' -F figures=@%s;filename=%s' "$f" "$f"; done) \
  -F "profile=biorxiv-dev" \
  -F "outputs=pdf,docx" | jq
```

Returns **202** with a job id. Poll it:

```bash
JOB=…
until [ "$(curl -s localhost:8080/jobs/$JOB | jq -r .state)" != "running" ]; do sleep 3; done
curl -s localhost:8080/jobs/$JOB | jq '{state, compliant, duration_s, artifacts}'
```

A manuscript takes roughly 8 s for PDF + DOCX.

```bash
curl -s -o manuscript.pdf  localhost:8080/jobs/$JOB/artifacts/index.pdf
curl -s -o manuscript.docx localhost:8080/jobs/$JOB/artifacts/index.docx
```

## `compliant: null` is correct here

`biorxiv-dev` defines **no compliance rules** — it is for internal development
PDFs, not submission. So jobs report `compliant: null` rather than `true`. A
profile with nothing to check must not claim compliance.

Checks are hygiene only: unresolved cross-references and missing figures.

## Advanced: `quarto_metadata`

Any key merged into the metadata the render sees, after the profile, so it
genuinely overrides:

```bash
  -F 'quarto_metadata=
draft-date: false
floatsintext: false
'
```

Around twenty keys are refused by name because they run code or read a
client-chosen path; `GET /profiles` returns the current list with reasons. Refusal
is a 400 at submit time, not a failure after a build.

The job manifest returns `effective_quarto_metadata`, so what was merged is never a
guess:

```bash
curl -s localhost:8080/jobs/$JOB | jq '.manifest.effective_quarto_metadata | {mainfont, monofont, papersize, format}'
```

## Two things that will surprise you

**The body font changes the page count.** The reference manuscript sets 28 pages in
Monaspace Argon and 21 without it — a 33% swing from one key, because the face is
monospaced. Check `manifest.fonts.substituted` before drawing any conclusion from a
page count.

**A fresh render will not match an archived PDF byte for byte**, and usually not on
the page either: `draft-date: true` stamps the build date on the title page, which
is rendered content and shifts the layout around it. That is a per-build value, not
drift. Use `POST /equivalence` rather than a checksum, and expect the date line to
differ.
