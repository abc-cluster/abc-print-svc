# Compiling a hybrid thesis through the API

A *hybrid* thesis mixes conventional chapters you wrote as markdown with
manuscript-based chapters that are published papers. The three kinds are handled
differently, and knowing which chapter is which is most of what you need to get
right:

| chapter kind | you send | what the service does |
|---|---|---|
| **conventional** | the markdown | typesets it to the faculty template |
| **published paper** | a short preamble markdown **and** the publisher's PDF | splices the PDF in whole; it keeps its own reference list, because that is the version of record and must not be altered |
| **manuscript** | a short preamble markdown **and** the manuscript PDF | same splice, but typeset to the thesis guidelines as a journal would carry it |

Worked example, using the reference dissertation's shape — chapters 01, 06–09
conventional; 02 and 03 published; 04 and 05 manuscripts:

```
01-introduction.md        conventional
02-magma.md               published   -> ch02-magma-ploscompbiol-2023.pdf
03-mtbseq-nf.md           published   -> ch03-mtbseq-nf-microorganisms-2025.pdf
04-nf-nomad.md            manuscript  -> ch04-nf-nomad-manuscript-local.pdf
05-abc-cluster.md         manuscript  -> ch05-abc-cluster-manuscript-local.pdf
06-mtb-resistotyper-ml.md conventional
07-discussion.md          conventional
08-conclusion-and-future-directions.md
09-appendices.md
```

Each paper chapter's markdown is a **preamble** — the linking statement and the
co-author contribution statement — ending at the point the paper is spliced in.
Inserted PDFs pair with a chapter by their `chNN-` prefix.

---

## 0. Check the deployment before blaming your document

```bash
curl -s http://localhost:8080/health | jq '{ok, problems}'
```

`ok: false` returns **503** and `problems` names each reason. Two worth knowing:
a `pypdf` too old to merge (internal links would silently dangle), and fonts that
are not installed.

```bash
curl -s http://localhost:8080/fonts | jq '.resolutions[] | {requested, resolved, metric_compatible}'
```

If `resolved` differs from `requested` you are getting a **substitution**. The
faculty length rule is measured in pages, so that matters — see §6.

## 1. Front matter

Send these alongside the chapters. They are recognised by exact filename:

```
Declaration.md                  Dedication.md
Acknowledgements.md             Declaration-of-contribution.md
Abbreviations.md                Research-outputs.md
Prologue.md
```

`Declaration-of-contribution.md` is the one a hybrid thesis cannot omit: it is
the declaration of co-author contributions, with the CRediT roles spelled out.

## 2. Submit the build

```bash
curl -s -X POST http://localhost:8080/compile \
  -F "sources=@writeup/thesis/01-introduction.md" \
  -F "sources=@writeup/thesis/02-magma.md" \
  -F "sources=@writeup/thesis/03-mtbseq-nf.md" \
  -F "sources=@writeup/thesis/04-nf-nomad.md" \
  -F "sources=@writeup/thesis/05-abc-cluster.md" \
  -F "sources=@writeup/thesis/06-mtb-resistotyper-ml.md" \
  -F "sources=@writeup/thesis/07-discussion.md" \
  -F "sources=@writeup/thesis/08-conclusion-and-future-directions.md" \
  -F "sources=@writeup/thesis/09-appendices.md" \
  -F "sources=@writeup/thesis/Declaration.md" \
  -F "sources=@writeup/thesis/Dedication.md" \
  -F "sources=@writeup/thesis/Acknowledgements.md" \
  -F "sources=@writeup/thesis/Declaration-of-contribution.md" \
  -F "sources=@writeup/thesis/Abbreviations.md" \
  -F "sources=@writeup/thesis/Research-outputs.md" \
  -F "sources=@writeup/thesis/Prologue.md" \
  -F "inserted=@writeup/inserted-papers/ch02-magma-ploscompbiol-2023.pdf" \
  -F "inserted=@writeup/inserted-papers/ch03-mtbseq-nf-microorganisms-2025.pdf" \
  -F "inserted=@writeup/inserted-papers/ch04-nf-nomad-manuscript-local.pdf" \
  -F "inserted=@writeup/inserted-papers/ch05-abc-cluster-manuscript-local.pdf" \
  -F "bibliography=@writeup/references.bib" \
  $(find writeup/thesis -name '_*-refs-*.bib' -exec printf ' -F bibliography=@%s' {} \;) \
  $(cd writeup && find figures -type f -exec printf ' -F figures=@figures/%s;filename=%s' {} {} \;) \
  -F "copy=examination" \
  -F "checks=true" | jq
```

Returns **202** with a job id and the chapter prefixes it detected:

```json
{ "id": "6f1c…", "state": "queued", "detected_chapters": ["01","02","03","04","05","06","07","08","09"] }
```

**Check `detected_chapters` against what you sent.** It is the cheapest way to
catch a file that did not upload or is misnamed.

