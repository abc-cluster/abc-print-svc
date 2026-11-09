# abc-print-svc — the front door.
#
# Validates, admits and queues requests, and dispatches each run as a child of
# abc-print-run (nomad/abc-print-run.nomad.hcl), which compiles in the worker
# image. This image carries no profile, pipeline or toolchain.
#
# Not deployed with `abc app deploy`, which cannot mount a host volume: the front
# door and its workers share the run directory. The routing tags below are the
# ones `abc app` generates, so the app keeps its URL scheme.
#
#   nomad job run -var image=ghcr.io/abc-cluster/abc-print-svc:<v> \
#                 -var tokens="$(…the ABC_PRINT_SVC_TOKEN hashes…)" nomad/abc-print-svc.nomad.hcl
#   nomad acl policy apply -namespace abc-apps -job <name> abc-print-svc-dispatch \
#                 nomad/front-door-dispatch.policy.hcl
#
# Its Nomad credential is its own workload identity (identity { file = true }),
# granted dispatch in svc-print by that job-scoped policy. There is no token to
# store.

variable "image" {
  type = string
}

variable "name" {
  type        = string
  default     = "app-mbhg-bioinformatics-abc-print-svc"
  description = "Job ID, service name and router name. Also the scope of the dispatch policy."
}

variable "host" {
  type    = string
  default = "mbhg-bioinformatics-abc-print-svc.apps.seedling.abc-cluster.cloud"
}

variable "tokens" {
  type        = string
  default     = ""
  description = "ABC_PRINT_SVC_TOKEN: label:scopes:sha256:hex entries, '|'-separated. Hashes, never tokens. Empty refuses everyone."
}

variable "nomad_addr" {
  type        = string
  default     = ""
  description = "The Nomad HTTP API as reached from inside the task. Empty: this node's agent."
}

variable "jurisdiction" {
  type    = string
  default = "ZA"
}

variable "datacenters" {
  type    = list(string)
  default = ["*"]
}

variable "node_pool" {
  type    = string
  default = "platform"
}

variable "volume" {
  type    = string
  default = "scratch"
}

variable "trusted_proxies" {
  type        = string
  default     = "10.0.0.0/8,172.16.0.0/12,100.64.0.0/10"
  description = "Where X-Forwarded-For is believed from: the edge and Traefik. Otherwise every caller shares the proxy's rate-limit bucket."
}

job "abc-print-svc" {
  id          = var.name
  namespace   = "abc-apps"
  type        = "service"
  datacenters = var.datacenters
  node_pool   = var.node_pool

  meta {
    abc_app      = "abc-print-svc"
    abc_project  = "mbhg-bioinformatics"
    abc_health   = "/health"
    khan_lineage = "interim: abc-print-svc front door on abc-dispatch"
  }

  # A canary: a new version runs beside the old until it passes /health, then
  # takes over; if it never does, the old one keeps serving and the job reverts.
  # Without it, updating a one-allocation service stops the old allocation first.
  update {
    max_parallel      = 1
    canary            = 1
    auto_promote      = true
    auto_revert       = true
    min_healthy_time  = "10s"
    healthy_deadline  = "5m"
    progress_deadline = "10m"
    health_check      = "checks"
  }

  group "app" {
    count = 1

    restart {
      attempts = 3
      delay    = "15s"
      interval = "5m"
      mode     = "fail"
    }

    network {
      port "http" {
        to = 8080
      }
    }

    volume "dispatch" {
      type   = "host"
      source = var.volume
    }

    service {
      name     = var.name
      provider = "nomad"
      port     = "http"
      tags = [
        "traefik.enable=true",
        "traefik.http.routers.${var.name}-public.rule=Host(`${var.host}`)",
        "traefik.http.routers.${var.name}-public.entrypoints=web",
        "traefik.http.routers.${var.name}-internal.rule=PathPrefix(`/apps/${trimprefix(var.name, "app-")}`)",
        "traefik.http.routers.${var.name}-internal.entrypoints=private,shared",
        "traefik.http.middlewares.${var.name}-strip.stripprefix.prefixes=/apps/${trimprefix(var.name, "app-")}",
        "traefik.http.routers.${var.name}-internal.middlewares=${var.name}-strip@nomad-abc-apps",
      ]
      check {
        type     = "http"
        path     = "/health"
        interval = "30s"
        timeout  = "10s"
      }
    }

    # The service's own corner of the shared volume, owned by the UID both
    # images run as. Created here so a deploy needs no step on the host.
    task "prepare" {
      driver = "docker"
      lifecycle {
        hook    = "prestart"
        sidecar = false
      }
      user = "root"
      config {
        image   = var.image
        command = "sh"
        # Per job name: two front doors (a -next beside the live one) must never
        # share a run store or run directories — each would adopt the other's runs.
        args    = ["-c", "d=/srv/dispatch/abc-print-svc/${var.name}; mkdir -p $d/state $d/runs && chown 10001 /srv/dispatch/abc-print-svc $d $d/state $d/runs && chmod 0750 /srv/dispatch/abc-print-svc $d"]
        network_mode = "none"
      }
      volume_mount {
        volume      = "dispatch"
        destination = "/srv/dispatch"
      }
      resources {
        cpu    = 50
        memory = 32
      }
    }

    task "app" {
      driver = "docker"

      config {
        image = var.image
        ports = ["http"]
      }

      # The Nomad credential the front door dispatches with: its own workload
      # identity, written to secrets/nomad_token.
      identity {
        file = true
      }

      volume_mount {
        volume      = "dispatch"
        destination = "/srv/dispatch"
      }

      env {
        ABC_PRINT_SVC_TOKEN          = var.tokens
        ABCPRINT_EXECUTOR            = "nomad"
        ABCPRINT_NOMAD_ADDR          = var.nomad_addr != "" ? var.nomad_addr : "http://${attr.unique.network.ip-address}:4646"
        ABCPRINT_NOMAD_TOKEN_FILE    = "${NOMAD_SECRETS_DIR}/nomad_token"
        ABCPRINT_DISPATCH_NAMESPACE  = "svc-print"
        ABCPRINT_DISPATCH_JOB        = "abc-print-run"
        ABCPRINT_JURISDICTION        = var.jurisdiction
        ABCPRINT_DATA_DIR            = "/srv/dispatch/abc-print-svc/${var.name}/state"
        ABCPRINT_DISPATCH_ROOT       = "/srv/dispatch/abc-print-svc/${var.name}/runs"
        ABCPRINT_CORS_ORIGINS        = "*"
        ABCPRINT_MAX_RENDERS         = "2"
        ABCPRINT_MAX_QUEUED          = "8"
        ABCPRINT_ARTIFACT_TTL_S      = "21600"
        ABCPRINT_TRUSTED_PROXIES     = var.trusted_proxies
      }

      resources {
        cpu    = 500
        memory = 512
      }
    }
  }
}
