#!/usr/bin/env bash
# Copy the thesis pipeline scripts into the build context.
#
# They live in the dissertation repo today; extracting them into this one is
# slice 2's work. Until then the image vendors them at build time so the
# container has them baked in, and this script is the single place that knows
# where they came from.
set -euo pipefail
SRC="${1:-$HOME/projects/PHD-dissertation/writeup/bin}"
DEST="$(cd "$(dirname "$0")/.." && pwd)/pipeline"
[ -d "$SRC" ] || { echo "vendor-pipeline: no such source dir: $SRC" >&2; exit 1; }
mkdir -p "$DEST"
rsync -a --delete --exclude '__pycache__' "$SRC"/ "$DEST"/
echo "vendored $(ls -1 "$DEST" | wc -l | tr -d ' ') files from $SRC -> $DEST"
