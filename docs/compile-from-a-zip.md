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
