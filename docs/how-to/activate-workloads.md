# Activate workloads after a namespace migration

Use this procedure for the staged Magpie namespace and Bub workload rename.
It is a controlled maintenance operation, not a zero-downtime upgrade.

## Prepare before stopping services

- Review the quiesce and activation commits, the retained-volume migration and
  reverse-binding procedure, and the Cloudflare route transition together.
- Collect normal pull-request checks and review evidence before the window.
  A stacked draft with no checks is not a passing gate. Preserve stack ancestry
  when integrating the quiesce commit, or verify the resulting activation tree
  again; do not assume a squash produces the same replica settings.
- Verify the pinned runtime image is available. Keep the existing credentials,
  recovery material, infrastructure state and volume identities outside Git.
- Verify the destination namespace exists before stopping services. If it is
  absent, prepare it explicitly through the reviewed operator procedure, without
  activating replacement workloads or allowing a new volume to provision.
- Keep both stages unmerged until every transition is ready. Merging quiesce
  and allowing Argo to sync stops the runtime and gateway.

## Perform the transition in order

1. Merge the reviewed quiesce change through GitOps. Verify the actual old
   Deployments have zero replicas and no remaining pods reference either claim.
   A healthy zero-replica Deployment does not mean its service is available.
2. Follow the reviewed operator procedure to retain and rebind the gateway
   volume. Verify its reclaim policy before removing the old claim, and bind
   `gateways/magpie-config` to the same PV and CSI volume identity. Do not delete
   the PV, change its volume handle or permit a replacement volume to provision.
3. Verify `agents/t3-code-home` is still bound to its original volume. The new
   runtime must mount it at `/home/t3`, preserving the state and workspace layout.
4. Merge the activation change only after those bindings are correct and the
   old writers are stopped. Verify Argo's reconciled revision, new pods,
   Services and readiness; source checks alone do not prove reconciliation.
5. Apply the reviewed, committed Cloudflare route inputs using the existing
   production Cloudflare root and state. Preserve the identity allow-list and
   GitHub login restriction. Do not initialize a second infrastructure owner.
6. Check the new public endpoints and their negative authentication cases.
   Keep Magpie limited to the `/v1` API path with native bearer authentication;
   keep the management interface private. An expected unkeyed `401` proves an
   authentication boundary, not successful upstream model inference.

## Avoid unsafe shortcuts

`ReadWriteOnce` restricts mounts to one node, not one pod. Old and new pods on
that node can mount the same claim. Separate `Recreate` Deployments do not
provide a cross-Deployment writer lock: verify the old writers are gone.

A Git revert does not reverse a PVC namespace transition or restore its old
binding. On failure, stop new writers before using the reviewed reverse-binding
procedure and restoring compatible workload and route commits. Do not delete
volume data or rotate credentials as an improvised rollback.
