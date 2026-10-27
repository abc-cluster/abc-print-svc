# Integrating a client with abc-print-svc

For anyone writing a client — a Logseq plugin, an editor extension, CI. It assumes
no context beyond this file and the live schema at `/redoc`.

**What the service is.** It takes document sources plus a *profile* and returns a
rendered document, a **manifest** of everything that decided that output, and a
**check report**. The renderer is the easy half; the report is the point.

---

## 0. Get a token in, first

Every call except `GET /health` and the docs pages needs a bearer token. Without
one you get **401** on everything, which looks like a broken service.

```
Authorization: Bearer abcp_…
```

**Experimenting in a browser.** Open `/docs`, press **Authorize**, paste the
token. Every *Try it out* on that page then carries it, and `/redoc` shows a
padlock on each call that needs one. (Until 2026-10-27 the OpenAPI document
declared no security scheme at all, so there *was* no Authorize button — the
token appeared as an untitled optional header and a client author could only
discover it by reading this file.)

**The cheapest proof your token works**, needing only the `read` scope:

```bash
SVC=https://mbhg-bioinformatics-abc-print-svc.apps.seedling.abc-cluster.cloud
TOKEN=abcp_…

curl -s -H "Authorization: Bearer $TOKEN" $SVC/profiles | jq '.profiles[].ref'
# "biorxiv-dev@0.1"
# "su-fmhs@unversioned"
```

**A whole build, end to end**, using a fixture that ships in the repo so it needs
nothing of your own:

```bash
JOB=$(curl -s -X POST $SVC/compile/archive \
        -H "Authorization: Bearer $TOKEN" \
        -F bundle=@examples/garden-thesis-demo.zip \
        -F profile=su-fmhs -F copy=examination | jq -r .id)

until [ "$(curl -s -H "Authorization: Bearer $TOKEN" $SVC/jobs/$JOB | jq -r .state)" != "queued" ] \
   && [ "$(curl -s -H "Authorization: Bearer $TOKEN" $SVC/jobs/$JOB | jq -r .state)" != "running" ]; do sleep 2; done

curl -s -H "Authorization: Bearer $TOKEN" $SVC/jobs/$JOB | jq '{state, compliant, checks}'
curl -s -H "Authorization: Bearer $TOKEN" -o out.pdf \
     $SVC/jobs/$JOB/artifacts/thesis-assembled.pdf
```

### Scopes

A token carries some subset of three:

| scope | endpoints |
|---|---|
| `read` | `/profiles` `/toolchain` `/fonts` `/jobs` `/jobs/{id}` artefacts `/equivalence` |
| `compile` | `/compile` `/compile/archive` `/compile/manuscript` |
| `deliver` | `/jobs/{id}/deliver` `/delivery/destinations` |

A plugin that submits and downloads wants `compile,read` and should not hold
`deliver` — that scope writes to an object store.

### Jobs are private to the token that created them

`GET /jobs` lists only your own, and another token's job id is a **404**, not a
403, so the response does not confirm it exists. You cannot poll an id someone
else gives you, and nobody can poll yours. This matters if you were planning to
hand job ids around: don't.

### The queue is global, and 429 means wait

The deployment renders two documents at a time with a queue of eight. Past that a
submission is refused with **429** and `Retry-After`. The queue is shared, so a
429 can mean *somebody else* filled it.

**A client that retries immediately on 429 will take the whole service.** That is
measured, not hypothetical: one token submitting in a loop held all ten admission
slots, and the next caller's first request was refused. Back off on 429.

`GET /health` needs no token and reports `auth` and `capacity`, so it answers both
"which deployment am I pointed at" and "is it busy" without credentials.

---

## 1. The five calls

All but `/health` need `Authorization: Bearer abcp_…` — see §0.

```
GET  /health                       can this deployment build at all?  (no token)
GET  /profiles                     what profiles exist, and what may be overridden
POST /compile        (thesis)      or  POST /compile/manuscript   -> 202 + job id
GET  /jobs/{id}                    poll until state is terminal
GET  /jobs/{id}/artifacts/{name}   download
POST /jobs/{id}/deliver            or have the service deliver it
```

