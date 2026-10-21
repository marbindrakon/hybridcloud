# Platform RBAC Model — Conceptual Overview

> **Superseded.** This page describes an earlier access model and earlier components. Use
> [Onboard a tenant](../../../docs/platform/onboard-a-tenant.md) and
> [Getting started](../../../docs/tenant/getting-started.md); the CRDs and `oc explain` are
> authoritative.

**Audience:** Platform administrators, architects, onboarding engineers

## What problem does this solve?

In a multi-tenant platform with 40+ customers, you need fine-grained control over
**who can do what in each tenant's namespace**. The Sovereign Cloud RBAC model gives
every tenant exactly the permissions they need — no more, no less — without requiring
manual Kubernetes or Keycloak configuration.

## Two-layer RBAC

```
Layer 1 (Entity Operator) — Kubernetes namespace RBAC
  Entity CR → K8s Roles + RoleBindings in entity-<name> namespace

Layer 2 (Plugin RBAC Operator) — Keycloak group hierarchy
  Rbac CR → Keycloak group entity-name/rbac-name
            ↓ status.group
  Entity CR references Rbac CRs → RoleBinding subjects = Keycloak groups
```

## The Named Roles

Every entity namespace can have up to 14 K8s Roles automatically managed by the
Entity operator. Each role maps to a specific responsibility:

| Role | What they can do |
|---|---|
| Entity Admin | Full control of all CRs in the namespace |
| Auditor | Read everything — cannot modify |
| CloudOSO Admin | Manage OpenStack cloud resources |
| CloudOSO Viewer | View OpenStack cloud resources |
| CloudAWS Admin | Manage AWS cloud resources |
| CloudAWS Viewer | View AWS cloud resources |
| PlatformOpenshift Admin | Manage OpenShift cluster deployments |
| PlatformOpenshift Viewer | View OpenShift cluster deployments |
| Team Admin | Manage team membership |
| Team Viewer | View team membership |
| Project Admin | Manage projects and assignments |
| Project Viewer | View projects and assignments |
| Assignment Admin | Manage assignments between entities |
| Identity Admin | Manage RBAC (Keycloak groups) only |

## How it works end-to-end

```
1. Create Entity CR → namespace entity-acme-corp created
2. Create Rbac CRs → Keycloak groups created (acme-corp/acme-platform-admins etc.)
3. Update Entity CR namespaceRbac → 14 K8s Roles created
                                  → RoleBindings bind Keycloak groups to Roles
4. User logs in via Keycloak → token includes Keycloak group membership
5. OpenShift maps Keycloak group → Kubernetes RBAC group → permissions enforced
```

## Security properties

- **No wildcard permissions** — each role grants access to exactly the resources it needs
- **Auditor is always read-only** — cannot create or modify any CR
- **RBAC CRs protected** — only EntityAdmin, IdentityAdmin, and role-specific admins can view Rbac CRs
- **Keycloak group lifecycle managed** — when an Entity or Rbac CR is deleted, the corresponding Keycloak group is removed automatically (finalizer)
- **Secrets never in Git** — Keycloak client credentials flow through Vault → ExternalSecret

## Scalability

Designed for production-scale tenants:
- 40+ entities
- 10,000+ RBAC group memberships per entity
- Role resolution is parallelized — no sequential blocking loops
