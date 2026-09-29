#!/usr/bin/env bash
# Render the demo manuscript against a running service and check the result.
#
# One command to confirm a deployment works, using fixture content that ships
# with this repository — no private material needed.
#
#   ./examples/run-demo.sh [base-url]        default: http://localhost:8080
set -euo pipefail
BASE="${1:-http://localhost:8080}"
HERE="$(cd "$(dirname "$0")" && pwd)"
DEMO="$HERE/manuscript-demo"
OUT="${DEMO_OUT:-$(mktemp -d)}"

say() { printf "%s\n" "$*"; }
die() { printf "FAIL: %s\n" "$*" >&2; exit 1; }

command -v curl >/dev/null || die "curl is required"
command -v python3 >/dev/null || die "python3 is required"

say "service: $BASE"
health=$(curl -fsS --max-time 20 "$BASE/health" 2>/dev/null) \
  || die "cannot reach $BASE/health — is the service running?"
python3 - "$health" <<'PY'
import json, sys
d = json.loads(sys.argv[1])
print(f"  health ok={d['ok']}  engines={d.get('engines_available')}")
if not d["ok"]:
    print("  problems:", d["problems"]); sys.exit(1)
PY

say "submitting the demo manuscript…"
job=$(curl -fsS -X POST "$BASE/compile/manuscript" \
  -F "sources=@$DEMO/index.qmd" \
  -F "sources=@$DEMO/_metadata.yml" \
  -F "bibliography=@$DEMO/references.bib" \
  -F "figures=@$DEMO/figures/blocks.svg;filename=figures/blocks.svg" \
  -F "quarto_metadata=<$DEMO/quarto-metadata.yaml" \
  -F "profile=biorxiv-dev" -F "outputs=pdf" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['id'])")
say "  job $job"

for _ in $(seq 1 120); do
  state=$(curl -fsS "$BASE/jobs/$job" | python3 -c "import json,sys; print(json.load(sys.stdin)['state'])")
  case "$state" in succeeded|failed) break ;; esac
  sleep 2
done

curl -fsS "$BASE/jobs/$job" > "$OUT/job.json"
python3 - "$OUT/job.json" <<'PY'
import json, sys, re
d = json.load(open(sys.argv[1]))
print(f"  state={d['state']}  {d['duration_s']}s  artifacts={[a['name'] for a in d['artifacts']]}")
if d["state"] != "succeeded":
    strip = lambda s: re.sub(r"\x1b\[[0-9;]*m", "", s)
    for line in d.get("log_tail", [])[-15:]:
        print("   ", strip(line)[:160])
    sys.exit(1)
PY

curl -fsS -o "$OUT/demo.pdf" "$BASE/jobs/$job/artifacts/index.pdf"
say "  wrote $OUT/demo.pdf"

# Content checks. Rendering is not enough: a build can succeed while silently
# dropping citations or a figure, which is exactly what a fixture should catch.
if command -v pdftotext >/dev/null 2>&1; then
  pdftotext -layout "$OUT/demo.pdf" "$OUT/demo.txt"
  python3 - "$OUT/demo.txt" <<'PY'
import sys
t = open(sys.argv[1]).read()
checks = [
    ("citations resolved",        "?@" not in t),
    ("bibliography rendered",     "Journal of Fabricated Results" in t),
    ("figure reference resolved", "Figure 1" in t),
    ("table reference resolved",  "Table 1" in t),
    ("figure actually drawn",     "Blocks after sorting" in t),
]
bad = [n for n, ok in checks if not ok]
for n, ok in checks:
    print(f"  {'ok  ' if ok else 'FAIL'} {n}")
sys.exit(1 if bad else 0)
PY
else
  say "  (pdftotext not installed locally — skipped content checks)"
fi

say "demo passed"
