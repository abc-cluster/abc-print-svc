# What the front door may do in Nomad: dispatch its workers, follow them, stop them.
#
# Attached to the front door's own WORKLOAD IDENTITY, not to a token — so there is
# no Nomad credential to store, rotate or leak:
#
#   nomad acl policy apply -namespace abc-apps -job <front-door-job-id> \
#     -description "abc-print-svc front door: dispatch abc-print-run" \
#     abc-print-svc-dispatch nomad/front-door-dispatch.policy.hcl
#
# The capabilities are exactly what abc_dispatch.nomad.NomadClient calls:
#   dispatch-job   POST   /v1/job/abc-print-run/dispatch
#   read-job       GET    /v1/job/<child>, /v1/job/<child>/allocations
#   list-jobs      GET    /v1/jobs?prefix=abc-print-run/   (re-adoption after a restart)
#   submit-job     DELETE /v1/job/<child>                  (cancel, placement deadline)
#
# submit-job is broader than "stop": it would also let the front door register a
# job in svc-print. Nomad has no narrower capability for stopping a job. The
# front door's code never registers one, and svc-print holds nothing else.
namespace "svc-print" {
  capabilities = ["dispatch-job", "read-job", "list-jobs", "submit-job"]
}
