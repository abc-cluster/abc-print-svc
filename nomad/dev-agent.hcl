# A throwaway local Nomad for testing the dispatch path end to end, with the real
# images and with ACLs ON, so the front door's workload-identity policy is what
# actually authorises its dispatches:
#
#   nomad agent -dev -config nomad/dev-agent.hcl -data-dir <dir>
#   (then see docs/building.md, "Testing dispatch locally")
#
# Non-default ports so it can never be mistaken for a real agent. The scratch
# host volume stands in for the one the cluster nodes register; point its path at
# a directory Docker Desktop shares.
bind_addr = "0.0.0.0"

ports {
  http = 14646
  rpc  = 14647
  serf = 14648
}

acl {
  enabled = true
}

client {
  host_volume "scratch" {
    path      = "/private/tmp/abcprint-nomad-scratch"
    read_only = false
  }
}

plugin "docker" {
  config {
    volumes {
      enabled = true
    }
    allow_privileged = false
  }
}
