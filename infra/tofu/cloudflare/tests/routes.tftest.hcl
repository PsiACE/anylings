# Offline contract for per-route zones, the single tunnel and Access IdP
# selection. Mocked provider: no token, no Cloudflare API call and no state.
mock_provider "cloudflare" {
  mock_data "cloudflare_zone" {
    defaults = {
      id      = "zone-example-com"
      name    = "example.com"
      zone_id = "zone-example-com"
      account = { id = "synthetic-account", name = "synthetic" }
    }
  }
}

run "single_zone_backward_compatible" {
  command = plan

  variables {
    cloudflare_account_id = "synthetic-account"
    zone_name             = "example.com"
    access_include_emails = ["synthetic@example.com"]
    routes = {
      "ui.example.com" = {
        service = "http://ui.namespace.svc:8080"
      }
    }
  }

  assert {
    condition     = toset(keys(data.cloudflare_zone.route)) == toset(["example.com"])
    error_message = "An existing input without zone_name must look up exactly the default zone."
  }

  assert {
    condition     = cloudflare_dns_record.route["ui.example.com"].zone_id == "zone-example-com"
    error_message = "A route without zone_name must use the default zone."
  }

  assert {
    condition     = cloudflare_zero_trust_access_application.route["ui.example.com"].zone_id == "zone-example-com"
    error_message = "The Access application must use the route's zone."
  }

  assert {
    condition     = cloudflare_zero_trust_access_application.route["ui.example.com"].allowed_idps == null
    error_message = "Without explicit IdPs the root must leave the provider default intact."
  }
}

run "two_zones_one_account_and_tunnel" {
  command = plan

  variables {
    cloudflare_account_id    = "synthetic-account"
    zone_name                = "example.com"
    access_include_emails    = ["synthetic@example.com"]
    access_allowed_idp_ids   = ["synthetic-github-idp"]
    access_include_group_ids = []
    routes = {
      "ui.example.com" = {
        service = "http://ui.namespace.svc:8080"
      }
      "argo.example.com" = {
        service   = "http://argo.namespace.svc:8080"
        zone_name = "example.com"
      }
      "api.example.net" = {
        service   = "http://api.namespace.svc:8080"
        path      = "api"
        access    = false
        zone_name = "example.net"
      }
    }
  }

  override_data {
    target = data.cloudflare_zone.route["example.net"]
    values = {
      id      = "zone-example-net"
      name    = "example.net"
      zone_id = "zone-example-net"
      account = { id = "synthetic-account", name = "synthetic" }
    }
  }

  assert {
    condition     = toset(keys(data.cloudflare_zone.route)) == toset(["example.com", "example.net"])
    error_message = "Each distinct route zone must be looked up once."
  }

  assert {
    condition     = cloudflare_dns_record.route["api.example.net"].zone_id == "zone-example-net"
    error_message = "A route must write DNS in its own zone, not the default one."
  }

  assert {
    condition     = cloudflare_dns_record.route["ui.example.com"].zone_id == "zone-example-com"
    error_message = "A default-zone route must stay in the default zone."
  }

  # The tunnel resource has no for_each or count: its single address is the one
  # tunnel serving both zones.
  assert {
    condition     = cloudflare_zero_trust_tunnel_cloudflared.this.config_src == "cloudflare"
    error_message = "The one tunnel must stay remotely managed."
  }

  assert {
    condition     = toset(keys(cloudflare_zero_trust_access_application.route)) == toset(["argo.example.com", "ui.example.com"])
    error_message = "Only Access-protected routes get an application; the API hostname stays bearer-authorized by the gateway."
  }

  assert {
    condition     = cloudflare_zero_trust_access_application.route["ui.example.com"].zone_id == "zone-example-com"
    error_message = "A protected application keeps its own zone."
  }

  assert {
    condition     = alltrue([for hostname, app in cloudflare_zero_trust_access_application.route : app.allowed_idps == toset(["synthetic-github-idp"])])
    error_message = "Explicit IdP IDs must reach the real Access application field for every protected route."
  }

  assert {
    condition     = alltrue([for hostname, app in cloudflare_zero_trust_access_application.route : length(app.policies) == 1 && app.policies[0].decision == "allow"])
    error_message = "Restricting IdPs must not replace or widen the allow policy."
  }

  assert {
    condition = contains(
      [for rule in cloudflare_zero_trust_tunnel_cloudflared_config.this.config.ingress : "${rule.hostname}:${rule.path == null ? "" : rule.path}" if try(rule.hostname, null) != null],
      "api.example.net:api",
    )
    error_message = "The API route must stay path-restricted in the tunnel ingress."
  }

  assert {
    condition = [
      for rule in cloudflare_zero_trust_tunnel_cloudflared_config.this.config.ingress : rule.hostname
      if try(rule.hostname, null) != null
    ] == keys(var.routes)
    error_message = "Tunnel ingress must keep the declared route order."
  }

  assert {
    condition     = cloudflare_zero_trust_tunnel_cloudflared_config.this.config.ingress[length(var.routes)].service == "http_status:404"
    error_message = "The explicit routes must stay in front of the final catch-all rule."
  }
}

run "reject_hostname_outside_selected_zone" {
  command = plan

  variables {
    cloudflare_account_id = "synthetic-account"
    zone_name             = "example.com"
    access_include_emails = ["synthetic@example.com"]
    routes = {
      "ui.example.net" = {
        service = "http://ui.namespace.svc:8080"
      }
    }
  }

  expect_failures = [var.routes]
}

run "reject_hostname_outside_route_zone" {
  command = plan

  variables {
    cloudflare_account_id = "synthetic-account"
    zone_name             = "example.com"
    access_include_emails = ["synthetic@example.com"]
    routes = {
      "ui.example.com" = {
        service   = "http://ui.namespace.svc:8080"
        zone_name = "example.net"
      }
    }
  }

  expect_failures = [var.routes]
}

run "reject_cross_account_zone" {
  command = plan

  variables {
    cloudflare_account_id = "synthetic-account"
    zone_name             = "example.com"
    access_include_emails = ["synthetic@example.com"]
    routes = {
      "ui.example.com" = {
        service = "http://ui.namespace.svc:8080"
      }
    }
  }

  override_data {
    target = data.cloudflare_zone.route["example.com"]
    values = {
      id      = "zone-other-account"
      name    = "example.com"
      zone_id = "zone-other-account"
      account = { id = "another-account", name = "another" }
    }
  }

  expect_failures = [data.cloudflare_zone.route]
}

run "empty_identity_fails_closed" {
  command = plan

  variables {
    cloudflare_account_id = "synthetic-account"
    zone_name             = "example.com"
    routes = {
      "ui.example.com" = {
        service = "http://ui.namespace.svc:8080"
      }
    }
  }

  expect_failures = [cloudflare_zero_trust_access_application.route]
}
