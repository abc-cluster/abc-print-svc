#!/usr/bin/env bash
# Copy the pipeline scripts and the SU/FMHS profile into the build context.
#
# Both still live in the dissertation repo; extracting them into this one is
# slice 2's work. Until then the image vendors them at build time so the
# container is self-contained, and this script is the single place that knows
# where they came from — one copy, not two that drift.
set -euo pipefail
SRC_REPO="${1:-$HOME/projects/PHD-dissertation}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

BIN_SRC="$SRC_REPO/writeup/bin"
PROFILE_SRC="$SRC_REPO/writeup/thesis-quarto"
[ -d "$BIN_SRC" ]     || { echo "vendor: no pipeline at $BIN_SRC" >&2; exit 1; }
[ -d "$PROFILE_SRC" ] || { echo "vendor: no profile at $PROFILE_SRC" >&2; exit 1; }

mkdir -p "$ROOT/pipeline" "$ROOT/profiles/su-fmhs"
rsync -a --delete --exclude '__pycache__' "$BIN_SRC"/ "$ROOT/pipeline"/

# Exclude render output and quarto scratch: they are regenerated per build and
# would otherwise bloat the image and stale-cache the profile.
rsync -a --delete \
  --exclude '_output/' --exclude '.quarto/' --exclude 'subuild*.typ' \
  --exclude '*.pdf' --exclude '__pycache__' \
  "$PROFILE_SRC"/ "$ROOT/profiles/su-fmhs"/

[ -d "$ROOT/profiles/su-fmhs/_extensions/sun-thesis" ] \
  || { echo "vendor: sun-thesis extension missing from the profile" >&2; exit 1; }

# Extend the typst font stacks with libre equivalents. Without this the image
# ships Carlito but the template only ever asks for Calibri, so typst falls off
# the end of the stack and renders in its own default — a different font
# entirely, with no error anywhere.
python3 "$ROOT/docker/patch-profile-fonts.py" "$ROOT/profiles/su-fmhs"

# Profiles authored in this repo (biorxiv-dev and any future one) are copied in
# alongside the vendored SU/FMHS profile, so /srv/profiles holds the registry.
if [ -d "$ROOT/profiles-src" ]; then
  for d in "$ROOT"/profiles-src/*/; do
    [ -d "$d" ] || continue
    rsync -a --delete "$d" "$ROOT/profiles/$(basename "$d")"/
    echo "vendored profile $(basename "$d")"
  done
fi

echo "vendored $(find "$ROOT/pipeline" -type f | wc -l | tr -d ' ') pipeline files"
echo "vendored $(find "$ROOT/profiles/su-fmhs" -type f | wc -l | tr -d ' ') profile files ($(du -sh "$ROOT/profiles/su-fmhs" | cut -f1))"