Start with `/health`. It returns **503** when the deployment cannot build, with
`problems` naming each reason. Checking it once at plugin start-up turns "the
service is broken" into a specific sentence a user can act on.

## 2. Pick the right compile endpoint

There are two **engines**, and they are different code paths, not options:

| endpoint | engine | for | passes |
|---|---|---|---|
| `POST /compile` | `thesis-assemble` | a thesis with spliced publisher PDFs | render → measure → re-render → splice |
| `POST /compile/manuscript` | `quarto-render` | a manuscript or preprint | one render per format |

`GET /profiles` tells you which engine a profile uses. Posting to the wrong one
returns **400** naming the right one, rather than failing obscurely later.

## 3. The job lifecycle

Compiling returns **202** immediately with a job id. The thesis build is multi-pass
and takes ~30 s; a manuscript takes ~8 s. Neither is a request you hold open.

```
queued -> running -> succeeded | failed
```

**`succeeded` does not mean compliant.** These are three separate facts and a client
should surface them separately:

| field | meaning |
|---|---|
| `state: failed` | no document was produced |
| `state: succeeded` | a document exists |
| `compliant: false` | the document exists **and breaks a rule** |
| `compliant: null` | the profile defines no rules, so the question does not apply |

A job whose checks fail still returns its artefacts, deliberately: discarding the
document would throw away the evidence of what is wrong with it.

Poll with backoff; there is no webhook yet.

## 4. What to send

Multipart. Both endpoints take `sources`, `figures`, `bibliography`.

**Figures keep their relative path.** A document referencing
`figures/diagram.png` must have that file sent as `figures/diagram.png`, not
`diagram.png`. In `curl` that is `-F "figures=@local/path.png;filename=figures/diagram.png"`.

**Paths may not escape the document's directory.** A `..` component is refused.
This is deliberate: a bundle that reaches outside itself is not portable, and the
service declining it is the correct answer. Lay bundles out self-contained.

**Send every bibliography.** The thesis engine also auto-includes
`_<area>-refs-<date>.bib` files. Sending only `references.bib` loses those
citations to nothing louder than a warning.

## 5. Configuration, and where YAML comes from

**The service accepts a YAML document. It does not know or care what produced it.**
A file in the bundle, a posted form field and a block extracted from an editor page
are three sources of one artefact, not three features.

For a Logseq plugin specifically: **put a fenced YAML block in the page header and
extract it verbatim.** Do not reconstruct it from Logseq's flat `key:: value`
properties — that header cannot express the schema (`chapters:` is a list of
objects; `fonts:` is role-keyed with nested stacks), and a lossy property→YAML
mapping introduces a second source of truth. If flat properties are supported
later, the rule must be that the YAML block wins.

Two version axes, and they move independently:

```yaml
schema_version: 1          # the shape of the config file
profile: biorxiv-dev@0.1   # the content of the rules
```

A rules change bumps the profile. A field rename bumps the schema. Quote the
profile with its version in every request, and record the `manifest` you get back:
that is what makes a later rebuild a claim rather than a hope.

## 6. `quarto_metadata` — the escape hatch

`POST /compile/manuscript` accepts a `quarto_metadata` YAML mapping, merged into the
metadata the render sees. It is for advanced users and it genuinely overrides.

One precedence chain, lowest first:

```
bundle _metadata.yml     what the document IS (authors, abstract, keywords)
profile-derived          what the PROFILE governs (fonts, page, csl)
profile.quarto_metadata  profile-level passthrough
build quarto_metadata    this field
```

**Around twenty keys are refused by name** — `execute`, `engine`, `jupyter`,
`knitr`, `filters`, `shortcodes`, `pre-render`, `post-render`, `project`,
`template`, `include-*`, `resource-path`, `pdf-engine`, `output-*`. Each either
runs code or reads a client-chosen path. `GET /profiles` returns the current list
with a reason per key; read it rather than hard-coding one.

Refusal is a **400 at submit time**, not a failure after a build. The bundle's own
`_metadata.yml` gets the same screening, or a refused key would simply move house.

The job manifest returns `effective_quarto_metadata`, so what was actually merged
is never a guess. Show it to the user when a render surprises them.

## 7. Delivery

