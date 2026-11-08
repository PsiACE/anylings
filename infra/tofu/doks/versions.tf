terraform {
  required_version = ">= 1.13.0"

  required_providers {
    digitalocean = {
      source  = "digitalocean/digitalocean"
      version = "2.107.0"
    }
  }

  # Local state is a rehearsal default only. Production use must configure a
  # private, encrypted and backed-up backend; see ../README.md.
}

# Authentication comes from DIGITALOCEAN_TOKEN / DIGITALOCEAN_ACCESS_TOKEN in
# the environment. No token is accepted as an input variable or stored in a
# tracked tfvars file.
provider "digitalocean" {}
