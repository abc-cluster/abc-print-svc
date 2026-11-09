#!/usr/bin/env bash
# Put abc-dispatch into the build context as a wheel, from a pinned tag.
#
# abc-dispatch is a private repository. Installing it from git inside the image
# build would need an SSH key or a token in the build — which ends up in a layer
# or a build log. Instead the wheel is built HERE, from a local clone, at an exact
# tag, and the image installs it with --no-index. The tag and the commit it
# resolved to are recorded beside it, and the Containerfile refuses to build
# without that record.
#
#   docker/vendor-dispatch.sh            # tag from DISPATCH_TAG below
#   docker/vendor-dispatch.sh v0.4.0 ~/projects/abc-dispatch
set -euo pipefail
TAG="${1:-${DISPATCH_TAG:-v0.4.0}}"
SRC="${2:-${ABC_DISPATCH_REPO:-$HOME/projects/abc-dispatch}}"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OUT="$ROOT/vendor-wheels"

git -C "$SRC" rev-parse --verify --quiet "refs/tags/$TAG" >/dev/null \
  || { echo "FATAL: $SRC has no tag $TAG (git fetch --tags?)" >&2; exit 1; }
COMMIT="$(git -C "$SRC" rev-parse "$TAG^{commit}")"

STAGE="$(mktemp -d "${TMPDIR:-/tmp}/abcprint-dispatch.XXXXXX")"
trap 'rm -rf "$STAGE"' EXIT
# From the tag, not the working tree: uncommitted changes in the clone must not
# reach an image.
git -C "$SRC" archive --format=tar "$TAG" | tar -x -C "$STAGE"
mkdir -p "$STAGE/wheels"
python3 -m pip wheel -q --no-deps -w "$STAGE/wheels" "$STAGE"

rm -rf "$OUT" && mkdir -p "$OUT"
cp "$STAGE"/wheels/abc_dispatch-*.whl "$OUT"/
printf '{"package": "abc-dispatch", "tag": "%s", "commit": "%s"}\n' "$TAG" "$COMMIT" \
  > "$OUT/DISPATCH.json"
echo "vendored abc-dispatch $TAG ($COMMIT): $(ls "$OUT"/*.whl | xargs -n1 basename)"
