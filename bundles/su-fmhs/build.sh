#!/usr/bin/env bash
# Build the su-fmhs profile bundle from the thesis repository, ready to publish.
#
#   bundles/su-fmhs/build.sh <thesis-repo> <revision> <version> <out-dir> [alias ...]
#   bundles/su-fmhs/build.sh ~/projects/PHD-dissertation d0e8657 1.0.0 /tmp/su-fmhs-1.0.0 git-d0e8657 unversioned
#
# Until 0.17.0 this was the image build's vendor step (docker/vendor-pipeline.sh):
# the template and the pipeline were copied into the worker image. Since 0.18.0
# they are a profile bundle published to the store, so improving the rulebook is
# a publish, not a rebuild, and the worker image carries nothing private at all.
#
# What it does, in a staging directory that is discarded on any failure:
#   1. takes writeup/thesis-quarto (the template) and writeup/bin (the pipeline)
#      from REVISION with `git archive` — committed content only, never the
#      working tree, so the same revision always yields the same digest;
#   2. drops build artefacts (render logs leaked private front matter on 2026-11-08);
#   3. adds the libre fallbacks to the template's font stacks;
#   4. sanitises: the author's own front matter is replaced with fixtures, and the
#      run FAILS if any personal string survives (docker/sanitise-profile.py);
#   5. writes profile.yaml, pipeline/PROVENANCE.json and examples/;
#   6. moves the result to OUT-DIR and prints its digest.
#
# Then publish it (signed) with nomad/publish-profile.sh OUT-DIR.
set -euo pipefail
REPO="${1:?thesis repository}"; REV="${2:?revision}"; VERSION="${3:?version, e.g. 1.0.0}"
OUT="${4:?output directory (must not exist)}"; shift 4
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
[ ! -e "$OUT" ] || { echo "FATAL: $OUT exists; give a new directory" >&2; exit 1; }

FULL="$(git -C "$REPO" rev-parse --verify "$REV^{commit}")"
SHORT="$(git -C "$REPO" rev-parse --short "$FULL")"
WHEN="$(git -C "$REPO" log -1 --format=%cI "$FULL")"

STAGE="$(mktemp -d "${TMPDIR:-/tmp}/su-fmhs-bundle.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
mkdir -p "$STAGE/src" "$STAGE/b"
git -C "$REPO" archive --format=tar "$FULL" writeup/bin writeup/thesis-quarto | tar -x -C "$STAGE/src"
B="$STAGE/b"

# 1–2. Template at the bundle root, pipeline under pipeline/, no build artefacts.
rsync -a --exclude '_output/' --exclude '.quarto/' --exclude 'subuild*' --exclude '*.log' \
  --exclude '*.pdf' --exclude '__pycache__' "$STAGE/src/writeup/thesis-quarto/" "$B/"
rsync -a --exclude '__pycache__' --exclude '*.log' "$STAGE/src/writeup/bin/" "$B/pipeline/"
[ -d "$B/_extensions/sun-thesis" ] || { echo "FATAL: sun-thesis extension missing at $SHORT" >&2; exit 1; }
[ -f "$B/pipeline/thesis-assemble.sh" ] || { echo "FATAL: no thesis-assemble.sh at $SHORT" >&2; exit 1; }
chmod 0755 "$B"/pipeline/*.sh "$B"/pipeline/*.py 2>/dev/null || true

# 3. Font stacks: libre equivalents after each proprietary family.
python3 "$ROOT/docker/patch-profile-fonts.py" "$B"

# 4. Sanitise; exits non-zero (and so does this script) if anything personal survives.
#    The front-matter fragments are gitignored in the thesis repo (generated per
#    build from the thesis's own Dedication.md / Acknowledgements.md), so they are
#    not in the archive. The image always shipped FIXTURE versions as the fallback
#    for a thesis without them; an empty placeholder makes the sanitiser
#    regenerate exactly those, from the fixtures, with the profile's converters.
[ -f "$B/md2dedication.py" ] && : > "$B/_dedication.typ"
[ -f "$B/md2acknowledgements.py" ] && : > "$B/_acknowledgements.typ"
python3 "$ROOT/docker/sanitise-profile.py" "$B" "$ROOT/examples/thesis-chapter-demo/frontmatter" "$B/pipeline"
find "$B" -name '__pycache__' -type d -prune -exec rm -rf {} +

# 5. Describe it. No timestamps of this run: the digest must depend on the
#    revision alone.
aliases=""
for a in "$@"; do aliases="${aliases:+$aliases, }\"$a\""; done
sed -e "s/@VERSION@/$VERSION/" -e "s/@REV@/$SHORT/" -e "s/@ALIASES@/$aliases/" \
  "$ROOT/bundles/su-fmhs/profile.yaml.in" > "$B/profile.yaml"
cat > "$B/pipeline/PROVENANCE.json" <<JSON
{
  "source": "thesis pipeline (separate repository)",
  "revision": "$SHORT",
  "revision_full": "$FULL",
  "revision_date": "$WHEN",
  "uncommitted_changes": false
}
JSON
mkdir -p "$B/examples"
cp "$ROOT/examples/garden-thesis-demo.zip" "$B/examples/garden-thesis-demo.zip"

leftovers="$(find "$B" -type f \( -name '*.log' -o -name 'subuild*' -o -name '*.pdf' \) | head -5)"
[ -z "$leftovers" ] || { echo "FATAL: build artefacts in the bundle: $leftovers" >&2; exit 1; }

# 6. Done: move into place and report.
mkdir -p "$(dirname "$OUT")"
mv "$B" "$OUT"
digest="$(cd "$ROOT/src" && python3 -c 'import sys; from abcprint.profile_store import dir_digest; print(dir_digest(sys.argv[1]))' "$OUT")"
echo "built su-fmhs@$VERSION from $SHORT ($(find "$OUT" -type f | wc -l | tr -d ' ') files) $digest"
echo "BUNDLE OK"
