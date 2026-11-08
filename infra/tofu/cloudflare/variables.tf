variable "cloudflare_account_id" {
  type = string
}

variable "zone_name" {
  description = "An existing Cloudflare zone; this root does not create it."
  type        = string
}

variable "tunnel_name" {
  type    = string
  default = "anylings"
}

variable "routes" {
  description = <<-EOT
    Explicit public hostname to in-cluster origin mapping. Nothing is routed
    implicitly: every hostname that should be reachable has to be listed here.
    `access = true` puts the hostname behind a Cloudflare Access application.
  EOT
  type = map(object({
    service = string
    path    = optional(string, "")
    access  = optional(bool, true)
  }))
  nullable = false

  validation {
    condition     = length(var.routes) > 0
    error_message = "At least one explicit route is required; the root does not invent public hostnames."
  }

  validation {
    condition = alltrue([
      for hostname, route in var.routes : endswith(hostname, ".${var.zone_name}") || hostname == var.zone_name
    ])
    error_message = "Every route hostname must belong to var.zone_name."
  }

  validation {
    condition = alltrue([
      for hostname, route in var.routes : can(regex("^https?://", route.service))
    ])
    error_message = "Route services must be explicit http:// or https:// origins reachable from the cluster."
  }
}

variable "access_include_emails" {
  type    = list(string)
  default = []
}

variable "access_include_group_ids" {
  description = "Existing Cloudflare Access group IDs allowed for protected routes. Groups are referenced by ID, not created here."
  type        = list(string)
  default     = []
}

variable "session_duration" {
  description = "Cloudflare Access session duration for protected routes."
  type        = string
  default     = "24h"
}

variable "r2_buckets" {
  description = "Optional bucket names in apac. Other provider defaults apply; destruction is protected."
  type        = set(string)
  default     = []
}
