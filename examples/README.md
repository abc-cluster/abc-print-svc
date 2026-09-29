# Demo documents

Fixture content for testing a deployment, so nothing here depends on a private
repository.

**Everything in these files is invented** — every author, affiliation, result,
citation and figure. None of it is drawn from real work, and no sentence in them
should be mistaken for a claim about anything.

| | engine | use |
|---|---|---|
| `manuscript-demo/` | `quarto-render` | works in every image |
| `thesis-chapter-demo/` | `thesis-assemble` | only in an image built with the thesis pipeline |

## One command

```bash
./examples/run-demo.sh                      # against http://localhost:8080
./examples/run-demo.sh http://host:8080     # somewhere else
```

It checks health, submits `manuscript-demo`, polls, downloads the PDF, and then
**checks the content** — that citations resolved, the bibliography rendered, the
figure and table cross-references resolved, and the figure actually drew. Exits
non-zero on any failure and prints the build log when a render fails.

Rendering is not enough on its own: a build can succeed while silently dropping
citations or a figure, which is exactly what these checks catch.

## Why the fixtures look like this

A one-paragraph document renders under almost any misconfiguration, which makes
it useless for catching defects. Each fixture therefore carries the constructs
that have actually broken builds here:

- **a figure with a caption and a cross-reference** — a figure path that does not
  resolve stops the render, and the service requires document-relative paths
- **a table with a caption and a label** — captions separated from their table
  are a recurring defect
- **citations against a bibliography** — a bibliography that is not sent is
  reported only as a *warning*, and the citations then vanish silently
- **inline code and a fenced block** — these take different paths through the
  renderer than prose

The figures are generated SVGs rather than binary images: they are diffable,
carry no data, and typst embeds them as vector graphics. That is worth knowing
when verifying — `pdfimages` finds **zero** images in the output, because there
is no bitmap. The right check is that the SVG's own text appears in the PDF.

## The YAML in a compilation

Four YAML documents take part, and they are not interchangeable. Only the first
two are yours to write.

| file | who writes it | sent? | what it decides |
|---|---|---|---|
| `manuscript-demo/_metadata.yml` | you | yes, as a `sources` part | what the document **is** — authors, affiliations, abstract, keywords |
| `manuscript-demo/quarto-metadata.yaml` | you, optionally | yes, as the `quarto_metadata` **field** | per-build overrides; the advanced escape hatch |
| `profiles-src/biorxiv-dev/profile.yaml` | the profile owner | no — it lives in the service | fonts, page setup, reference style, which engine |
| `_quarto.yml` | the **engine**, at build time | no | declares the Quarto project so typst's root encloses the extension |

Precedence is a single chain, lowest first — the profile sits above your
`_metadata.yml` because fonts and page setup are its call, and
`quarto-metadata.yaml` sits above the profile, because an escape hatch that
cannot override is not one:

```
_metadata.yml  ->  profile  ->  profile.quarto_metadata  ->  quarto-metadata.yaml
```

Two things worth knowing when sending them:

- `_metadata.yml` goes in as a **`sources`** part, but `quarto_metadata` is a
  **string field**. With curl that is `-F "quarto_metadata=<file"` — a `<`, not
  an `@`, because `@` would upload it as a file part.
- What was actually merged is returned, so it is never a guess:
  ```bash
  curl -s localhost:8080/jobs/$JOB | jq .manifest.effective_quarto_metadata
  ```

The profile is not sent; read the live one instead:

```bash
curl -s localhost:8080/profiles | jq '.profiles[] | select(.name=="biorxiv-dev")'
```

> A `build.yaml` — declaring chapters, their kind, front-matter order and
> compliance rules — is **designed but not implemented**. It is deliberately not
> shipped here: a fixture carrying a config file that nothing reads would be
> worse than none.

## By hand

```bash
cd examples/manuscript-demo
curl -s -X POST http://localhost:8080/compile/manuscript \
  -F "sources=@index.qmd" \
  -F "sources=@_metadata.yml" \
  -F "bibliography=@references.bib" \
  -F "figures=@figures/blocks.svg;filename=figures/blocks.svg" \
  -F "profile=biorxiv-dev" \
  -F "outputs=pdf,docx" | jq
```

Note the `;filename=figures/blocks.svg`. Figures keep the path the document
references them by; sending the bare basename makes the reference unresolvable.

## The thesis chapter

`thesis-chapter-demo/` targets the other engine and needs an image built with the
thesis pipeline. Check first:

```bash
curl -s localhost:8080/health | jq .engines_available
```

If `thesis-assemble` is `false`, that image cannot build it and `POST /compile`
returns 503 saying so. See [docs/building.md](../docs/building.md).

```bash
cd examples/thesis-chapter-demo
curl -s -X POST http://localhost:8080/compile \
  -F "sources=@01-introduction.md" \
  -F "bibliography=@references.bib" \
  -F "figures=@figures/F1.1-placeholder.svg;filename=F1.1-placeholder.svg" \
  -F "copy=examination" -F "checks=true" | jq
```

**Expect `compliant: false`, with `L3` failing.** That is the fixture being
honest rather than broken: `L3` requires **at least 50 DOIs** in the reference
list, a threshold hardcoded for a complete thesis (the real one has 154). A
single chapter cannot reach it, and padding the fixture with fifty invented
references to satisfy a counter would teach the wrong thing.

18 of 19 applicable checks pass. The figure is named `F1.1-placeholder.svg`
because check `A3` requires a figure's filename to encode the number it renders
under — worth copying in real chapters.

> This is a real limitation rather than a quirk of the fixture: `L3`'s threshold
> is a **profile-specific rule living as a constant in the checker**. Moving
> thresholds like it into profile data is exactly what the configuration work is
> for.