To build a subset, pass `chapters`:

```bash
-F "chapters=01,04,07"        # one conventional, one manuscript, one conventional
```

## 3. Poll

The build is **multi-pass** — render once to measure where each inserted paper
lands and how many pages it needs, re-render reserving that many pages, then
splice — so it returns a job rather than blocking.

```bash
JOB=6f1c…
until [ "$(curl -s localhost:8080/jobs/$JOB | jq -r .state)" != "running" ]; do sleep 5; done
curl -s localhost:8080/jobs/$JOB | jq '{state, duration_s, checks, artifacts}'
```

## 4. Read the check report before the PDF

```bash
curl -s localhost:8080/jobs/$JOB | jq '.checks | {passed, failed, warnings, failures}'
```

`failed: 0` with a non-zero `passed` is `all_clear`. These catch the defects that
ship silently: dangling internal links, figures numbered out of sequence, a list
of figures that disagrees with the body, unresolved cross-references, captions
separated from their table.

## 5. Get the document

Download directly:

```bash
curl -s -o thesis.pdf localhost:8080/jobs/$JOB/artifacts/thesis-assembled.pdf
```

Or have the service deliver it. Check what this deployment supports first:

```bash
curl -s localhost:8080/delivery/destinations | jq
```

**To your Downloads folder** (the operator must have mounted one):

```bash
curl -s -X POST localhost:8080/jobs/$JOB/deliver \
  -F "artifact=thesis-assembled.pdf" -F "destination=downloads" \
  -F "filename=thesis-examination.pdf" | jq
```

**To your object-store prefix:**

```bash
curl -s -X POST localhost:8080/jobs/$JOB/deliver \
  -F "artifact=thesis-assembled.pdf" -F "destination=minio" \
  -F "bucket=su-yourgroup" -F "key=users/you/thesis-examination.pdf" | jq
```

> PDF password protection is **not implemented**. It will attach at this
> delivery boundary when it is. Until then, do not assume a delivered file is
> protected.

## 5a. Fonts change your page count — measure on the copy you will submit

Measured on the reference thesis, same sources, same service:

| fonts | pages | checks |
|---|---|---|
| licensed Calibri/Cambria (mounted) | **230** | all pass |
| libre Carlito/Caladea (shipped) | **224** | all pass |

Both are correct, fully-rendered documents. But that is a **6-page (2.6%)
difference**, and the faculty length rule is measured in pages. Do not measure
length on a substituted build.

Check which you got:

```bash
curl -s localhost:8080/jobs/$JOB | jq '.manifest.fonts.substituted'
```

An empty list means the licensed fonts were mounted and resolved.

## 6. Examination copy vs submission copy

They are two variants of one profile, and the difference is not cosmetic:

```bash
-F "copy=examination"   # default: no branded title frame, no Afrikaans Opsomming
-F "copy=submission"    # adds both
```

On the reference thesis that is 230 pages versus 231. If your faculty measures
length in pages, build the copy you will actually submit before measuring.

## 7. Confirm a rebuild is the same document

Two builds of one bundle are **not** byte-identical — typst writes resource
dictionaries in per-process order and stamps a random instance id, so ~84,000
bytes differ at identical length while the document is unchanged. Compare
properly instead:

```bash
curl -s -X POST "localhost:8080/equivalence?sample=8" \
  -F "a=@build-1.pdf" -F "b=@build-2.pdf" | jq '{equivalent, pages, text_equal, pixel_mismatches}'
```

This is also how you answer whether a font substitution moved your pagination:
build once with the licensed fonts mounted and once without, and compare.

---

## Gotchas that cost real time

- **Chapter selection takes the shortest filename per prefix.** Sending both
  `06-x.md` and `06-x-supplement.md` builds the *supplement* as chapter 6.
- **Strip your own numbering.** The profile numbers headings and figures. Hand
  numbering on top produces `Figure 6.4 Figure-1:` — a defect the reference
  thesis actually shipped.
- **Two reference syntaxes coexist and only one fails loudly.** A markdown
  `@fig-x` cannot resolve a raw typst `<label>`; it renders as `?@fig-x` and the
  build stays clean.
- **Send every bibliography, not just `references.bib`.** The pipeline also
  auto-includes `writeup/thesis/_*-refs-*.bib`. Miss them and the citations they
  carry vanish with nothing louder than a citeproc warning.
- **Figures keep their subpaths.** `figures/F6.2-metro-src/…svg` must be sent
  with its relative path (`;filename=` in curl), or the render dies on a missing
  file. Flattening to a basename does not work.
- **Citations arrive as BibTeX.** Plain-text citations are not recoverable and
  are better rejected than silently rendered as prose.
- **The reference list spans spliced PDFs.** Keys cited *inside* an inserted
  manuscript reach the consolidated bibliography through the nocite union, so
  each work appears both after its own paper and in the thesis bibliography.
