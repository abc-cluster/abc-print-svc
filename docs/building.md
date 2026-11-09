# Building the images

```bash
git clone https://github.com/abc-cluster/abc-print-svc.git
cd abc-print-svc
./docker/vendor-pipeline.sh        # optional: the thesis engine
./docker/vendor-dispatch.sh        # abc-dispatch, from its pinned tag
docker build --target worker -t abc-print-worker:local -f Containerfile .
docker build --target api    -t abc-print-svc:local    -f Containerfile .
```

Two images from one file, built together:

- **`worker`** — quarto, typst, fonts, profiles, the thesis pipeline. A build runs
  in it, one Nomad job per run. Its build measures what it can do into
  `/srv/capabilities.json`.
- **`api`** — the front door. Copies that record and the doc assets from the
  worker stage of the same build, and nothing else of it: no profile, pipeline or
  toolchain. Its build fails if any is present.

`vendor-dispatch.sh` needs a local clone of the private `abc-cluster/abc-dispatch`
(default `~/projects/abc-dispatch`); it builds the wheel from the tag, not from the
working tree, and records the commit beside it.

To run everything on one machine without Nomad, serve the API from the worker
image with the in-process executor:

```bash
docker run --rm -p 8080:8080 -e ABC_PRINT_SVC_AUTH=disabled abc-print-worker:local \
  python3 -m uvicorn --factory abcprint.service:build --host 0.0.0.0 --port 8080
```

That is the whole thing. It works from a clean clone with nothing else on disk.

## If you do not need the thesis engine, nothing is missing

`vendor-pipeline.sh` prepares the build context. It always vendors the profiles
authored in this repo — which is how **`biorxiv-dev` and the `quarto-render`
engine** get in — and then *optionally* adds the thesis pipeline if it can find
it.

The thesis pipeline and the SU/FMHS profile still live in a **separate,
private repository**. If you do not have it, the script prints:

```
NOTE: no thesis pipeline at /…/PHD-dissertation/writeup/bin
      building WITHOUT the thesis-assemble engine (quarto-render still works).
```

and continues. The resulting image runs everything except `POST /compile`, which
returns **503** naming the reason rather than failing a job thirty seconds in.

Check what an image can do:

```bash
curl -s localhost:8080/health | jq .engines_available
# { "quarto-render": true, "thesis-assemble": false }
```

> An earlier version of this script **exited 1** when that repository was absent,
> which made the documented first build step fail for everyone who is not its
> author. If you hit that, you have an old checkout.

To include the thesis engine, point the script at the repo:

```bash
./docker/vendor-pipeline.sh /path/to/PHD-dissertation
# or: export ABCPRINT_THESIS_REPO=/path/to/PHD-dissertation
```

## Troubleshooting

**The build hangs after `load build context`.** Some Docker Desktop / BuildKit
combinations stall there. Use the legacy builder:

```bash
DOCKER_BUILDKIT=0 docker build -t abc-print-svc:local -f Containerfile .
```

**`COPY pipeline/ … : not found`.** You skipped `vendor-pipeline.sh`. It creates
that directory, with a placeholder when the thesis repo is absent.

**Font or documentation downloads fail the build.** Deliberate. An earlier
revision swallowed them with `|| echo NOTE` and produced an image that built
"successfully" with a font missing, which then rendered in a different face with
no error anywhere. If a download fails, the build stops. Re-run it; these are
network fetches from GitHub, Google Fonts and two CDNs.

**`pip install` fails with `from versions: none`.** A transient index failure,
not a version conflict. Re-run.

## Confirm it works

```bash
./examples/run-demo.sh
```

Fixture content ships in `examples/`, so this needs nothing private. It checks
health, renders the demo manuscript, and verifies the output actually contains
its citations, cross-references and figure.

## Just use the published images

Building is only necessary if you are changing the service:

```bash
docker pull ghcr.io/abc-cluster/abc-print-svc:0.15.0      # the front door (public)
docker pull ghcr.io/abc-cluster/abc-print-worker:0.15.0   # the worker
```

0.15.0 is published for `linux/amd64`. Releases up to 0.14.1 were multi-arch
(`linux/amd64` and `linux/arm64`).

The worker package is not public, so authenticate once:

```bash
echo "$GITHUB_TOKEN" | docker login ghcr.io -u <your-github-username> --password-stdin
```

The token needs `read:packages`. Repo access governs pull access, so anyone who
can read the repository can pull the image.

## CORS — needed for a browser or editor plugin

A plugin runs in a browser context, so without CORS every call is blocked before
it reaches the service. The request never appears in the log, which makes this
look like the service being down rather than refusing.

The default allows **any** origin, so a plugin works out of the box:

```bash
curl -s localhost:8080/health | jq .cors_allowed_origins   # ["*"]
```

Restrict it for anything not on a laptop:

```bash
docker run --rm -p 8080:8080 \
  -e ABCPRINT_CORS_ORIGINS="app://logseq.com,http://localhost:3000" \
  ghcr.io/abc-cluster/abc-print-svc:latest
```

