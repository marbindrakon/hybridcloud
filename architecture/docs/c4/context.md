# C4 Level 1 — System Context

**Scope**: Hybrid Sovereign Cloud (`hybridcloud/` monorepo)  
**API group**: `hybridsovereign.redhat/v1alpha1`  
**Last updated**: 2026-07-15

---

## Purpose

Hybrid Sovereign Cloud is a multi-tenant hybrid cloud management platform. Platform administrators onboard entities (tenants), provision OpenShift and cloud resources, and manage plugin integrations (AAP, Quay, Vault, RBAC). Tenant users self-service teams, projects, assignments, and cloud environments inside their entity boundary.

Runtime configuration is GitOps-only after bootstrap (ArgoCD on the central cluster). Secrets never live in Git; Vault + ExternalSecrets deliver credentials at runtime.

---

## System Context Diagram

```mermaid
C4Context
    title Hybrid Sovereign Cloud — System Context

    Person(platformAdmin, "Platform Administrator", "Onboards entities, manages plugin config CRs")
    Person(tenantAdmin, "Tenant Administrator", "Manages teams, projects, clouds within an entity")
    Person(tenantDev, "Tenant Developer", "Consumes assignments and cloud resources")

    System(hsc, "Hybrid Sovereign Cloud", "Operators, EDA, UI, bootstrap GitOps")

    System_Ext(idp, "Keycloak (RHBK)", "OIDC for dashboards, console, Vault")
    System_Ext(vault, "HashiCorp Vault", "Central secrets; no credentials in Git")
    System_Ext(quay, "Quay Registry", "OCI charts and container images")
    System_Ext(gitea, "Gitea", "CR snapshot / tenancy config mirror")
    System_Ext(clouds, "Cloud Providers", "AWS, OpenStack, ACM-managed OCP")
    System_Ext(vmware, "VMware vCenter", "Source inventory for VM migration")

    Rel(platformAdmin, hsc, "Entity / plugin config CRs", "K8s API / Admin UI")
    Rel(tenantAdmin, hsc, "Team / Project / Assignment CRs", "K8s API / Tenant UI")
    Rel(tenantDev, hsc, "View scoped resources", "K8s API / Console plugin")

    Rel(hsc, idp, "OAuth login, group sync", "OIDC")
    Rel(hsc, vault, "Pull / push secrets", "ExternalSecret / PushSecret")
    Rel(hsc, quay, "Pull images and Helm charts", "OCI / HTTPS")
    Rel(hsc, gitea, "Sync CR YAML snapshots", "HTTPS")
    Rel(hsc, clouds, "Provision and manage", "Cloud APIs / ACM")
    Rel(hsc, vmware, "Inventory and migrate VMs", "MTV / os-migrate")
```

---

## Actors

| Actor | Interaction | Auth |
|-------|-------------|------|
| Platform Administrator | Entity onboarding, `RbacConfig` / `AAPConfig` / `QuayConfig`, bootstrap | Keycloak via Admin Dashboard or OCP console plugin |
| Tenant Administrator | Team, Project, Assignment, network and cloud environment CRs in `entity-<name>` | Keycloak; the roles of the tenant's access groups ([role table](../../../docs/platform/onboard-a-tenant.md#4-give-the-access-groups-roles)) |
| Tenant Developer | Scoped CRUD / read per RoleBinding | Keycloak |

---

## External systems

| System | Role | Monorepo path |
|--------|------|---------------|
| Keycloak (RHBK) | SSO for UI, console, Vault OIDC | `bootstrap/helm/charts/rhbk/`, sovereign Jobs |
| Vault | Credential store (HA Raft, both clusters) | `bootstrap/helm/charts/vault*` |
| Quay | Image + Helm chart registry | `bootstrap/make/upload-*.mk` |
| Gitea | `tenancy_repo` + cluster-builds | `bootstrap/helm/charts/gitea/`, `iaac/` |
| RHACM | Managed cluster / spoke lifecycle | `bootstrap/helm/charts/rhacm/` |
| AMQ Streams | Cross-cluster event bus | `bootstrap/helm/charts/amq-streams/` |
| AAP + EDA | Job execution and rulebook activations | `aap-config/`, `eda/` |
| Cloud providers | AWS, OpenStack, managed OCP | `operator/namespace/` roles |

---

## Cluster topology

| Cluster | Lab API | Management role |
|---------|---------|-----------------|
| Central | `api.central.lab.example.com` | Sole ArgoCD control plane; RHACM; Vault; Gitea; Kafka; AAP **Controller + EDA** |
| Services | `api.services.lab.example.com` | All `hybridsovereign.redhat` operators, UIs, tenant CRs; AAP Controller only |

Central ArgoCD deploys to both clusters via `destination.server`. The services cluster must not host Application / ApplicationSet management resources.

---

## Monorepo layout

| Path | Responsibility |
|------|----------------|
| `bootstrap/` | ArgoCD app-of-apps, init chart, platform OCI charts |
| `operator/primary/` | Primary operator (Entity + plugin configs) |
| `operator/namespace/` | Per-entity namespace operator |
| `eda/` | Rulebooks and decision environments |
| `iaac/` | Python StatefulSet — CR → Gitea sync |
| `ui/` | PatternFly dashboards + console plugins |
| `aap-config/` | AAP/EDA config-as-code |
| `migration/` | VMware → CloudOSO migration playbooks |
| `samples/` | Sanitized sample CRs |
| `specs/` | Feature specifications `001`–`034` |
| `architecture/` | This C4 documentation set |

---

## Related

- [containers.md](containers.md) — L2
- [components/operator.md](components/operator.md) — operator tiers
- [../decisions/ADR-001-monorepo.md](../decisions/ADR-001-monorepo.md)
