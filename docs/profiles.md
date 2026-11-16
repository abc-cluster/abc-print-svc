# Profiles: publish them, pin them, ask for their shape

A **profile** is the rulebook a document is built against. It holds a template (for
example a Quarto extension, CSL and partials), its settings and compliance rules, and
the rules for what a client zip may contain. Since 0.17.0 a profile can be
**published** and improved without rebuilding or redeploying the worker image. Every
build records exactly which version it used.

## For clients

### Ask what a zip should look like

```bash
curl -s -H "Authorization: Bearer $TOKEN" $SVC/svc/v1/profiles | jq '.profiles[] | {name, default}'
curl -s -H "Authorization: Bearer $TOKEN" $SVC/svc/v1/profiles/biorxiv-dev/shape | jq
curl -s -H "Authorization: Bearer $TOKEN" -o start.zip $SVC/svc/v1/profiles/su-fmhs/skeleton.zip
```

`…/shape` returns:
- `layouts`: what the profile accepts.
  - `garden`: sources under `src/`.
  - `flat`: the document at the top of the zip, moved under `src/` for you.
- `rules`: the exact `required`, `allowed` and `forbidden` paths and `limits`. These
  are the rules the front door enforces, so a zip that follows them is not refused for
  its shape.
- `tree`: an annotated example, with `path`, `role`, `pattern` and `required` for each
  file.

`…/skeleton.zip` is that tree as placeholder files. It is accepted as it is, so
replace the contents and keep the shape.

A client zip never brings code: `.lua` files, `_extensions/` and `_quarto.yml` are
refused. Per-build settings go in `quarto_metadata`, which has its own refused keys.

### Name a version, or take the default

- `"profile": "su-fmhs"` gets the profile's **default** version. The default moves
  only when the operator promotes a version, never just because one was published.
- `"profile": "su-fmhs@0.3"` gets exactly that version.

The front door resolves the name **once, when it admits the run**, so a promote
between admission and the build does not change what you get. The run then records
the version in two places:

```json
"options":  {"profile": "su-fmhs@0.3", "profile_digest": "sha256:…"},
"manifest": {"profile": {"name": "su-fmhs", "version": "0.3", "digest": "sha256:…",
                         "source": "s3://…", "published_at": "…", "engine": "thesis-assemble"}}
```

The digest is a hash of the profile's **content**. Two runs with the same digest
were built against the same rulebook. To rebuild an old document, request its
`name@version`.

## For the operator

### What a bundle is

A bundle is a directory in the same layout as a baked profile. It has
`profile.yaml`, the template files beside it (for example `_extensions/` and a
CSL), and optionally `examples/*.zip`. `examples/` and `fonts/` are never copied
into a build.

```yaml
schema_version: 2
name: biorxiv-dev
version: "0.2"                  # immutable once published
engine: quarto-render
requires:
  quarto: ">=1.7.31 <1.8"       # checked against the worker at publish time
  fonts: ["Monaspace Argon"]
bundle:
  layouts: [garden, flat]
  required: [src/index.qmd]
  allowed: ["src/**", "assets/**"]
  forbidden: ["**/*.lua", "_extensions/**", "**/_extensions/**", "**/_quarto.yml"]
  limits: {max_files: 2000, max_bytes: 268435456}   # may only tighten the global caps
  tree:                          # optional: what /shape shows a client
    - {path: src/index.qmd, role: entry document}
    - {path: assets/figures/current/figure-1.svg, role: figure}
```

### Publish, promote, list

Run these inside the worker image, so the `requires` checks see the worker's real
toolchain:

```bash
docker run --rm -v "$PWD/my-profile:/p:ro" \
  -e ABCPRINT_PROFILE_SOURCE=s3://abc-print-profiles/za \
  -e ABCPRINT_PROFILE_S3_ENDPOINT=… -e ABCPRINT_PROFILE_S3_ACCESS_KEY=… -e ABCPRINT_PROFILE_S3_SECRET_KEY=… \
  ghcr.io/abc-cluster/abc-print-worker:<v> \
  python3 -m abcprint.publish publish /p [--promote] [--deny-list /p/../deny.txt]

python3 -m abcprint.publish promote biorxiv-dev@0.2
python3 -m abcprint.publish list
python3 -m abcprint.publish check ./my-profile       # validate only, nothing uploaded
```

`publish` refuses anything that would otherwise fail later, in a build:
- a missing or unpublishable version;
- a `schema_version` newer than the worker reads;
- a malformed `bundle:` section;
- `requires` the worker cannot meet;
- an example that fails the profile's own rules;
- with `--deny-list`, any file containing a listed term. This privacy check for
  private profiles was previously done by the sanitiser at image build time.

It refuses a version that is already published with different content. The first
version published becomes the default; later ones need `--promote`.

### How a bundle reaches a build

The worker job has **no network**, so it cannot reach the store.
1. When the front door admits a run, it copies the pinned version into a mirror on
   the shared volume (`/srv/dispatch/abc-print-svc/profile-mirror`).
2. The worker resolves the version from that mirror.
3. The worker unpacks the bundle and refuses to build unless the content hashes to
   the recorded digest.

With `profile_source` unset, nothing changes: the profiles baked into the worker
image answer, as before, and now carry a digest too.

Deploy with a store:

```bash
PROFILE_SOURCE=s3://abc-print-profiles/za PROFILE_S3_ENDPOINT=https://… PROFILE_S3_CREDENTIALS=true \
  NOMAD_CMD="…" nomad/deploy.sh <version>
```

The read-only key goes in the Nomad variable `nomad/jobs/<job name>`, as
`profile_s3_access_key` and `profile_s3_secret_key`.

### Not yet

- **Fonts shipped in a bundle** (`fonts/`) are accepted by `publish` but not yet
  added to the render's font path. A profile that needs a font missing from the
  image is refused at publish.
- **The thesis-assemble pipeline scripts are engine code** and stay in the image.
  Only a thesis profile's template and rules are publishable.
- **Signatures.** Bundles are pinned by content digest, not signed.
