variable "account_id" {
  description = "Cloudflare account ID for Zero Trust."
  type        = string
}

variable "zone_id" {
  description = "Cloudflare DNS zone ID in the same account."
  type        = string
}

variable "hostname" {
  description = "Public hostname for anylings."
  type        = string
}

variable "admin_email" {
  description = "Email address allowed by Cloudflare Access."
  type        = string
}

variable "team_name" {
  description = "Zero Trust team name, without .cloudflareaccess.com."
  type        = string
}
