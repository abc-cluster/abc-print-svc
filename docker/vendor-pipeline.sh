#!/usr/bin/env bash
# Prepare the build context.
#
# Two parts, and only one of them is required:
#
#   profiles-src/*     profiles authored HERE. Always available in a clone.
#                      biorxiv-dev lives here, so the quarto-render engine works
#                      for anyone.
#   the thesis pipeline + SU/FMHS profile, which still live in the dissertation
#                      repo. OPTIONAL: without them the image simply cannot run
#                      the thesis-assemble engine, and says so at /health.
#
# This used to exit 1 when the dissertation repo was absent, which made the
# documented first build step fail for everyone who is not its author.
set -euo pipefail
SRC_REPO="${1:-${ABCPRINT_THESIS_REPO:-$HOME/projects/PHD-dissertation}}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

mkdir -p "$ROOT/pipeline" "$ROOT/profiles"

# --- profiles authored in this repo (always) --------------------------------
for d in "$ROOT"/profiles-src/*/; do
  [ -d "$d" ] || continue
  rsync -a --delete "$d" "$ROOT/profiles/$(basename "$d")"/
  echo "vendored profile $(basename "$d")"
done

# --- the thesis pipeline + profile (only if the source repo is present) -----
BIN_SRC="$SRC_REPO/writeup/bin"
PROFILE_SRC="$SRC_REPO/writeup/thesis-quarto"

if [ -d "$BIN_SRC" ] && [ -d "$PROFILE_SRC" ]; then
  rsync -a --delete --exclude '__pycache__' "$BIN_SRC"/ "$ROOT/pipeline"/
  mkdir -p "$ROOT/profiles/su-fmhs"
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
  echo "vendored $(find "$ROOT/pipeline" -type f | wc -l | tr -d ' ') pipeline files (thesis engine enabled)"
else
  # A placeholder keeps `COPY pipeline/` valid; the service detects the absence
  # and reports thesis-assemble as unavailable rather than failing a job later.
  printf '%s\n' \
    "The thesis pipeline was not vendored into this image." \
    "" \
    "It lives in a separate repository. Without it the thesis-assemble engine is" \
    "unavailable and POST /compile returns 503; the quarto-render engine and every" \
    "other endpoint work normally. See docs/building.md." \
    > "$ROOT/pipeline/NOT-VENDORED.txt"
  echo "NOTE: no thesis pipeline at $BIN_SRC"
  echo "      building WITHOUT the thesis-assemble engine (quarto-render still works)."
  echo "      Set ABCPRINT_THESIS_REPO, or pass the repo path, to include it."
fi
