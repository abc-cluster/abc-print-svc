# Quickstart: using the API

How to compile a document through the API, starting from nothing. Every command
here is tested against a live deployment. You do not need a copy of this
repository: the examples create the files they need.

You need:

- the service URL
- a token (ask whoever runs the deployment; it starts `abcp_`)
- `curl`, and `jq` if you want readable output

```bash
SVC=https://your-deployment.example
TOKEN=abcp_your_token_here
```

## 1. Check the service, without a token

`/health` is the only endpoint that needs no token.

```bash
curl -s $SVC/health | jq '{ok, auth, capacity, engines_available}'
```

```json
{
  "ok": true,
  "auth": { "mode": "token", "tokens": 2 },
  "capacity": { "max_renders": 2, "max_queued": 8, "rendering": 0, "waiting": 0 },
  "engines_available": { "quarto-render": true, "thesis-assemble": true }
}
```

`ok: false` means the deployment cannot build. `problems` says why. Check this
first when something fails, before you suspect your own document.

## 2. Try it in the browser

Open `$SVC/docs`, click **Authorize**, paste your token. Every *Try it out*
button on that page then sends it. `$SVC/redoc` is the same API as a reference
page.

That is the fastest way to look around. The rest of this guide uses `curl`, so you
can paste it into a script.

## 3. Check your token works

```bash
curl -s -H "Authorization: Bearer $TOKEN" $SVC/profiles | jq '.profiles[].ref'
```

```
"biorxiv-dev@0.1"
"su-fmhs@unversioned"
```

A **profile** is a named set of rules: fonts, layout, citation style, and the
checks a document must pass. You pick one per build.

If this returns `401`, the token is missing or wrong. See *Errors* below.

## 4. Build something small

The service takes a zip. Make one:

```bash
mkdir -p paper/src
cat > paper/src/index.qmd <<'EOF'
---
title: A very small paper
author:
  - name: R. E. Searcher
    affiliations: Department of Invented Examples
    email: searcher@example.edu
    corresponding: true
---

## Introduction

One paragraph, so the build has something to do.
EOF

(cd paper && zip -qr ../paper.zip .)
```

Send it:

```bash
JOB=$(curl -s -X POST $SVC/compile/archive \
        -H "Authorization: Bearer $TOKEN" \
        -F bundle=@paper.zip \
        -F profile=biorxiv-dev | jq -r .id)
echo $JOB
```

apaquarto needs an affiliation and one corresponding author. Without them the
document still builds, but the log fills with errors. Those are easy to miss, so
check `log_tail` even on a build that succeeded.

You get a job id back straight away. Builds take seconds to a minute, so the API
does not hold the request open.

Poll until it finishes:

```bash
while true; do
  STATE=$(curl -s -H "Authorization: Bearer $TOKEN" $SVC/jobs/$JOB | jq -r .state)
  echo $STATE
  case $STATE in succeeded|failed) break;; esac
  sleep 2
done
```

Download it:

```bash
curl -s -H "Authorization: Bearer $TOKEN" -o paper.pdf \
     $SVC/jobs/$JOB/artifacts/index.pdf
```

If the job failed, `error` and `log_tail` say why:

```bash
curl -s -H "Authorization: Bearer $TOKEN" $SVC/jobs/$JOB | jq '{state, error, log_tail}'
```

## 5. Build a thesis chapter, and read the report

The other engine applies a faculty rulebook and returns a **check report**. That
report is the point of the service.

```bash
mkdir -p thesis/src

cat > thesis/src/01-introduction.md <<'EOF'
# Introduction

## 0-EN

A short abstract. The template lifts this out of the chapter and sets it as the
Abstract page. It is also what attaches the front matter to this chapter.

## Background

One paragraph of body text, with a citation so the reference list has an entry
to render [@example2020].
EOF

cat > thesis/src/thesis.yml <<'EOF'
author: R. E. Searcher
degree: Doctor of Philosophy
department: Department of Invented Examples
faculty: Faculty of Medicine and Health Sciences
supervisor: Prof. D. Emonstration
award-date: March 2027
EOF

cat > thesis/src/Declaration.md <<'EOF'
# Declaration

By submitting this dissertation I declare that it is my own original work.
EOF

cat > thesis/src/Acknowledgements.md <<'EOF'
# Acknowledgements

With thanks to everyone who read a draft.
EOF

cat > thesis/src/references.bib <<'EOF'
@article{example2020,
  title   = {An example work},
  author  = {Searcher, R. E.},
  journal = {Journal of Examples},
  year    = {2020},
  doi     = {10.0000/example}
}
EOF

(cd thesis && zip -qr ../thesis.zip .)
```

