variable "config_path" {
  description = "Absolute path to a reviewed k0sctl Cluster configuration. Its contents are stored in private state; referenced SSH keys must remain available for refresh, apply and destroy."
  type        = string
  nullable    = false

  validation {
    condition     = startswith(var.config_path, "/") && fileexists(var.config_path)
    error_message = "config_path must name an existing absolute configuration file."
  }
}
