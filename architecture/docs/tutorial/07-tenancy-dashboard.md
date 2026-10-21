# Tutorial: Tenancy Dashboard

> **Superseded.** This page describes an earlier access model and earlier components. Use
> [Onboard a tenant](../../../docs/platform/onboard-a-tenant.md) and
> [Getting started](../../../docs/tenant/getting-started.md); the CRDs and `oc explain` are
> authoritative.

## Overview

The Tenancy Dashboard is the self-service UI for tenant administrators to manage teams, projects, assignments, plugins (Vault, AAP, Quay), and RBAC groups within their entity namespaces. It runs on the services cluster with full CRUD over all `hybridsovereign.redhat` CRs.

**URL:** `https://tenancy-dashboard.apps.services.lab.example.com`

---

## 1. Login Flow

Authentication follows the same OpenShift OAuth flow as the Sovereign Cloud Dashboard. The user's OAuth access token is forwarded to every Kubernetes API call — the dashboard never uses a static ServiceAccount token.

After login, the dashboard loads entity namespaces the user has access to (those labeled `hybridsovereign.redhat/entity`). A searchable sidebar lists available entities.

---

## 2. Navigation

The left sidebar lists all accessible entity namespaces. Click an entity to scope all views to that namespace. Resource sections:

| Section | CRs |
|---------|-----|
| Teams | `Team` |
| Assignments | `Assignment` |
| Projects | `Project` |
| PlatformOpenshift | `PlatformOpenshift` |
| CloudOSO | `CloudOSO` |
| RBAC | `Rbac`, `RbacConfig` |
| Vault | `Vault`, `VaultKV` |
| AAP | `AAPConfig`, `AAPOrg` |
| Quay | `QuayConfig`, `QuayOrg` |

---

## 3. Tenant Onboarding Flow

A complete tenant onboarding consists of creating: Entity → RBAC → Team → Project → Assignment. The dashboards split this: the Sovereign Cloud Dashboard manages Entities; the Tenancy Dashboard manages everything within an entity namespace.

### Step 1: Create the Entity (Sovereign Cloud Dashboard)

Use the [Sovereign Cloud Dashboard](06-user-dashboard.md) to create the `Entity` CR. This creates the `entity-<name>` namespace.

### Step 2: Create RBAC groups (Tenancy Dashboard)

Navigate to the entity namespace in the Tenancy Dashboard → **RBAC** → **New Rbac**.

```
Name: acme-developers
Config: default-rbac-config
Description: Developer group for Acme Corp
```

The Plugin RBAC operator creates the Keycloak group `acme-corp/acme-developers`. Users added to this group in Keycloak gain the associated access.

### Step 3: Create a Team

Navigate to **Teams** → **New Team**:

```
Name: platform-team
RbacConfig: default-rbac-config
Team Admin: [acme-developers]
Features: Istio: false, Argo: false
```

### Step 4: Create a Project

Navigate to **Projects** → **New Project**:

```
Name: web-project
RbacConfig: default-rbac-config
Project Admin: [acme-developers]
```

### Step 5: Create an Assignment

Navigate to **Assignments** → **New Assignment**:

```
Name: acme-main-assignment
Team: platform-team
Projects: [web-project]
OpenShift: [dev-cluster]   (PlatformOpenshift CR name)
```

The Assignment operator validates that `platform-team` and `web-project` are both `ready: True` before setting the assignment as ready.

---

## 4. Managing Vault for a Tenant

### Create a tenant Vault instance

Navigate to **Vault** → **New Vault**:

```
Name: acme-vault
HA: true
RbacConfig: default-rbac-config
```

The Plugin Vault operator:
1. Deploys a dedicated Vault in the entity namespace
2. Creates Keycloak OIDC client `vault-acme-corp-acme-vault`
3. Configures OIDC auth so `acme-corp/acme-developers` group has admin access

Check status in the YAML view: `status.vaultUrl` contains the Route URL.

### Create a KV secrets engine

Navigate to **Vault** → **New VaultKV**:

```
Name: acme-secrets
Vault: acme-vault
Admin RBAC: [acme-developers]
Reader RBAC: [acme-readers]
```

Tenant developers can now log in to their Vault instance at `status.loginUrl` using their Keycloak credentials.

---

## 5. Managing AAP and Quay Orgs

### Create AAP organization

Navigate to **AAP** → **New AAPOrg**:

```
Name: automation
Config: default-aap-config
Description: Automation for Acme Corp
```

Creates AAP org `acme-corp-automation`.

### Create Quay organization

Navigate to **Quay** → **New QuayOrg**:

```
Name: registry
Config: default-quay-config
```

Creates Quay org `acme-corp-registry`.

---

## 6. Day-2 Management

### Editing resources

Most resources support inline editing from the detail page (click the resource name to open). The **Assignment** resource supports inline editing of `team`, `projects`, and `openshift` fields without recreating the resource.

### Deleting resources

Use the delete button on list pages. The dashboard shows a confirmation dialog before deletion. Dependent resources (e.g., VaultKV before Vault) must be deleted first to avoid orphaned resources.

### YAML View

Every resource detail page includes a **YAML** tab showing the live Kubernetes YAML. Use this for quick inspection without needing `oc` CLI access.

---

## 7. Troubleshooting

### Entity namespace not visible in sidebar

The namespace may be missing the entity label:

```bash
oc label namespace entity-acme-corp \
  hybridsovereign.redhat/entity=acme-corp
```

### CR creation fails with "Forbidden"

The logged-in user does not have RBAC permissions for the CRD in that namespace. Ask the platform team to add your
access group to a role ([Onboard a tenant, step 4](../../../docs/platform/onboard-a-tenant.md#4-give-the-access-groups-roles)),
or to add you to an access group that holds one.

### Status stuck at `ready: False`

Check the operator logs for the relevant CR type:

```bash
oc logs -n sovereign-cloud -l name=team-operator --tail=50
```

Common causes:
- Referenced `Rbac` CR doesn't exist or is not ready
- Entity namespace missing label
- Keycloak unreachable from operator pod

### Dashboard 502 Bad Gateway

The backend pod may have restarted. Check:

```bash
oc get pods -n sovereign-cloud -l app=tenancy-dashboard
oc logs -n sovereign-cloud -l app=tenancy-dashboard -c dashboard --previous
```

OAuth secrets may need refreshing:

```bash
oc get externalsecret -n sovereign-cloud -l app=tenancy-dashboard
```

---

## Related Documentation

- [20-tenancy-dashboard.md](../technical/20-tenancy-dashboard.md) — Architecture reference
- [24-tenancy-operators.md](../archive/technical/24-tenancy-operators.md) — Tenancy operators (Team, Assignment, Project, etc.)
- [05-day2-plugins.md](05-day2-plugins.md) — Plugin operator operations
- [06-user-dashboard.md](06-user-dashboard.md) — Sovereign Cloud Dashboard (Entity management)
- [03-day2-operators.md](03-day2-operators.md) — General operator operations