```
GET  /delivery/destinations        what this deployment supports
POST /jobs/{id}/deliver            destination=downloads | minio
```

Check destinations first: an unconfigured one returns **503** with the environment
variables the operator needs to set. `downloads` requires the operator to have
mounted a directory; `minio` requires S3 credentials.

**PDF password protection is not implemented.** `/delivery/destinations` reports
that explicitly. Do not tell a user a delivered file is protected.

## 8. Reproducibility — do not checksum PDFs

Two builds of identical sources are **not byte-identical**, and this is measured
rather than assumed: the same 230-page thesis built twice on one machine 30 seconds
apart produced files of identical length differing in ~84,000 bytes. Typst writes
resource dictionaries in per-process order and stamps a random instance id.

`SOURCE_DATE_EPOCH` is pinned by the service, which removes the timestamps but not
the ordering. A checksum comparison will therefore report a false difference.

Use `POST /equivalence` instead. It compares on four planes — page count, extracted
text with layout, rendered pixels, and metadata with volatile fields excluded **by
name** so new drift still fails.

> Two traps if you write your own comparison. A **small** document *does* compare
> byte-identical once `SOURCE_DATE_EPOCH` is pinned, while a full one does not — so
> validating on a small sample gives the wrong answer. And `/ID[0]` looks like a
> content fingerprint but is shared by a 230-page thesis and a one-chapter subset.

## 8a. The manifest names the rulebook, not just the tools

`manifest.pipeline` records which revision of the verification pipeline checked a
document:

```bash
curl -s localhost:8080/jobs/$JOB | jq .manifest.pipeline
```

This matters because the pipeline lives in a **separate repository** and changes
independently of the service. On 2026-10-07 check `C1` broadened to catch the
renderer's own scaffold markers, after five unresolved citation markers reached a
chapter with every check reporting clear. A document built before that day and
one built after were verified against **different rules**, and a manifest naming
only tool versions could not show it.

Record `manifest.pipeline.revision` alongside the artefact. `uncommitted_changes:
true` means the image was built from a working tree with local edits, so the
revision alone does not identify what ran.

## 9. Fonts change page counts

Not cosmetic. Measured on the same sources:

| | pages |
|---|---|
| thesis, licensed Calibri/Cambria mounted | 230 |
| thesis, shipped libre substitutes | 224 |
| manuscript, with Monaspace Argon | 28 |
| manuscript, without it | 21 |

If a profile's faculty rule is measured in pages, a substituted build cannot be used
to measure it. `GET /fonts` and the job manifest both report what actually resolved;
`manifest.fonts.substituted` empty means no substitution occurred.

## 10. Error shapes

| code | meaning | client should |
|---|---|---|
| 400 | bundle or metadata rejected | show `detail`; it names the rule |
| 404 | unknown job or artefact | artefact 404 lists what the job produced |
| 409 | artefact requested before the job succeeded | keep polling |
| 410 | workspace reclaimed | re-submit |
| 422 | malformed upload | show `detail` |
| 503 | deployment cannot build, or destination unconfigured | surface to the operator, not the author |

Errors name the **rule**, not the tool. If you ever see a raw typst or pandoc
traceback reach a user, that is a bug worth reporting — the symptom pointing
somewhere other than the cause is what makes these expensive.


### Authentication and capacity

| code | meaning | retry? |
|---|---|---|
| **401** | no token, or one this deployment does not recognise | no — fix the token |
| **403** | valid token, but it lacks the scope this call needs | no — ask for a wider token |
| **429** | too many builds admitted; `Retry-After` is set | yes, after the delay |
| **410** | the job existed; its artefacts have expired (6h) | rebuild |
| **404** on a job | not yours, or never existed | no |
| **503** | the deployment has no tokens configured at all | no — tell the operator |

The 401/403 split is the one worth handling properly: they need opposite
responses, and a client that treats both as "auth failed" will either retry
forever or give up on a fixable permissions problem.

## 11. Not implemented yet

Stated so a client is not written against them: no authentication, no webhooks, no
durable job store (in-process, bounded, lost on restart), no retention policy, no
PDF password protection, no `/validate` endpoint yet — the plan is source-only
validation that is honest about the page-based length rule needing a render.
