# C4 Level 3 — Multi-Tier Operator

**Scope**: `hybridcloud/operator/` — primary + namespace tiers  
**API group**: `hybridsovereign.redhat/v1alpha1`  
**Last updated**: 2026-07-11

---

## Purpose

The monorepo consolidates 13 legacy Ansible operators into a two-tier architecture:

1. **Primary operator** — cluster-scoped; watches platform-level and plugin-config CRs in `sovereign-cloud` and `sovereign-cloud-plugins`.
2. **Namespace operator** — one Deployment per entity in `entity-<name>`; watches tenant-scoped CRs within that namespace only.

Both tiers are lightweight event emitters. Heavy provisioning runs in EDA on the central cluster (see [event-system.md](event-system.md)).

---

## Component Diagram

```mermaid
C4Component
    title Multi-Tier Operator — Services Cluster

    Container_Boundary(primary, "Primary Operator") {
        Component(entityRole, "entity role", "Ansible", "Validate spec; emit EntityCreateRequested; deploy namespace operator")
        Component(rbacCfgRole, "rbacconfig role", "Ansible", "Emit RbacConfig events")
        Component(aapCfgRole, "aapconfig role", "Ansible", "Emit AAPConfig events")
        Component(quayCfgRole, "quayconfig role", "Ansible", "Emit QuayConfig events")
        Component(entityFin, "entity_finalizer", "Ansible", "Block delete until EDA sets deletionComplete")
        Component(deployNs, "deploy_namespace_operator", "Ansible", "SA + Role + Deployment in entity-<name>")
    }

    Container_Boundary(namespace, "Namespace Operator (per entity)") {
        Component(teamRole, "team role", "Ansible", "Team CR reconcile + events")
        Component(assignRole, "assignment role", "Ansible", "Assignment CR reconcile + events")
        Component(projectRole, "project role", "Ansible", "Project CR reconcile + events")
        Component(personaRole, "persona role", "Ansible", "Persona CR reconcile + events")
        Component(platRole, "platformopenshift role", "Ansible", "OCP cluster CR reconcile + events")
        Component(cloudosoRole, "cloudoso role", "Ansible", "OpenStack env CR reconcile + events")
        Component(cloudawsRole, "cloudaws role", "Ansible", "AWS env CR reconcile + events")
        Component(migrateRole, "openstackmigration role", "Ansible", "VM migration CR reconcile + events")
        Component(pluginRoles, "plugin roles", "Ansible", "Rbac, AAPOrg, QuayOrg, Vault, VaultKV")
    }

    ComponentDb(etcd, "etcd", "K8s API")
    Component(eventEmit, "Event Emitter", "events.k8s.io/v1", "Typed Create/Delete/Reconcile events")
    Component(amqPub, "AMQ Publisher", "Kafka", "Optional audit events via amq_publish.yml")

    Rel(entityRole, deployNs, "On Entity create")
    Rel(deployNs, namespace, "Creates Deployment")
    Rel(entityRole, eventEmit, "EntityCreateRequested")
    Rel(teamRole, eventEmit, "TeamCreateRequested")
    Rel(pluginRoles, eventEmit, "Plugin events")
    Rel(eventEmit, etcd, "Persist")
    Rel(entityRole, amqPub, "Audit trail")
    Rel(namespace, etcd, "Watch entity-<name>")
    Rel(primary, etcd, "Watch sovereign-cloud*")
```

---

## CR Ownership Matrix

### Primary operator (`operator/primary/watches.yaml`)

| Kind | Namespace | Role | Finalizer |
|------|-----------|------|-----------|
| `Entity` | `sovereign-cloud` | `entity` | `entities.hybridsovereign.redhat/finalizer` |
| `RbacConfig` | `sovereign-cloud-plugins` | `rbacconfig` | `rbacconfigs.hybridsovereign.redhat/finalizer` |
| `AAPConfig` | `sovereign-cloud-plugins` | `aapconfig` | `aapconfig.hybridsovereign.redhat/finalizer` |
| `QuayConfig` | `sovereign-cloud-plugins` | `quayconfig` | `quayconfig.hybridsovereign.redhat/finalizer` |

