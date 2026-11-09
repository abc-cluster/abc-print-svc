#!/usr/bin/env bash
# Deploy abc-print-svc on Nomad: the workers' namespace and job, the front door's
# dispatch policy, then the front door. Idempotent; rerun to roll a new version.
#
#   NOMAD_ADDR=… NOMAD_TOKEN=<management> nomad/deploy.sh 0.15.0
#
# A management token is needed once per deploy, by the operator, to register the
# jobs and the policy. The running service holds no token: the front door
# dispatches with its own workload identity.
#
# Redeploying the front door under the job ID that `abc app deploy` used
# (app-mbhg-bioinformatics-abc-print-svc) is a rolling update: the new allocation
# must pass /health before the old one stops, and the URL does not change.
set -euo pipefail
V="${1:?usage: $0 <version>}"
HERE="$(cd "$(dirname "$0")" && pwd)"
NAME="${ABCPRINT_JOB_NAME:-app-mbhg-bioinformatics-abc-print-svc}"
HOST="${ABCPRINT_HOST:-mbhg-bioinformatics-abc-print-svc.apps.seedling.abc-cluster.cloud}"
# NOMAD_CMD lets a wrapper stand in for the nomad binary, e.g.
#   NOMAD_CMD="abc admin services cli nomad --"
read -r -a NOMAD <<< "${NOMAD_CMD:-nomad}"

tokens="$(grep -vE '^[[:space:]]*(#|$)' "$HERE/tokens.hashes" | paste -sd'|' -)"
[ -n "$tokens" ] || { echo "FATAL: no token entries in $HERE/tokens.hashes" >&2; exit 1; }

"${NOMAD[@]}" namespace apply \
  -description "abc-print-svc workers: dispatched by the print front door only; no member policy grants anything here" \
  svc-print
"${NOMAD[@]}" job run -detach -var "image=ghcr.io/abc-cluster/abc-print-worker:$V" \
  "$HERE/abc-print-run.nomad.hcl"
# Before the front door, so its first allocation can dispatch.
"${NOMAD[@]}" acl policy apply -namespace abc-apps -job "$NAME" \
  -description "abc-print-svc front door: dispatch abc-print-run" \
  "abc-print-svc-dispatch" "$HERE/front-door-dispatch.policy.hcl"
"${NOMAD[@]}" job run -var "image=ghcr.io/abc-cluster/abc-print-svc:$V" \
  -var "name=$NAME" -var "host=$HOST" -var "tokens=$tokens" \
  "$HERE/abc-print-svc.nomad.hcl"
