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
  # Everything is assembled in a STAGING directory and swapped into place only
  # after sanitising and verification have passed.
  #
  # This used to rsync --delete straight into pipeline/ and profiles/su-fmhs/,
  # sanitise, and write PROVENANCE.json last. So a failure anywhere in between
  # left the build context half-vendored: pipeline scripts present, old
  # provenance already deleted, new provenance never written, and — the part that
  # mattered — whatever the sanitiser had just REFUSED still sitting in the
  # profile. On 2026-11-08 that is exactly what shipped: the sanitiser exited
  # "FATAL: personal content survived sanitisation" over render logs carrying the
  # author's private acknowledgements, the exit code was lost to a `| tail`, and
  # 0.13.0 and 0.14.0 were built from the rejected context and published.
  #
  # Staging outside the repo means a failed run leaves the previous good context
  # untouched, and leaves no private material anywhere in the working tree.
  STAGE="$(mktemp -d "${TMPDIR:-/tmp}/abcprint-vendor.XXXXXX")"
  trap 'rm -rf "$STAGE"' EXIT
  mkdir -p "$STAGE/pipeline" "$STAGE/su-fmhs"

  rsync -a --exclude '__pycache__' --exclude '*.log' "$BIN_SRC"/ "$STAGE/pipeline"/
  # Build artefacts are excluded by PATTERN, not by the one extension that
  # happened to exist when this was written. `subuild*` covers every file a
  # render writes beside the profile — .typ, .qmd, .log and *_files/ — and *.log
  # catches any other log. The render log is the one that leaked: it echoes the
  # document's metadata, which includes the front matter.
  rsync -a \
    --exclude '_output/' --exclude '.quarto/' --exclude 'subuild*' \
    --exclude '*.log' --exclude '*.pdf' --exclude '__pycache__' \
    "$PROFILE_SRC"/ "$STAGE/su-fmhs"/
  [ -d "$STAGE/su-fmhs/_extensions/sun-thesis" ] \
    || { echo "vendor: sun-thesis extension missing from the profile" >&2; exit 1; }

  # Extend the typst font stacks with libre equivalents. Without this the image
  # ships Carlito but the template only ever asks for Calibri, so typst falls off
  # the end of the stack and renders in its own default — a different font
  # entirely, with no error anywhere.
  python3 "$ROOT/docker/patch-profile-fonts.py" "$STAGE/su-fmhs"

  # Replace the author's own thesis material with fixture content, and FAIL if
  # anything personal survives. Under `set -e` a failure here exits the script
  # with the staging directory discarded and the live build context unchanged.
  python3 "$ROOT/docker/sanitise-profile.py" \
    "$STAGE/su-fmhs" "$ROOT/examples/thesis-chapter-demo/frontmatter" "$STAGE/pipeline"

  # Record WHICH revision was vendored. The pipeline and the profile live in
  # another repository and change independently of this one — on 2026-10-07 a
  # check broadened under us, so a document built before and after that day was
  # verified against different rules. Without this the manifest can name every
  # tool version and still not say which rulebook ran.
  rev="$(git -C "$SRC_REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)"
  when="$(git -C "$SRC_REPO" log -1 --format=%cI 2>/dev/null || echo unknown)"
  dirty="false"; [ -n "$(git -C "$SRC_REPO" status --porcelain -- writeup/bin writeup/thesis-quarto 2>/dev/null)" ] && dirty="true"
  cat > "$STAGE/pipeline/PROVENANCE.json" <<JSON
{
  "source": "thesis pipeline (separate repository)",
  "revision": "$rev",
  "revision_date": "$when",
  "uncommitted_changes": $dirty,
  "vendored_at": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
  "files": $(find "$STAGE/pipeline" -type f ! -name PROVENANCE.json | wc -l | tr -d ' ')
}
JSON
  cp "$STAGE/pipeline/PROVENANCE.json" "$STAGE/su-fmhs/PROVENANCE.json"

  # Swap in only now that every step above has passed.
  rm -rf "$ROOT/pipeline" "$ROOT/profiles/su-fmhs"
  mkdir -p "$ROOT/profiles"
  mv "$STAGE/pipeline" "$ROOT/pipeline"
  mv "$STAGE/su-fmhs" "$ROOT/profiles/su-fmhs"
  echo "vendored $(find "$ROOT/pipeline" -type f | wc -l | tr -d ' ') pipeline files (thesis engine enabled, rev $rev${dirty:+, dirty=$dirty})"
  echo "VENDOR OK"
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
