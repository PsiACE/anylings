# Architecture

Why the repository is split into OpenTofu roots, a renderer and a GitOps tree,
and what each layer is allowed to own. Read this before changing ownership; the
task steps live in [operate the platform](../how-to/operate.md).

## Layers

```text
configuration (cluster.toml)
      │
      ├─► OpenTofu k0s / doks ──► cluster + kubeconfig + context
      │
      └─► render + encrypt ─────► $ANYLINGS_OUTPUT_DIR/kubernetes/
                                        │  (published to Git)
                                        ▼
                              bootstrap Application
                                        │
                       ┌────────────────┴────────────────┐
                   Argo itself                     projects root
                                                        │
                                          AppProject + ApplicationSet
                                                        │
                                          apps/*/*/config.json
                                                        │
                                              workload Applications
```

Three consequences follow, and they matter more than the diagram:

1. **OpenTofu stops at the bootstrap Application.** After the first install,
   Argo owns Argo, the project, the ApplicationSet and every workload. There is
   no persistent Helm release competing with Argo self-management, and no second
   controller reconciling the same objects.
2. **The renderer produces files, not resources.** `just configure platform`
   writes and encrypts a tree; publishing it is a deliberate Git act. Rendering
   never contacts a cluster or a cloud API, which is why the offline checks can be
   run anywhere.
3. **The cloud root owns the edge, the cluster owns the connector.** Tunnel,
   routes, DNS and Access applications are OpenTofu resources; the cluster runs
   only the outbound connector with a token. A public hostname therefore exists
   only when both halves are correct, and neither half is inferred.

## Backends differ only in provisioning

k0s and DOKS produce the same kubeconfig-plus-context contract, and everything
after that is shared: render, publish, bootstrap, wait, platform apply. k0s
follows the official terraform_data/k0sctl orchestration pattern internally, so
the cluster lifecycle stays in the normal root rather than a separate CLI path.
DOKS uses the official DigitalOcean provider for the same lifecycle.

Neither backend is a generic cluster factory: k0s applies the hosts you name, and
DOKS keeps one fixed worker pool whose size and count are its only pool inputs.
Autoscaling, automatic upgrades and HA control planes are not silently added.

## Ordering and its safety property

Within a render, and against the edge, the order is fixed:

```text
Access policy → tunnel ingress → DNS record
```

A new hostname becomes resolvable only after the policy that gates it and the
route that serves it exist. Destroy walks the same path backwards. This is why an
intentionally public route has to say so explicitly: the default is protected, the
allowed identities must be non-empty, and a route that opts out must restrict
itself in the tunnel ingress instead of asking for a global bypass.

## What deletion means

Following Argo CD Autopilot's model, there is exactly one deletion behaviour:

- Removing a workload directory from Git prunes the generated Application and its
  workloads.
- You delete the bootstrap Application and the tree cascades; Argo's own
  installation is not cascaded through its self-management Application, but its
  Git credential may be removed.
- Argo self-management alone omits the deletion finalizer, as upstream specifies.
- There is no retention mode, no preserve toggle and no parallel "detach" path.
- Infrastructure objects keep their own explicit lifecycle: the k0s and DOKS
  cluster owners and R2 buckets carry `prevent_destroy`, so removing any of them
  is a reviewed lifecycle change rather than a plain destroy.

Storage follows the same rule. This template creates no PersistentVolumeClaim for
an application; an operator-owned claim is mounted, never adopted, so deleting the
workload leaves the claim and its data to its owner. The platform does not back
anything up.

## What is deliberately absent

No operator, database or log-tailing daemon beyond the pinned components listed in
the [configuration reference](../reference/configuration.md). No service mesh,
no ingress controller, no dashboard server, no secret manager. Observability,
the agent runtime and SpinKube are opt-in slices; a default render publishes only
the gateway, the connector and the reloader.

## Limits of the evidence

Offline checks prove that a configuration renders, encrypts, resolves and matches
its declared contracts, using mocked or absent providers. They do not prove live
cloud behaviour, Access propagation, CSI behaviour, DNS resolution from a Pod,
certificate trust or multi-host upgrades. Treat those as environment-specific
validation you must perform, not as something a passing check implies.
