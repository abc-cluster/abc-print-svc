#!/usr/bin/env bash
# Publish a profile bundle into the shared-volume store, through the running front
# door (it mounts the volume and carries the publish tool and the worker's
# capability record, so `requires` is checked against the real worker).
#
#   NOMAD_CMD="abc admin services cli nomad --" nomad/publish-profile.sh <bundle-dir> [--promote]
#   NOMAD_CMD=… nomad/publish-profile.sh --sign-only <name>@<version>
#   NOMAD_CMD=… nomad/publish-profile.sh --promote-only <name>@<version>
#   NOMAD_CMD=… nomad/publish-profile.sh --list
#
# Signing (0.18.0). With ABCPRINT_SIGNING_KEY=<Ed25519 PEM private key> the bundle
# is signed HERE, on the operator's machine: the front door computes the exact
# statement (name@version and content digest), openssl signs it locally, and only
# the signature travels. The private key never reaches the cluster. Make a key with
#   openssl genpkey -algorithm ed25519 -out ~/.abc/print-svc-profile-signing.pem
# and give the service its public half (ABCPRINT_PROFILE_TRUSTED_KEYS):
#   openssl pkey -in ~/.abc/print-svc-profile-signing.pem -pubout -outform DER | tail -c 32 | base64
#
# The bundle is streamed in as a tar; nothing is left in the container afterwards.
set -euo pipefail
read -r -a NOMAD <<< "${NOMAD_CMD:-nomad}"
NAME="${ABCPRINT_JOB_NAME:-app-mbhg-bioinformatics-abc-print-svc}"
SRC="file:///srv/dispatch/abc-print-svc/profile-store"
KEY="${ABCPRINT_SIGNING_KEY:-}"
alloc=$("${NOMAD[@]}" job allocs -namespace abc-apps "$NAME" | awk '$6=="running"{print $1; exit}')
[ -n "$alloc" ] || { echo "FATAL: no running allocation of $NAME" >&2; exit 1; }
ex() { "${NOMAD[@]}" alloc exec -namespace abc-apps -task app "$@"; }
# Run `python3 -m abcprint.publish <args>` in the front door over the streamed bundle.
in_door() { local dir="$1"; shift
  tar -C "$dir" -cz . | ex -i "$alloc" sh -c \
    't=$(mktemp -d); trap "rm -rf $t" EXIT; mkdir "$t/b"; tar -xz -C "$t/b" && python3 -m abcprint.publish '"$*"; }

sign() {  # statement file -> "--signature <b64> --key-id <id>"
  local sig kid
  sig=$(openssl pkeyutl -sign -inkey "$KEY" -rawin -in "$1" | base64 | tr -d '\n')
  kid=$(openssl pkey -in "$KEY" -pubout -outform DER | tail -c 32 | shasum -a 256 | cut -c1-16)
  [ -n "$sig" ] && [ -n "$kid" ] || { echo "FATAL: signing failed" >&2; exit 1; }
  printf -- '--signature %s --key-id %s' "$sig" "$kid"
}

stmt="$(mktemp)"; trap 'rm -f "$stmt"' EXIT
case "${1:-}" in
  --list)         ex "$alloc" python3 -m abcprint.publish list --source "$SRC" ;;
  --promote-only) ex "$alloc" python3 -m abcprint.publish promote "${2:?name@version}" --source "$SRC" ;;
  --sign-only)
    [ -n "$KEY" ] || { echo "FATAL: set ABCPRINT_SIGNING_KEY" >&2; exit 1; }
    ex "$alloc" python3 -m abcprint.publish statement "${2:?name@version}" > "$stmt"
    grep -q '^abc-print-profile-v1 ' "$stmt" || { cat "$stmt" >&2; exit 1; }
    # shellcheck disable=SC2046
    ex "$alloc" python3 -m abcprint.publish sign "$2" $(sign "$stmt") --source "$SRC" ;;
  ""|-*)          echo "usage: $0 <bundle-dir> [--promote] | --sign-only name@version | --promote-only name@version | --list" >&2; exit 2 ;;
  *)
    dir="$1"; shift
    [ -f "$dir/profile.yaml" ] || { echo "FATAL: $dir has no profile.yaml" >&2; exit 1; }
    sigargs=""
    if [ -n "$KEY" ]; then
      in_door "$dir" 'statement "$t/b"' > "$stmt"
      grep -q '^abc-print-profile-v1 ' "$stmt" || { cat "$stmt" >&2; exit 1; }
      echo "signing: $(cat "$stmt")"
      sigargs="$(sign "$stmt")"
    fi
    in_door "$dir" 'publish "$t/b" --source '"$SRC $sigargs $*"
    ;;
esac