### Namespace operator (`operator/namespace/watches.yaml`)

| Kind | Namespace | Role |
|------|-----------|------|
| `Team` | `entity-<name>` | `team` |
| `Assignment` | `entity-<name>` | `assignment` |
| `Project` | `entity-<name>` | `project` |
| `Persona` | `entity-<name>` | `persona` |
| `PlatformOpenshift` | `entity-<name>` | `platformopenshift` |
| `CloudOSO` | `entity-<name>` | `cloudoso` |
| `CloudAWS` | `entity-<name>` | `cloudaws` |
| `OpenStackMigration` | `entity-<name>` | `openstackmigration` |
| `Rbac` | `entity-<name>` | `rbac` |
| `AAPOrg` | `entity-<name>` | `aaporg` |
| `QuayOrg` | `entity-<name>` | `quayorg` |
| `Vault` | `entity-<name>` | `vault` |
| `VaultKV` | `entity-<name>` | `vaultkv` |

---

## Reconcile Pattern (Both Tiers)

Every role follows the same lightweight pattern:

1. Validate CR spec prerequisites.
2. Set `status.status: reconciling`, `status.ready: false`.
3. Emit a typed Kubernetes Event (`<Kind>CreateRequested`, `<Kind>DeleteRequested`, or `<Kind>ReconcileRequested`).
4. Optionally publish to AMQ Streams (`operator/roles/_common/tasks/amq_publish.yml`).
5. On delete: finalizer blocks removal until EDA sets `status.deletionComplete: true`.

Heavy Ansible (Keycloak groups, ACM resources, cloud APIs) runs in EDA roles under `hybridcloud/eda/`.

---

## Namespace Operator Deployment

When the primary `entity` role reconciles an Entity CR, `deploy_namespace_operator.yml` creates:

| Resource | Name | Scope |
|----------|------|-------|
| Namespace | `entity-<name>` | Cluster |
| ServiceAccount | `hybridsovereign-namespace-operator` | Namespace |
| Role | `hybridsovereign-namespace-operator` | Namespace (`hybridsovereign.redhat/*`) |
| RoleBinding | `hybridsovereign-namespace-operator` | Namespace |
| Deployment | `hybridsovereign-namespace-operator` | Namespace |

The Deployment runs with `--watched-namespaces=entity-<name>` and a per-entity leader election ID.

---

## RBAC Model

| Tier | K8s RBAC | Rationale |
|------|----------|-----------|
| Primary | `ClusterRole` | Must create namespaces and deploy namespace operators |
| Namespace | `Role` (namespace-scoped) | Least privilege within `entity-<name>` |

Entity namespace RBAC (14 named roles for tenant users) is provisioned by EDA `entity_provision` role, not by the operator directly.

---

## Deployment

| Artifact | Path | Registry image |
|----------|------|----------------|
| Primary operator image | `hybridcloud/operator/primary/Dockerfile` | `hybridsovereign-primary-operator` |
| Namespace operator image | `hybridcloud/operator/namespace/Dockerfile` | `hybridsovereign-namespace-operator` |
| CRD bundle | `hybridcloud/operator/config/crd/bases/` | Embedded in primary Helm chart |
| Helm chart | `hybridcloud/operator/primary/helm/` | OCI via `bootstrap/make/upload-*` |

ArgoCD Application: `hybridsovereignPrimaryOperator` (sync-wave 38, services cluster).

---

## Related Documents

- [code/entity-lifecycle.md](../code/entity-lifecycle.md) — Entity state machine
- [event-system.md](event-system.md) — event pipeline to EDA
- [../decisions/ADR-002-multi-tier-operator.md](../../decisions/ADR-002-multi-tier-operator.md)
- [../technical/17-entity-operator.md](../../archive/technical/17-entity-operator.md)
