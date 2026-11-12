# Offline contract for the committed production route profile. The test plans
# the tracked var-file itself, so the reviewed mapping and the assertions cannot
# drift apart. Mocked provider: no token, no Cloudflare API call and no state.
# Identity inputs here are synthetic stand-ins for the operator's private ones:
# this file only proves that the profile overrides the routes and leaves the
# inherited account, identity provider, allow list and session policy intact.
mock_provider "cloudflare" {
  mock_data "cloudflare_zone" {
    defaults = {
      id      = "zone-anylings-ai"
      name    = "anylings.ai"
      zone_id = "zone-anylings-ai"
      account = { id = "synthetic-account", name = "synthetic" }
    }
  }
}

run "production_profile_routes" {
  command = plan

  variables {
    cloudflare_account_id    = "synthetic-account"
    zone_name                = "anylings.ai"
    access_include_emails    = ["operator@example.com"]
    access_allowed_idp_ids   = ["synthetic-github-idp"]
    access_include_group_ids = []
    session_duration         = "24h"
    # The actual committed profile, not a copy or a fixture.
    routes = jsondecode(file("profiles/production.routes.tfvars.json")).routes
  }

  override_data {
    target = data.cloudflare_zone.route["anylings.com"]
    values = {
      id      = "zone-anylings-com"
      name    = "anylings.com"
      zone_id = "zone-anylings-com"
      account = { id = "synthetic-account", name = "synthetic" }
    }
  }

  assert {
    condition = toset(keys(var.routes)) == toset([
      "bub.anylings.ai",
      "argo.anylings.com",
      "magpie.anylings.ai",
    ])
    error_message = "The production profile must declare exactly the Bub, Argo and Magpie hostnames."
  }

  assert {
    condition     = var.routes["bub.anylings.ai"].service == "http://bub.agents.svc.cluster.local:3773"
    error_message = "Bub must route to its current agents namespace origin."
  }

  assert {
    condition     = var.routes["argo.anylings.com"].service == "http://argocd-server.argo-system.svc.cluster.local:80"
    error_message = "Argo must route to the in-cluster argocd-server service."
  }

  assert {
    condition     = var.routes["magpie.anylings.ai"].service == "http://magpie.gateways.svc.cluster.local:3425" && var.routes["magpie.anylings.ai"].path == "^/v1(/.*)?$"
    error_message = "Magpie must route to the gateways namespace on the path-gated API origin."
  }

  assert {
    condition     = alltrue([for hostname in keys(var.routes) : !strcontains(hostname, "t3.anylings.ai") && !strcontains(hostname, "magpie-api")])
    error_message = "Retired hosts and the old API hostname must stay absent."
  }

  assert {
    condition     = toset(keys(cloudflare_zero_trust_access_application.route)) == toset(["argo.anylings.com", "bub.anylings.ai"])
    error_message = "Only Bub and Argo get Access applications; the API hostname stays bearer-authorized by the gateway."
  }

  assert {
    condition     = !contains(keys(cloudflare_zero_trust_access_application.route), "magpie.anylings.ai")
    error_message = "The public API route must not gain an Access application."
  }

  assert {
    condition = alltrue([
      for hostname, app in cloudflare_zero_trust_access_application.route :
      app.allowed_idps == toset(["synthetic-github-idp"]) && app.session_duration == "24h"
    ])
    error_message = "Inherited identity provider selection and session duration must still reach both protected routes."
  }

  assert {
    condition = alltrue([
      for hostname, app in cloudflare_zero_trust_access_application.route :
      length(app.policies) == 1 && app.policies[0].decision == "allow" &&
      tolist(app.policies[0].include)[0].email.email == "operator@example.com"
    ])
    error_message = "Both protected routes must keep the single-email allow policy."
  }

  assert {
    condition     = cloudflare_dns_record.route["magpie.anylings.ai"].zone_id == "zone-anylings-ai"
    error_message = "Magpie DNS must stay in the anylings.ai zone."
  }

  assert {
    condition     = cloudflare_dns_record.route["argo.anylings.com"].zone_id == "zone-anylings-com"
    error_message = "Argo DNS must use the anylings.com zone in the same account."
  }

  assert {
    condition = contains(
      [for rule in cloudflare_zero_trust_tunnel_cloudflared_config.this.config.ingress : "${rule.hostname}:${rule.path == null ? "" : rule.path}" if try(rule.hostname, null) != null],
      "magpie.anylings.ai:^/v1(/.*)?$",
    )
    error_message = "The committed profile's path regex must survive into the tunnel ingress."
  }

  assert {
    condition     = cloudflare_zero_trust_tunnel_cloudflared_config.this.config.ingress[length(var.routes)].service == "http_status:404"
    error_message = "The three routes must stay in front of the closing catch-all rule."
  }
}

run "profile_alone_leaves_no_identity_input" {
  command = plan

  variables {
    cloudflare_account_id = "synthetic-account"
    zone_name             = "anylings.ai"
    access_include_emails = ["operator@example.com"]
    session_duration      = "24h"
    routes                = jsondecode(file("profiles/production.routes.tfvars.json")).routes
  }

  override_data {
    target = data.cloudflare_zone.route["anylings.com"]
    values = {
      id      = "zone-anylings-com"
      name    = "anylings.com"
      zone_id = "zone-anylings-com"
      account = { id = "synthetic-account", name = "synthetic" }
    }
  }

  assert {
    condition     = alltrue([for hostname, app in cloudflare_zero_trust_access_application.route : app.allowed_idps == null])
    error_message = "Without an explicit IdP set the profile must not invent one."
  }

  assert {
    condition     = cloudflare_zero_trust_tunnel_cloudflared.this.config_src == "cloudflare"
    error_message = "The profile must keep the one remotely managed tunnel."
  }
}
