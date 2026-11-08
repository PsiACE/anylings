terraform {
  required_version = ">= 1.13.0"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "5.28.1"
    }
  }

  # Local state is a rehearsal default only. Production use must configure a
  # private, encrypted and backed-up backend; see ../README.md.
}

# Authentication comes from CLOUDFLARE_API_TOKEN in the environment. No API key,
# token or secret is accepted as an input variable or stored in tracked files.
provider "cloudflare" {}
