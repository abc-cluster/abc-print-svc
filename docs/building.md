# Building the image

```bash
git clone https://github.com/abc-cluster/abc-print-svc.git
cd abc-print-svc
./docker/vendor-pipeline.sh
docker build --platform linux/amd64 -t abc-print-svc:local -f Containerfile .
docker run --rm -p 8080:8080 abc-print-svc:local
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
DOCKER_BUILDKIT=0 docker build --platform linux/amd64 -t abc-print-svc:local -f Containerfile .
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

## Just use the published image

Building is only necessary if you are changing the service:

```bash
docker pull ghcr.io/abc-cluster/abc-print-svc:latest
```

It is private to the `abc-cluster` org, so authenticate once:

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
