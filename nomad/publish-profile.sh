#!/usr/bin/env bash
# Publish a profile bundle into the shared-volume store, through the running front
# door (it mounts the volume and carries the publish tool and the worker's
# capability record, so `requires` is checked against the real worker).
#
#   NOMAD_CMD="abc admin services cli nomad --" nomad/publish-profile.sh <bundle-dir> [--promote]
#   NOMAD_CMD=… nomad/publish-profile.sh --promote-only <name>@<version>
#   NOMAD_CMD=… nomad/publish-profile.sh --list
#
# The bundle is streamed in as a tar; nothing is left in the container afterwards.
set -euo pipefail
read -r -a NOMAD <<< "${NOMAD_CMD:-nomad}"
NAME="${ABCPRINT_JOB_NAME:-app-mbhg-bioinformatics-abc-print-svc}"
SRC="file:///srv/dispatch/abc-print-svc/profile-store"
alloc=$("${NOMAD[@]}" job allocs -namespace abc-apps "$NAME" | awk '$6=="running"{print $1; exit}')
[ -n "$alloc" ] || { echo "FATAL: no running allocation of $NAME" >&2; exit 1; }
ex() { "${NOMAD[@]}" alloc exec -namespace abc-apps -task app "$@"; }

case "${1:-}" in
  --list)         ex "$alloc" python3 -m abcprint.publish list --source "$SRC" ;;
  --promote-only) ex "$alloc" python3 -m abcprint.publish promote "${2:?name@version}" --source "$SRC" ;;
  ""|-*)          echo "usage: $0 <bundle-dir> [--promote] | --promote-only name@version | --list" >&2; exit 2 ;;
  *)
    dir="$1"; shift
    [ -f "$dir/profile.yaml" ] || { echo "FATAL: $dir has no profile.yaml" >&2; exit 1; }
    tar -C "$dir" -cz . | ex -i "$alloc" sh -c \
      'set -e; t=$(mktemp -d); trap "rm -rf $t" EXIT; mkdir "$t/b"; tar -xz -C "$t/b"; python3 -m abcprint.publish publish "$t/b" --source '"$SRC"' '"$*"
    ;;
esac
