# 41 — Two-Layer RBAC Design

> **Superseded.** This page describes an earlier access model and earlier components. Use
> [Onboard a tenant](../../../docs/platform/onboard-a-tenant.md) and
> [Getting started](../../../docs/tenant/getting-started.md); the CRDs and `oc explain` are
> authoritative.

## Overview

Sovereign Cloud enforces two distinct RBAC layers for every tenant:

| Layer | Name | Where Enforced | What It Controls |
|---|---|---|---|
| 1 | Namespace RBAC | Kubernetes (entity namespace) | Who can create/view each CR type |
| 2 | Tool RBAC | Underlying platform (OSO/AWS/OCP/Vault/AAP) | Who has which role in the provisioned tool |

---

## Layer 1 — Namespace RBAC (Entity Operator)

### Overview

Layer 1 is configured in `spec.namespaceRbac` of the `Entity` CR. The Entity operator reads this field during reconciliation and creates:

- A **creator** `Role` with `[get, list, watch, create, update, patch, delete]` verbs for each resource type  
- A **viewer** `Role` with `[get, list, watch]` for each resource type
- A **RoleBinding** mapping Keycloak group paths (via Rbac CR `status.group`) to each role

The `entityAdmin` list is automatically prepended to every creator binding.

### Resource Types Covered

`cloudosos`, `cloudawss`, `platformopenshifts`, `vaultkvs`, `vaults`, `quayorgs`, `aaporgs`, `projects`, `teams`, `assignments`, `rbacs`

### Entity CR Example

```yaml
spec:
  namespaceRbac:
    entityAdmin: [platform-admins]
    cloudOSO:
      creators: [infra-team]
      viewers: [devs, viewers]
    platformOpenshift:
      creators: [platform-admins, infra-team]
      viewers: [devs]
    vaultKV:
      creators: [platform-admins, devs]
      viewers: [viewers]
```

### How Group Paths Are Resolved

Each entry is an Rbac CR name. The operator looks up the Rbac CR and reads `status.group`, which contains the Keycloak group path `/<entity-name>/<rbac-name>`. This path is used as the Kubernetes `Group` subject in RoleBindings, enabling SSO-to-RBAC linkage without any user/group sync.

### Kubernetes Role/RoleBinding Structure

```
Role: cloudosos-creator  → verbs: [get, list, watch, create, update, patch, delete]
Role: cloudosos-viewer   → verbs: [get, list, watch]
RoleBinding: cloudosos-creator → subjects: [Group: acme-corp.platform-admins, Group: acme-corp.infra-team]
RoleBinding: cloudosos-viewer  → subjects: [Group: acme-corp.devs]
# OpenShift group names are <tenant>.<access group>; <tenant>/<access group> is the sign-in directory path.
```

---

## Layer 2 — Tool RBAC (Plugin/Operator)

Layer 2 is configured in `spec.toolRbac` on each provisioned resource CR. The responsible operator reads the RBAC groups and applies roles within the underlying platform.

### VaultKV — 4-Tier Vault Policies

| Field | Policy Name | Capabilities |
|---|---|---|
| `vaultAdminRbac` | `{kv}-admin` | Full CRUD + list + metadata delete |
| `vaultOpsRbac` | `{kv}-ops` | CRUD data + delete/list metadata |
| `vaultDeveloperRbac` | `{kv}-developer` | Create + read data, read metadata |
| `vaultReaderRbac` | `{kv}-reader` | Read + list data and metadata |

### AAPOrg — 3-Tier AAP Teams

| Field | AAP Team | Roles Granted |
|---|---|---|
| `aapAdminRbac` | `{org}-admins` | Project Admin, Inventory Admin, Template Admin, Execute, Auditor |
| `aapJobExecutorRbac` | `{org}-executors` | Execute |
| `aapViewerRbac` | `{org}-viewers` | Auditor (read-only) |

### QuayOrg — 3-Tier (Existing, Unchanged)

| Field | Quay Role |
|---|---|
| `quayAdminRbac` | Organization Admin |
| `quayCreatorRbac` | Creator |
| `quayMemberRbac` | Member |

### PlatformOpenshift — 4-Tier Cluster RBAC

