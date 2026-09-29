#!/usr/bin/env bash
# Check everything the build needs, and say precisely what is missing.
#
# A failed `docker build` prints a wall of output whose last line is rarely the
# cause. This runs first and names the problem.
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
fail=0; warn=0
ok()   { printf "  \033[32mok\033[0m    %s\n" "$1"; }
bad()  { printf "  \033[31mFAIL\033[0m  %s\n" "$1"; fail=$((fail+1)); }
note() { printf "  \033[33mnote\033[0m  %s\n" "$1"; warn=$((warn+1)); }

echo "abc-print-svc build doctor"
echo

# --- tools ------------------------------------------------------------------
for t in docker rsync curl; do
  command -v "$t" >/dev/null 2>&1 && ok "$t found" || bad "$t is NOT installed"
done
command -v python3 >/dev/null 2>&1 && ok "python3 found" \
  || note "python3 not found — only needed to build WITH the thesis pipeline"

# --- docker -----------------------------------------------------------------
if command -v docker >/dev/null 2>&1; then
  if docker info >/dev/null 2>&1; then
    ok "docker daemon reachable"
    HOST_ARCH="$(docker version --format '{{.Server.Arch}}' 2>/dev/null || echo unknown)"
    ok "docker architecture: ${HOST_ARCH}"
    # The image installs quarto and pandoc .debs per architecture. Both publish
    # amd64 and arm64, and the Containerfile now picks the right one, so a native
    # build is expected. Forcing --platform linux/amd64 on arm64 means emulation.
    case "$HOST_ARCH" in
      arm64|aarch64)
        note "on arm64 do NOT pass --platform linux/amd64; it forces qemu emulation," ;
        note "  which is slow and can fail mid-build. A native build is supported." ;;
    esac
  else
    bad "docker is installed but the daemon is not reachable — start Docker Desktop"
  fi
fi

# --- build context ----------------------------------------------------------
[ -f "$ROOT/Containerfile" ] && ok "Containerfile present" || bad "Containerfile missing — wrong directory?"
[ -d "$ROOT/src/abcprint" ]  && ok "service source present" || bad "src/abcprint missing"
if [ -d "$ROOT/profiles-src" ]; then ok "profiles-src present"; else bad "profiles-src missing"; fi

if [ -d "$ROOT/pipeline" ]; then
  if [ -f "$ROOT/pipeline/thesis-assemble.sh" ]; then
    ok "thesis pipeline vendored (thesis-assemble engine will be available)"
  else
    ok "pipeline/ placeholder present (quarto-render only — this is fine)"
  fi
else
  note "pipeline/ not prepared yet — run ./docker/vendor-pipeline.sh first"
fi
[ -d "$ROOT/profiles/biorxiv-dev" ] && ok "biorxiv-dev profile vendored" \
  || note "profiles/ not prepared yet — run ./docker/vendor-pipeline.sh first"

# --- network ----------------------------------------------------------------
# The build downloads quarto, pandoc, fonts and the doc UIs. A proxy or firewall
# that blocks any of these fails the build at a step that looks unrelated.
echo
echo "  network reachability (the build downloads these):"
# Probe the ACTUAL assets, not the bare hosts: a host can answer 403 on / while
# serving the file fine, and following redirects concatenates status codes.
for u in \
  "https://github.com/quarto-dev/quarto-cli/releases/download/v1.7.31/quarto-1.7.31-linux-amd64.deb" \
  "https://github.com/jgm/pandoc/releases/download/3.7.0.1/pandoc-3.7.0.1-1-amd64.deb" \
  "https://cdn.redoc.ly/redoc/latest/bundles/redoc.standalone.js" \
  "https://cdn.jsdelivr.net/npm/swagger-ui-dist@5/swagger-ui.css" \
  "https://deb.debian.org/debian/dists/bookworm/Release" ; do
  code=$(curl -fsSL -I -o /dev/null -m 15 -w '%{http_code}' "$u" 2>/dev/null | tail -c 3)
  short="$(echo "$u" | sed -E 's#https://([^/]+)/.*#\1#')"
  if [ -z "$code" ] || [ "$code" = "000" ]; then
    bad "  cannot fetch from $short — a proxy or firewall will fail the build"
  else
    ok "  $short ($code)"
  fi
done

echo
if [ "$fail" -gt 0 ]; then
  echo "  $fail problem(s) must be fixed before building."
  exit 1
fi
echo "  ready to build:"
echo "    ./docker/vendor-pipeline.sh"
echo "    docker build -t abc-print-svc:local -f Containerfile ."
[ "$warn" -gt 0 ] && echo "  ($warn note(s) above are informational)"
exit 0
