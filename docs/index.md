# anylings

One repository, one machine: your Fedora desktop **is** the homelab. The local
CLI configures the host, bootstraps a single-node k0s cluster, and lets Argo CD
keep everything in sync from GitHub.

::::{grid} 2 2 2 2
:gutter: 3

:::{grid-item-card} Quickstart
:link: quickstart
:link-type: doc

Install the CLI, configure the host, and bootstrap the cluster.
:::

:::{grid-item-card} Operations
:link: operations
:link-type: doc

Deploy apps, open local UIs, add workers, and back up state.
:::

:::{grid-item-card} Remote access
:link: remote-access
:link-type: doc

Optionally publish Glance and Argo CD through Cloudflare Access.
:::

:::{grid-item-card} Architecture
:link: architecture
:link-type: doc

See how host configuration, Kubernetes, and GitOps fit together.
:::

:::{grid-item-card} Reference
:link: reference
:link-type: doc

Look up CLI commands, repository layout, and local configuration.
:::
::::

```{toctree}
:hidden:
:maxdepth: 1

quickstart
operations
remote-access
architecture
reference
```
