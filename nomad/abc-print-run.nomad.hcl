# abc-print-run — the worker: one dispatched child per compilation.
#
# The operator registers this once. The front door dispatches it, filling only
# the meta slots below; a dispatch cannot choose the image, the driver, the
# namespace, the volume or the resources. That is what makes a request a request
# and not a job spec.
#
#   nomad namespace apply -description "abc-print-svc workers" svc-print
#   nomad job run -var image=ghcr.io/abc-cluster/abc-print-worker:<v> nomad/abc-print-run.nomad.hcl
#
# The namespace is the boundary (abc-universe brainstorms/abc-data-platform/
# 2026-10-31-workload-identity-credential-delivery-evaluation.md): no member
# policy grants anything in svc-print, so no member can submit a job here, read
# one, exec into one, or read its logs. Only the front door's workload identity
# may dispatch into it (nomad/front-door-dispatch.policy.hcl).

variable "image" {
  type        = string
  description = "The worker image, built with --target worker. Same version as the front door."
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
  type        = string
  default     = "scratch"
  description = "Host volume shared with the front door, mounted at the same path in both."
}

job "abc-print-run" {
  namespace   = "svc-print"
  type        = "batch"
  datacenters = var.datacenters
  node_pool   = var.node_pool

  parameterized {
    payload       = "forbidden"
    meta_required = ["run_id", "run_dir", "owner_prefix", "kind",
                     "jurisdiction", "project_id", "trigger_source", "cost_center"]
  }

  meta {
    khan_lineage = "interim: abc-print-svc dispatch worker; absorbed by Khan POST /v1/jobs"
  }

  group "run" {
    # A failed worker is reported, not retried: the front door decides what a
    # failure means, and a retry here would hide it — and a retried render of the
    # same run would write into the same run directory.
    restart {
      attempts = 0
      mode     = "fail"
    }
    reschedule {
      attempts  = 0
      unlimited = false
    }

    volume "dispatch" {
      type   = "host"
      source = var.volume
    }

    task "worker" {
      driver = "docker"

      config {
        image   = var.image
        command = "python3"
        args    = ["-m", "abc_dispatch.worker", "--kind", "abcprint.service:make_kind"]
        # A document is arbitrary input and the render needs nothing from the
        # network: the profile, fonts and extensions are in the image.
        network_mode = "none"
      }

      # The run directory the front door names in meta.run_dir is an absolute
      # path under this mount, so it must be the same path in both.
      volume_mount {
        volume      = "dispatch"
        destination = "/srv/dispatch"
      }

      env {
        ABCPRINT_SOURCE_DATE_EPOCH = "1700000000"
        ABCPRINT_BUILD_TIMEOUT     = "1800"
      }

      # Longer than run_tool's 30 s grace period, or Nomad's kill arrives first.
      kill_timeout = "45s"

      # One render. A thesis assemble is multi-pass and CPU-bound; ~674 MiB was
      # measured for a 230-page document plus spliced PDFs.
      resources {
        cpu    = 2000
        memory = 3072
      }
    }
  }
}
