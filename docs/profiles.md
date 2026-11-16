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
- A pin recorded before a profile was published still works. `su-fmhs@git-d0e8657`
  (what 0.17.0 runs recorded) and `su-fmhs@unversioned` resolve to `su-fmhs@1.0.0`,
  which was built from that same revision. The run records the real version.

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
CSL), and optionally:

- `examples/*.zip` — checked against the profile's own rules at publish, and shown
  to clients by `/shape`;
- `fonts/` — font files and their licence, put on the render's font path;
- `pipeline/` — for a `thesis-assemble` profile, **required**: the scripts that
  assemble and verify the thesis. Since 0.18.0 the worker image has none of its own.

None of these is copied beside the document.

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
aliases: []                      # other names a pin may use (e.g. git-<rev>)
fonts_license: OFL-1.1           # required when fonts/ is present (SPDX id)
```

### su-fmhs: built from the thesis repository

The SU/FMHS profile and its pipeline come from the (private) thesis repository.
`bundles/su-fmhs/build.sh` turns one committed revision into a bundle:

```bash
bundles/su-fmhs/build.sh ~/projects/PHD-dissertation <rev> <version> /tmp/su-fmhs-<version> [alias …]
```

It uses `git archive`, not the working tree, so the same revision always gives the
same digest. It drops build artefacts, adds the libre font fallbacks, and runs the
sanitiser, which **fails the build** if any personal string survives. `1.0.0` is
revision `d0e8657` — the content 0.17.0 had baked in — with the aliases
`git-d0e8657` and `unversioned`.

### Fonts in a bundle

`fonts/` may hold only `.ttf`/`.otf`/`.ttc`/`.otc` files and their licence text
(`OFL.txt`, `LICENSE`). `profile.yaml` must name the licence (`fonts_license`, an
SPDX id). Publishing reads each file's family from its `name` table and records
`fonts_shipped: [{file, families, sha256}]`; `requires.fonts` is satisfied by the
bundle's own families as well as the worker's.

A build puts them first on the font path (`TYPST_FONT_PATHS`, and `$HOME/.local/
share/fonts` for fontconfig), ahead of the image's licensed and OFL fonts.

The store is readable by whoever can read the volume or bucket. A bundle that
ships one of the proprietary families the SU template names (Calibri, Cambria, …)
under an open-licence id is refused. Whether a licence covers server-side
rendering for many users is the operator's call, and is not something the
service can check.

### Signatures

A digest proves a bundle is the one the index names. It does not prove the
operator published it: anyone who can write the store can write the index too. So
a version can carry an Ed25519 signature over

```
abc-print-profile-v1 <name>@<version> <digest>
```

The key stays on the operator's machine:

```bash
openssl genpkey -algorithm ed25519 -out ~/.abc/print-svc-profile-signing.pem    # once
openssl pkey -in ~/.abc/print-svc-profile-signing.pem -pubout -outform DER | tail -c 32 | base64
```

The second command prints the public key; deploy with it
(`PROFILE_TRUSTED_KEYS=<it> nomad/deploy.sh <v>`). Then:

```bash
ABCPRINT_SIGNING_KEY=~/.abc/print-svc-profile-signing.pem NOMAD_CMD=… \
  nomad/publish-profile.sh ./my-profile [--promote]          # signed at publish
ABCPRINT_SIGNING_KEY=… NOMAD_CMD=… nomad/publish-profile.sh --sign-only canary-quarto@1
```

The front door computes the statement, `openssl` signs it locally, and only the
signature travels. `--sign-only` signs a version that is already published.

The policy is `ABCPRINT_PROFILE_SIGNATURES=off|warn|require`. It defaults to
`require` whenever trusted keys are set:
- the front door refuses to publish an unsigned version;
- it refuses to admit a run on one (`503 profile_untrusted`, the operator's
  problem, not the caller's);
- the worker checks the signature again before building.

Baked profiles are trusted through the image digest instead.

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

**On seedling the store is a folder on the shared volume**
(`file:///srv/dispatch/abc-print-svc/profile-store`), published through the running
front door — no bucket, no extra credential:

```bash
NOMAD_CMD="abc admin services cli nomad --" nomad/publish-profile.sh ./my-profile [--promote]
NOMAD_CMD=… nomad/publish-profile.sh --promote-only biorxiv-dev@0.2
NOMAD_CMD=… nomad/publish-profile.sh --list
```

Deploy with a store:

```bash
PROFILE_SOURCE=s3://abc-print-profiles/za PROFILE_S3_ENDPOINT=https://… PROFILE_S3_CREDENTIALS=true \
  NOMAD_CMD="…" nomad/deploy.sh <version>
```

The read-only key goes in the Nomad variable `nomad/jobs/<job name>`, as
`profile_s3_access_key` and `profile_s3_secret_key`.

### Not yet

- **Who may sign.** One trusted-key list for the whole service. A per-profile
  signer (for example, Jorge for templates he owns) is not modelled.
- **Key rotation** is "add the new public key, re-sign, remove the old one". There
  is no revocation list beyond removing a key.