```bash
JOB=$(curl -s -X POST $SVC/compile/archive \
        -H "Authorization: Bearer $TOKEN" \
        -F bundle=@thesis.zip \
        -F profile=su-fmhs \
        -F copy=examination | jq -r .id)

while true; do
  STATE=$(curl -s -H "Authorization: Bearer $TOKEN" $SVC/jobs/$JOB | jq -r .state)
  case $STATE in succeeded|failed) break;; esac
  sleep 2
done

curl -s -H "Authorization: Bearer $TOKEN" $SVC/jobs/$JOB \
  | jq '{state, compliant, passed: .checks.passed, failed: .checks.failed, failures: .checks.failures}'
```

```json
{
  "state": "succeeded",
  "compliant": false,
  "passed": 19,
  "failed": 1,
  "failures": ["L3  the reference list looks truncated (1 DOIs after the heading)"]
}
```

**`succeeded` does not mean compliant.** The document built, and it also failed a
check. A job only *fails* when no document came out at all. Keep the two separate
if you show build status to a user.

The `L3` failure is expected. It checks that a full thesis has a real reference
list, and a one-chapter example cannot have one. Adding fake references to silence
it would be writing to the test.

`thesis.yml` sets the title page. Leave it out and you get the template's
placeholders, which check `C1` fails on.

## 6. Your token's scopes

A token carries some of three scopes. Call `/profiles` and look at the response,
or just try the call — a missing scope gives a clear `403`.

| scope | what it opens |
|---|---|
| `read` | `/profiles` `/toolchain` `/fonts` `/jobs` `/jobs/{id}` artefacts `/equivalence` |
| `compile` | `/compile` `/compile/archive` `/compile/manuscript` |
| `deliver` | `/jobs/{id}/deliver` `/delivery/destinations` |

## 7. Errors

| code | meaning | retry? |
|---|---|---|
| `401` | no token, or one the deployment does not know | no — fix the token |
| `403` | token is valid but lacks the scope for this call | no — ask for a wider token |
| `429` | too many builds in progress | yes, after `Retry-After` |
| `410` | the job existed; its files have expired | no — build again |
| `404` on a job | not your job, or never existed | no |
| `400` | your bundle or settings were refused; the message says what | no — fix the request |
| `503` | the deployment has no tokens configured | no — tell the operator |

Two of these catch people out:

**`404` on a job id someone gave you.** You only see jobs your own token created.
Another token's job is a `404`. Do not pass job ids between people.

**`429` means wait.** The service renders two documents at a time, with a queue of
eight shared by everyone. Honour `Retry-After`. A client that retries straight away
fills the queue and blocks other people.

## 8. Delivery

Instead of downloading, the service can put the file somewhere for you:

```bash
curl -s -X POST $SVC/jobs/$JOB/deliver \
  -H "Authorization: Bearer $TOKEN" \
  -F artifact=thesis-assembled.pdf \
  -F destination=downloads \
  -F filename=draft.pdf
```

Check what the deployment supports first, so you find out now rather than after a
build:

```bash
curl -s -H "Authorization: Bearer $TOKEN" $SVC/delivery/destinations | jq
```

Both destinations are scoped to your token. `downloads` writes to a folder named
after your token. A `minio` key must start with `users/<your token label>/`.

A key outside your prefix is refused, not moved. So the URI you get back is
always the one you asked for.

Two things to know before you build this into a UI:

- **`downloads` needs the operator to have mounted a directory.** If they have not,
  the file lands inside the container and is lost on restart.
  `/delivery/destinations` shows the path. Ask whether it is a real mount.
- **PDF password protection is not implemented.** A delivered file is not
  protected.

## 9. Files expire, jobs do not

Artefacts are deleted six hours after a build. The job still returns its state and
check report after that, but the artefact URL returns `410`. Download or deliver
what you need inside that window.

## Where to go next

| | |
|---|---|
| `docs/api-integration.md` | the full client guide |
| `docs/compile-from-a-zip.md` | the zip layouts, both engines |
| `docs/authentication.md` | tokens and scopes in detail |
| `docs/concurrency.md` | the queue, retention, limits |
| `$SVC/redoc` | the live API reference |
