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