Pushed via ACM ConfigurationPolicy → ClusterRoleBinding on the spoke cluster:

| Field | ClusterRole |
|---|---|
| `clusterAdminRbac` | `cluster-admin` |
| `clusterOperatorRbac` | `self-provisioner` |
| `clusterDeveloperRbac` | `edit` |
| `clusterViewerRbac` | `view` |

### CloudOSO / CloudAWS (Deferred)

Fields `toolRbac.projectAdminRbac/projectMemberRbac/projectViewerRbac` (CloudOSO) and `accountAdminRbac/accountPoweruserRbac/accountViewerRbac` (CloudAWS) are present in CRDs but operator implementation is deferred pending platform IAM integration testing.

---

## Permission Matrix

```mermaid
graph TD
  subgraph "Layer 1 (K8s Namespace)"
    EA[entityAdmin] -->|creator binding| ALL[All resource types]
    C[creators] -->|Role: {resource}-creator| CRUD[get/list/watch/create/update/patch/delete]
    V[viewers] -->|Role: {resource}-viewer| RONLY[get/list/watch]
  end

  subgraph "Layer 2 (Tool RBAC)"
    VK[VaultKV] --> VA[vaultAdminRbac → admin policy]
    VK --> VO[vaultOpsRbac → ops policy]
    VK --> VD[vaultDeveloperRbac → developer policy]
    VK --> VR[vaultReaderRbac → reader policy]
    AAP[AAPOrg] --> AA[aapAdminRbac → admin team]
    AAP --> AE[aapJobExecutorRbac → executor team]
    AAP --> AV[aapViewerRbac → viewer team]
    OCP[PlatformOpenshift] --> OA[clusterAdminRbac → cluster-admin]
    OCP --> OO[clusterOperatorRbac → self-provisioner]
    OCP --> OD[clusterDeveloperRbac → edit]
    OCP --> OV[clusterViewerRbac → view]
  end
```

---

## Rbac CR Group Path Resolution

All RBAC fields accept **Rbac CR names** (not direct group names). This indirection:

1. Allows multiple CRs to reference the same Keycloak group
2. Decouples platform roles from Keycloak group path format
3. Enables the UI to offer a group selector without knowing group paths

Resolution flow:

```
Rbac CR name → lookup CR status.group → Keycloak group path (/entity/rbac-name)
```

---

## Dashboard Integration

### Layer 1 RBAC via Dashboard

The `Entity` create/edit flow in tenancy dashboard exposes `namespaceRbac` as a collapsible section with `RbacGroupSelector` chips for each resource type.

### Layer 2 RBAC via Dashboard

Every Create form (CloudOSO, CloudAWS, PlatformOpenshift, VaultKV, AAPOrg, QuayOrg, Team, Project, Assignment) includes an **Access** step in the multi-step stepper form, using `RbacGroupSelector` to choose groups for each role tier.

### Permissions Endpoint

`GET /api/permissions?namespace=<ns>` returns a JSON map of `{ resource: { list, create, delete } }` based on `SelfSubjectAccessReview` calls made with the user's forwarded token.

The frontend `usePermissions` hook consumes this to:
- Hide nav items for resources the user cannot list
- Disable or hide Create buttons for resources the user cannot create
- Show/hide edit buttons based on patch permissions

---

## Hardening Checklist

- [x] No credentials stored in CRD spec fields — all secrets via Vault + ExternalSecret
- [x] Operator uses `no_log: true` for all credential variables  
- [x] `entityAdmin` auto-included in creator bindings — no accidental lockout
- [x] Rbac CR indirection — group paths not hardcoded in CR manifests
- [x] ACM PolicyTemplate enforcement mode for spoke cluster RBAC
- [x] SelfSubjectAccessReview checks use the user's own forwarded token (no privilege escalation)
- [ ] CloudOSO/CloudAWS IAM wiring — deferred (CRD fields present, operator logic pending)
- [ ] Audit logging for group-to-role changes — planned for next iteration

---

## Related Documents

- [39-rbac-design.md](39-rbac-design.md) — Original RBAC design (Layer 1 only)
- [40-platformopenshift-oidc.md](40-platformopenshift-oidc.md) — OIDC client creation per cluster
