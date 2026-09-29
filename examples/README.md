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