> The permissive default exists because the service has **no authentication**. On
> a laptop that is a reasonable trade; exposed beyond one, `*` means any page the
> user visits can drive the service. Set an explicit list.

## Runtime options

| variable | default | purpose |
|---|---|---|
| `ABCPRINT_CORS_ORIGINS` | `*` | comma-separated allowed origins |
| `ABCPRINT_DOWNLOADS` | `/srv/downloads` | mount a host dir to enable `downloads` delivery |
| `ABCPRINT_S3_ENDPOINT` + `AWS_ACCESS_KEY_ID` + `AWS_SECRET_ACCESS_KEY` | unset | enable `minio` delivery |
| `ABCPRINT_FONT_PATHS` | libre set in the image | mount licensed fonts; they win automatically |
| `ABCPRINT_BUILD_TIMEOUT` | `1800` | seconds before a render is killed |
| `ABCPRINT_SOURCE_DATE_EPOCH` | `1700000000` | pinned so timestamps do not vary per build |

A typical local run with delivery enabled:

```bash
docker run --rm -p 8080:8080 \
  -v "$HOME/Downloads":/srv/downloads \
  ghcr.io/abc-cluster/abc-print-svc:latest
```

### Front door (`api`) only

| variable | default | purpose |
|---|---|---|
| `ABCPRINT_EXECUTOR` | `in-process` | `nomad` to dispatch each run; `in-process` only in the worker image |
| `ABCPRINT_NOMAD_ADDR` | — | the Nomad HTTP API, as reached from the task |
| `ABCPRINT_NOMAD_TOKEN_FILE` | — | the credential; on the cluster, the task's workload identity |
| `ABCPRINT_DISPATCH_NAMESPACE` · `ABCPRINT_DISPATCH_JOB` | `svc-print` · `abc-print-run` | where the workers are |
| `ABCPRINT_DISPATCH_ROOT` | `/srv/dispatch/runs` | run directories, on the volume shared with the workers at the same path |
| `ABCPRINT_DATA_DIR` | `/srv/dispatch/state` | the run store (SQLite) and uploads |
| `ABCPRINT_JURISDICTION` | — | stamped on every dispatch |
| `ABCPRINT_MAX_RENDERS` · `ABCPRINT_MAX_QUEUED` | `2` · `8` | how many build at once, how many may wait |
| `ABCPRINT_ARTIFACT_TTL_S` | `21600` | how long outputs are kept |
| `ABCPRINT_TRUSTED_PROXIES` | unset | CIDRs whose `X-Forwarded-For` is believed, for rate limits |

## Deploying on Nomad

Three objects, all in `nomad/`:

```bash
# 1. the workers' namespace, which no member policy grants anything in
nomad namespace apply svc-print
# 2. the worker: a parameterized job the front door dispatches, one child per run
nomad job run -var image=ghcr.io/abc-cluster/abc-print-worker:<v> nomad/abc-print-run.nomad.hcl
# 3. the front door, and what its workload identity may do
nomad job run -var image=ghcr.io/abc-cluster/abc-print-svc:<v> \
              -var tokens="<ABC_PRINT_SVC_TOKEN hashes>" nomad/abc-print-svc.nomad.hcl
nomad acl policy apply -namespace abc-apps -job app-mbhg-bioinformatics-abc-print-svc \
              abc-print-svc-dispatch nomad/front-door-dispatch.policy.hcl
```

The front door and the workers mount the same host volume (`scratch`) at the same
path, because a run's directory is passed to the worker as an absolute path. The
front door's prestart task creates its directory there, owned by the UID both
images run as. `abc app deploy` is not used: it cannot mount a host volume.

## Testing dispatch locally

`nomad/dev-agent.hcl` is a throwaway agent with ACLs on, the docker driver and a
`scratch` volume, so the front door's workload-identity policy is what authorises
its dispatches — as on the cluster:

```bash
nomad agent -dev -config nomad/dev-agent.hcl -data-dir "$(mktemp -d)" &
export NOMAD_ADDR=http://127.0.0.1:14646
export NOMAD_TOKEN=$(nomad acl bootstrap -json | jq -r .SecretID)
nomad namespace apply svc-print && nomad namespace apply abc-apps
nomad job run -var image=abc-print-worker:local -var node_pool=default \
  -var 'datacenters=["dc1"]' nomad/abc-print-run.nomad.hcl
nomad job run -var image=abc-print-svc:local -var node_pool=default -var 'datacenters=["dc1"]' \
  -var nomad_addr=http://host.docker.internal:14646 -var tokens="$(cat my-token-entry)" \
  nomad/abc-print-svc.nomad.hcl
nomad acl policy apply -namespace abc-apps -job app-mbhg-bioinformatics-abc-print-svc \
  abc-print-svc-dispatch nomad/front-door-dispatch.policy.hcl
```
