# Authentication

Every endpoint except `/health` and the documentation pages requires a bearer
token:

```bash
curl -s -H "Authorization: Bearer abcp_…" $SVC/profiles
```

## Why this exists, and what it had to fix

Until 2026-10-19 the service had no authentication. It was reachable on the
public internet, `GET /jobs` enumerated **every caller's** jobs, and every
artefact was then fetchable by job id — so one caller could download another's
compiled document. That was verified, not theorised: a third party's PDF was
pulled from the public URL with no credentials.

The cause was a belief written into `abc-app.yaml` and never tested — that
`access: team` auth-gated the app, which is why `ABCPRINT_CORS_ORIGINS` was left
at `*`. It does not gate API calls. CORS was never the control either: it
constrains browsers, not `curl`.

So a shared secret would not have been enough. It would have closed the front
door and left the enumeration intact for anyone holding it. **Jobs are therefore
owned**: a token sees only the jobs it created, and another token's job is a
`404` rather than a `403`, so the response does not confirm the id exists.

## Minting a token

```bash
python -m abcprint.tokens new jorge-logseq --scopes compile,read
```

It prints two things: the **token**, shown once and never recoverable, and the
**config entry**, which holds only its SHA-256 hash. Give the holder the token.
Put the entry in the environment.

```
jorge-logseq:compile,read:sha256:ddbcf978…
```

Several entries are separated by newlines or `|`. A bare `sha256:<hex>` is also
accepted — one token, every scope — because the variable is named in the
singular and one token is the common case.

## Configuring it

```bash
ABC_PRINT_SVC_TOKEN='jorge-logseq:compile,read:sha256:ddbcf978…'
```

On the abc-cluster platform it goes in `abc-app.yaml` under `env:`, **not**
through `abc app config` — the platform reserves `ABC_*` for its own injected
variables and refuses to set one:

```
"ABC_PRINT_SVC_TOKEN" is a platform-injected variable and cannot be set/unset
```

The descriptor still delivers it to the job, so the only consequence is that
**rotation is a commit plus a deploy**, not a single CLI call — and a revoked
token keeps working until that deploy lands. Committing the hashes is acceptable
(a SHA-256 of a 32-byte urlsafe token is not brute-forceable); committing a
token would not be.

```bash
ABC_CLI_CONTEXT=<ctx> abc app deploy --node-pool platform
```

Keep the plaintexts somewhere outside the repo — `~/.abc/print-svc-tokens.txt`,
mode 600, is what this deployment uses. Nothing can recover them from the hashes.

Check what a deployment accepts, without revealing anything:

```bash
python -m abcprint.tokens check
curl -s $SVC/health | jq .auth      # {"mode": "token", "tokens": 2}
```

## Scopes

| scope | what it opens |
|---|---|
| `compile` | `POST /compile`, `/compile/archive`, `/compile/manuscript` |
| `read` | `GET /jobs`, `/jobs/{id}`, artefacts, `/profiles`, `/toolchain`, `/fonts`, `POST /equivalence` |
| `deliver` | `POST /jobs/{id}/deliver`, `GET /delivery/destinations` |

A token without a scope gets **403** naming the missing one; an absent or
unrecognised token gets **401**. The distinction is the difference between "ask
for a wider token" and "fix your token", and a caller cannot act on the right one
if both look the same.

Give a plugin `compile,read` and nothing else. `deliver` writes to an object
store, and a plugin that only needs to compile and poll has no business holding
it.

## Three deliberate behaviours

**No tokens configured means every request is refused** (503, naming the
variable), not allowed. The original defect was a service that failed open; this
one fails closed and says why.

**A malformed `ABC_PRINT_SVC_TOKEN` is fatal at startup.** An entry quietly
skipped for a typo is a token that stops working with no explanation — or, if it
were the only one, a service that comes up accepting nobody while looking
healthy.

**`/health` is open and reports the gate.** The Nomad check needs it, and it
carries no document data. It states `auth.mode`, so a deployment running without
authentication is *visible* rather than assumed — which is precisely what went
wrong the first time.

## Turning it off for local work

```bash
ABC_PRINT_SVC_AUTH=disabled
```

Every caller becomes `anonymous` and shares one job view. `/health` then carries
a `warning` saying so. Never set this on a reachable deployment.

## What this is not

It is not an identity system. A token is a bearer credential with a label: there
are no users, no expiry and no refresh. Revocation means removing the entry and
redeploying, which on this platform is a commit — so a compromised token is live
until that lands. Rate limiting, per-token quotas and PDF password
protection are all separate pieces of work and are not pretended at here.
