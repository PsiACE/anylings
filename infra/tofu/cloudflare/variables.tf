variable "cloudflare_account_id" {
  type = string
}

variable "zone_name" {
  description = "Default existing Cloudflare zone for routes that do not select their own. This root does not create zones."
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
    `zone_name` selects a different existing zone in the same account; it
    defaults to var.zone_name.
  EOT
  type = map(object({
    service   = string
    path      = optional(string, "")
    access    = optional(bool, true)
    zone_name = optional(string)
  }))
  nullable = false

  validation {
    condition     = length(var.routes) > 0
    error_message = "At least one explicit route is required; the root does not invent public hostnames."
  }

  validation {
    condition = alltrue([
      for hostname, route in var.routes : try(
        endswith(hostname, ".${coalesce(route.zone_name, var.zone_name)}") || hostname == coalesce(route.zone_name, var.zone_name),
        false,
      )
    ])
    error_message = "Every route hostname must belong to that route's zone: its zone_name, or var.zone_name when unset."
  }

  validation {
    condition = alltrue([
      for hostname, route in var.routes : route.zone_name == null || length(trimspace(route.zone_name)) > 0
    ])
    error_message = "A route zone_name must be a non-empty existing zone name, or omitted to use var.zone_name."
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

variable "access_allowed_idp_ids" {
  description = <<-EOT
    Existing Cloudflare Access identity provider IDs that protected routes accept.
    Empty keeps the provider default for the account; no identity provider is
    created, and the email/group allow policy still applies either way.
  EOT
  type        = set(string)
  default     = []

  validation {
    condition     = alltrue([for id in var.access_allowed_idp_ids : length(trimspace(id)) > 0])
    error_message = "access_allowed_idp_ids may only contain non-empty existing identity provider IDs."
  }
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
