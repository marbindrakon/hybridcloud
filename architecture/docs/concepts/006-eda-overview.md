# Platform EDA — Conceptual Overview

**Audience**: Product owners, managers, non-technical stakeholders  
**Last updated**: 2026-06-09

---

## What Changed and Why

Previously, each Sovereign Cloud operator (Entity, Team, Assignment, plugins, and others) performed its own automation directly inside the operator pod. That meant every operator needed credentials for Keycloak, Vault, cloud APIs, and Kubernetes — and every change to provisioning logic required rebuilding and redeploying the operator.

The Platform EDA rebuild changes this model. Operators on the services cluster now do one focused job: detect when a custom resource needs action, update a simple status field, and emit a structured event. All heavy automation — creating namespaces, configuring Keycloak groups, provisioning Vault instances, managing cloud resources — moves to Event-Driven Ansible (EDA) on the central cluster.

This is analogous to moving from a restaurant where every waiter also cooks, to one where waiters take orders and a central kitchen prepares the meals. Each role becomes simpler, more reliable, and easier to change independently.

---

## What EDA Does

Event-Driven Ansible is Red Hat's event-processing layer within Ansible Automation Platform 2.5. In Sovereign Cloud, EDA acts as the central automation engine:

1. **Receives events** from operator actions via an authenticated Event Stream webhook.
2. **Matches events** to the correct rulebook based on event type (create, delete, or forced reconcile).
3. **Runs Ansible playbooks** in isolated Decision Environment containers, each tailored to one operator's needs.
4. **Writes results back** to the custom resource status on the services cluster, signaling completion to the operator.

The operator watches for that completion signal. Once EDA marks a resource as ready (or deletion complete), the operator stops sending events and, on delete, allows the resource to be fully removed.

---

## How Events Travel Between Clusters

Operators and EDA run on different OpenShift clusters. Kubernetes Events are cluster-local — they do not automatically cross cluster boundaries.

The operators publish their events themselves to the Kafka event bus (AMQ Streams) on the central cluster, over an outbound TLS connection; the EDA activations consume them there. (An earlier design used a separate event forwarder posting to an EDA Event Stream; it is retired, see [retired components](../architecture.md#retired-components).)

This push-based design follows the same connectivity pattern already used by Red Hat Advanced Cluster Management: outbound connections from the managed cluster to the hub, avoiding firewall changes.

---

## Benefits

### Separation of Concerns

Operators handle Kubernetes API interactions and status management. EDA handles external system integration. Each component has a clear, bounded responsibility.

### Independent Scaling

Operator pods stay lightweight and can reconcile many custom resources concurrently. EDA activations scale based on event volume. Decision Environment images can be updated without touching operator deployments.

### Faster Iteration

Changing provisioning logic means updating an Ansible role and rebuilding a Decision Environment image — not redeploying a full operator with SDK dependencies. Platform engineers can test rulebook changes in isolation before registering new activations.

### Consistent Patterns

All eleven operators follow the same event contract, status handshake, and EDA registration flow. New operators inherit a proven template rather than reinventing integration patterns.

### Improved Security Posture

Operators no longer hold credentials for Keycloak, Vault, or cloud APIs. Their RBAC shrinks to event emission and status reads. Sensitive credentials stay in Vault and are consumed by EDA roles at runtime.

---

## Risk Mitigation

### Retry and Resilience

Kafka keeps events durably until the activations have consumed them. If the operator cannot publish, it publishes again on its next reconcile cycle. No single network blip can permanently lose a provisioning request.

### Idempotency

Every EDA playbook is designed to be safe to run multiple times. Duplicate events — from forwarder replay or operator re-emit — do not create duplicate resources. Ansible roles use present/absent semantics and check existing state before creating.

### Monitoring

The forwarder exposes health endpoints for Kubernetes probes. EDA activations report status on the central controller. The global test suite (`global_tests/`) validates end-to-end event flow, operator health, and dashboard functionality on demand.

### Forced Reconciliation

Anyone allowed to change an object can trigger a fresh automation run with **Retry now** on its page. This writes a new timestamp into the `reconcileNow` annotation; the operator detects the new value and emits a reconcile event. EDA re-runs provisioning without manual cluster access.

---

## What Stays the Same

- Custom resources, their schemas, and dashboard views are unchanged from a user perspective.
- GitOps deployment through central ArgoCD remains the only change path after bootstrap.
- Secrets continue to live exclusively in Vault, delivered via ExternalSecrets.
- The `sovereign-*` namespace model and tenant isolation boundaries are preserved.
- Both dashboards (sovereign and tenancy) reflect operator status fields as before.

---

## Operator Coverage

The following operators were refactored to the event-driven model:

| Tier | Operators |
|------|-----------|
| Core tenancy | Entity, Team, Assignment, Project |
| Platform | PlatformOpenshift, CloudOSO |
| Plugins | RBAC, Vault, AAP, Quay |

The Git export (`iaac-git-sync`, which replaced the retired Go Plugin SDX) handles CR-to-Gitea sync separately and is not part of the EDA path.

Each EDA-covered operator has a matching Decision Environment, create/delete rulebooks, and provision/teardown Ansible roles under `eda/<operator>/`.

---

## Further Reading

- Technical details: [006 EDA Architecture](../technical/006-eda-architecture.md)
- Developer guide: [006 EDA Developer Guide](../tutorial/006-eda-developer-guide.md)
- Business presentation: [006 EDA Slides](../archive/concepts/006-eda-slides.md)
- Security validation: [006 Hardening Check](../../hardeningcheck/006-platform-eda-rebuild.md)
