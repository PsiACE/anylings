variable "cluster_name" {
  type    = string
  default = "anylings"
}

variable "region" {
  type    = string
  default = "sgp1"
}

variable "kubernetes_version" {
  description = "Explicit DOKS release slug; there is no latest or automatic upgrade."
  type        = string
  nullable    = false

  validation {
    condition     = can(regex("^[0-9]+\\.[0-9]+\\.[0-9]+-do\\.[0-9]+$", var.kubernetes_version))
    error_message = "Use a full DOKS release slug, such as 1.36.3-do.0."
  }
}

variable "node_size" {
  description = "DOKS worker size slug. An existing pool's slug is immutable, so changing this is a reviewed pool replacement with a drain, not an in-place resize."
  type        = string
  nullable    = false
  default     = "s-2vcpu-4gb"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]*$", var.node_size))
    error_message = "Use a DigitalOcean size slug such as s-2vcpu-4gb, without spaces or a leading dash."
  }
}

variable "node_count" {
  description = "Fixed worker count for the single pool. Defaults to two; one is the explicit economy profile, never a silent reduction."
  type        = number
  nullable    = false
  default     = 2

  validation {
    condition     = var.node_count >= 1 && var.node_count <= 512 && floor(var.node_count) == var.node_count
    error_message = "Use a whole number of workers from 1 to 512; zero, negative and fractional counts are not accepted."
  }
}
