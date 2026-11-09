# abc-print-svc

Document compilation and compliance verification for thesis-style documents.

**The encoded rulebook is the product; the renderer is delivery.** A student can
already turn text into a PDF. What costs them weeks near submission is satisfying
a faculty rulebook that nobody has written down in executable form.

Available as a **library** and an **HTTP API** over the same functions; a CLI is
planned as a third shell over the same code, not a reimplementation.

---

## Status

Two engines, two profiles, verified against real documents rather than fixtures:

- a **hybrid thesis** — 9 chapters, 4 spliced papers, 101 figures, 16
  bibliographies, 21 checks, ~33 s
- a **manuscript** — the nf-nomad bioRxiv bundle, 28 pages matching its archived
  build exactly, PDF + DOCX in ~9 s

Not yet: webhooks, PDF password protection, or a `/validate` endpoint.

## How it runs

Each build is **its own Nomad job**, in a container that carries the toolchain —
the way Nextflow runs each task. Two images, built together from one Containerfile:

| image | target | what it is |
|---|---|---|
| `ghcr.io/abc-cluster/abc-print-svc` | `api` | the **front door**, on [abc-dispatch](https://github.com/abc-cluster/abc-dispatch): validates, admits, queues, and dispatches each run. Carries **no** profile, pipeline or toolchain. |
| `ghcr.io/abc-cluster/abc-print-worker` | `worker` | the **compilation pipeline**: quarto, typst, fonts, profiles, the thesis pipeline. Runs one build per dispatched job, with no network. |

The front door dispatches children of an operator-registered parameterized job,
`abc-print-run`, in a namespace no member can submit to (`nomad/`); a request can
fill only its `meta` slots, never choose an image or a volume. Its Nomad
credential is its own workload identity, granted dispatch by a job-scoped policy —
there is no token to store. Runs are kept in a durable store, so a restart
re-adopts builds in flight; admission bounds the queue with a per-caller fair
share; outputs expire after their retention window.

Why two images: on 2026-11-08 private render logs shipped in the public API image,
because compilation assets were vendored into it. The API image now has nowhere
for them to come from, and its build checks that it carries none.

## Documentation

| | |
|---|---|
| **[Quickstart: using the API](docs/quickstart.md)** | **start here.** Token to finished PDF, with files the examples create themselves |
| [Writing a client / plugin](docs/api-integration.md) | the contract, job lifecycle, error shapes, config sourcing |
| [Compile a hybrid thesis](docs/compile-a-hybrid-thesis.md) | conventional + published + manuscript chapters |
| [Compile a manuscript](docs/compile-a-manuscript.md) | the quarto-render engine |
| [Compile from a zip](docs/compile-from-a-zip.md) | project-garden layout, no plugin needed |
| [Authentication](docs/authentication.md) | tokens, scopes, how they are configured |
| [Concurrent use](docs/concurrency.md) | the render queue, 429, retention |
| [Building and running](docs/building.md) | build from source, CORS, runtime options |
| [Demo documents](examples/README.md) | fixture content; `./examples/run-demo.sh` verifies a deployment |
| `/redoc` · `/docs` | live API reference and try-it-out, vendored into the image |

## Quick start

On one machine, without Nomad, the worker image serves the API itself and builds
in-process:

```bash
docker run --rm -p 8080:8080 -e ABC_PRINT_SVC_AUTH=disabled \
  -e ABCPRINT_CAPABILITIES=/srv/capabilities.json \
  ghcr.io/abc-cluster/abc-print-worker:0.15.0 \
  python3 -m uvicorn --factory abcprint.service:build --host 0.0.0.0 --port 8080
./examples/run-demo.sh          # confirm it works, using fixture content
```

(`ABC_PRINT_SVC_AUTH=disabled` is for a local machine only; `/health` says so.)
On the cluster, deploy the two jobs in `nomad/` — see
[docs/building.md](docs/building.md#deploying-on-nomad). 0.15.0 is published for
`linux/amd64`.

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
from abcprint import toolchain, fonts, equivalence, profile

toolchain.detect().problems()                 # [] when the deployment can build
fonts.health(["Calibri", "Cambria"])          # what will actually render
equivalence.compare("a.pdf", "b.pdf")         # are these the same document?
profile.load("biorxiv-dev@0.1")               # the rulebook as data
```

## Engines and profiles

A profile **selects** an engine and configures it. That is not an accident of
naming: the manuscript path needs none of the thesis engine — no measure pass, no
page reservation, no splice — because those exist to serve inserted papers and a
faculty front matter.

| profile | engine | for | compliance rules |
|---|---|---|---|
| `su-fmhs` | `thesis-assemble` | SU/FMHS thesis, spliced papers | embedded in the template |
| `biorxiv-dev@0.1` | `quarto-render` | internal development PDFs | **none, by design** |

A profile with no rules reports `compliant: null`, never `true`.

## Endpoints

Two fronts onto the same runs. A 0.14 job id is a native run id.

**Native** (abc-dispatch): `POST /v1/uploads`, then `POST /v1/{thesis|manuscript|equivalence}/runs`;
`GET …/runs/{id}` (state, `outcome` with `compliant`, `checks`, `manifest`),
`…/status`, `…/log`, `…/outputs/{name}`, `…/bundle`, `POST …/cancel`, `DELETE …`;
`/v1/limits`, `/readyz`, `/service-info`. Send an `Idempotency-Key` on create.

**0.14**, unchanged for existing clients:

| method | path | purpose |
|---|---|---|
| POST | `/compile` | Thesis. **202** + job id. |
| POST | `/compile/manuscript` | Manuscript. **202** + job id. |
| POST | `/compile/archive` | A zip in project-garden layout. **202** + job id. |
| GET | `/jobs/{id}` | State, manifest, check report, `compliant`. |
| GET | `/jobs/{id}/artifacts/{name}` | Download an artefact. |
| POST | `/jobs/{id}/deliver` | Deliver to `downloads` or `minio`. |
| GET | `/profiles` | Profiles, engines, and the refused `quarto_metadata` keys. |
| GET | `/delivery/destinations` | What this deployment can deliver to. |
| GET | `/health` | Can this deployment build? **503** when not. |
| GET | `/toolchain` · `/fonts` | What rendered this; what will actually render. |
| POST | `/equivalence` | Are two builds the same document? |

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

**Measured, not assumed:** the same thesis renders **230 pages** with licensed
Calibri/Cambria mounted and **224 pages** with the shipped libre set — both fully
correct, all checks passing, but a **6-page difference**. Since the faculty length
rule is measured in **pages**, a substitution is a compliance event, not a
cosmetic one. `on_substitution` defaults to `fail`, and a
substitution is always declared. Whether a given substitution preserves pagination
is a *measurable* question — render both ways and compare with `/equivalence`.

**Open — needs SU IT:** whether the site licence covers a server rendering for many
users. If not, the libre set is the fallback and the length rule must be checked
against a locally rendered copy.

---

## Build

```bash
./docker/vendor-pipeline.sh       # the thesis pipeline + SU/FMHS profile (optional)
./docker/vendor-dispatch.sh       # abc-dispatch, as a wheel from its pinned tag
docker build --target worker -t abc-print-worker:local -f Containerfile .
docker build --target api    -t abc-print-svc:local    -f Containerfile .
```

The thesis pipeline lives in a separate private repo and is **optional** — without
it the worker runs everything except the thesis engine, and `/health` reports
`engines_available`. abc-dispatch is private too, so it is vendored as a wheel
rather than installed from git in the build. Full detail, including the BuildKit stall
and CORS, is in **[docs/building.md](docs/building.md)**.

## Not in scope

An Overleaf. A general LaTeX service. A Zotero integration — citations arrive as
`.bib`. Client-supplied executable filters, ever. A replacement for the local
write loop; sub-second iteration will always beat a cluster round trip.
