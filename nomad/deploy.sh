#!/usr/bin/env bash
# Deploy abc-print-svc on Nomad: the workers' namespace and job, the front door's
# dispatch policy, then the front door. Idempotent; rerun to roll a new version.
#
#   NOMAD_ADDR=… NOMAD_TOKEN=<management> nomad/deploy.sh 0.17.0
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
# Signed profiles (0.18.0): PROFILE_TRUSTED_KEYS=<base64 raw Ed25519 public key>[,…].
# Both jobs get it; set, it makes signatures mandatory.
key_vars=()
[ -n "${PROFILE_TRUSTED_KEYS:-}" ] && key_vars+=(-var "profile_trusted_keys=$PROFILE_TRUSTED_KEYS")
"${NOMAD[@]}" job run -detach -var "image=ghcr.io/abc-cluster/abc-print-worker:$V" \
  ${key_vars[@]+"${key_vars[@]}"} "$HERE/abc-print-run.nomad.hcl"
# Before the front door, so its first allocation can dispatch.
"${NOMAD[@]}" acl policy apply -namespace abc-apps -job "$NAME" \
  -description "abc-print-svc front door: dispatch abc-print-run" \
  "abc-print-svc-dispatch" "$HERE/front-door-dispatch.policy.hcl"
# Published profiles (optional): PROFILE_SOURCE=s3://bucket/prefix [PROFILE_S3_ENDPOINT=…]
# [PROFILE_S3_CREDENTIALS=true, with profile_s3_access_key/_secret_key in nomad/jobs/$NAME].
profile_vars=()
[ -n "${PROFILE_SOURCE:-}" ] && profile_vars+=(-var "profile_source=$PROFILE_SOURCE")
[ -n "${PROFILE_S3_ENDPOINT:-}" ] && profile_vars+=(-var "profile_s3_endpoint=$PROFILE_S3_ENDPOINT")
[ "${PROFILE_S3_CREDENTIALS:-}" = true ] && profile_vars+=(-var "profile_s3_credentials=true")
"${NOMAD[@]}" job run -var "image=ghcr.io/abc-cluster/abc-print-svc:$V" \
  -var "name=$NAME" -var "host=$HOST" -var "tokens=$tokens" ${profile_vars[@]+"${profile_vars[@]}"} \
  ${key_vars[@]+"${key_vars[@]}"} \
  "$HERE/abc-print-svc.nomad.hcl"
