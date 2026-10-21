# Developer Tutorial — Entity RBAC Configuration

> **Superseded.** This page describes an earlier access model and earlier components. Use
> [Onboard a tenant](../../../docs/platform/onboard-a-tenant.md) and
> [Getting started](../../../docs/tenant/getting-started.md); the CRDs and `oc explain` are
> authoritative.

**Goal:** Get a working Entity with its RBAC roles configured in ≤ 30 minutes.

**Prerequisites:**
- `oc` access to the services cluster
- `keycloak-admin` role in the Keycloak `sovereign-tenants` realm
- A running Entity operator (check: `oc get pods -n sovereign-cloud | grep entity`)
- A running Plugin RBAC operator (check: `oc get pods -n sovereign-cloud-plugins | grep plugin-rbac`)

---

## Step 1 — Create the entity namespace Rbac CRs

For each RBAC role you want to assign, create a `Rbac` CR in the entity namespace.
Each Rbac CR creates one Keycloak group at `<entity-name>/<rbac-cr-name>`.

```yaml
# Create 3 Rbac CRs for acme-corp entity
# (repeat for each group you need)

apiVersion: hybridsovereign.redhat/v1alpha1
kind: Rbac
metadata:
  name: acme-platform-admins
  namespace: entity-acme-corp
spec:
  config: keycloak-sovereign-tenants-services
  description: "Full entity control"
---
apiVersion: hybridsovereign.redhat/v1alpha1
kind: Rbac
metadata:
  name: acme-auditors
  namespace: entity-acme-corp
spec:
  config: keycloak-sovereign-tenants-services
  description: "Read-only audit access"
---
apiVersion: hybridsovereign.redhat/v1alpha1
kind: Rbac
metadata:
  name: acme-devs
  namespace: entity-acme-corp
spec:
  config: keycloak-sovereign-tenants-services
  description: "Developer team access"
```

Wait for all Rbac CRs to show `ready: true`:

```bash
oc get rbac -n entity-acme-corp -w
```

---

## Step 2 — Verify Keycloak groups were created

```bash
# Check Rbac CR status.group
oc get rbac acme-platform-admins -n entity-acme-corp -o jsonpath='{.status.group}'
# Expected: acme-corp/acme-platform-admins
```

In the Keycloak admin console, navigate to:
`Realm: sovereign-tenants → Groups → acme-corp → acme-platform-admins`

---

## Step 3 — Update the Entity CR with namespaceRbac

Now reference the Rbac CR names in the Entity's `namespaceRbac` spec.
Each key maps to a list of Rbac CR names whose Keycloak group will be bound.

```yaml
apiVersion: hybridsovereign.redhat/v1alpha1
kind: Entity
metadata:
  name: acme-corp
  namespace: sovereign-cloud
spec:
  description: "ACME Corporation"
  billingID: "ACME-2024-001"
  websiteLink: "https://acme-corp.example.com"
  namespaceRbac:
    entityAdmin:
      - acme-platform-admins
    auditor:
      - acme-auditors
    cloudAWSAdmin:
      - acme-platform-admins
    cloudAWSView:
      - acme-devs
    teamAdmin:
      - acme-devs
    projectAdmin:
      - acme-devs
    identityAdmin:
      - acme-platform-admins
```

---

## Step 4 — Verify K8s Roles and RoleBindings

```bash
# List all Roles created in the entity namespace
oc get roles -n entity-acme-corp

# Expected output includes:
# entity-admin
# auditor
# cloudaws-admin
# cloudaws-view
# team-admin
# project-admin
# identity-admin

# Check RoleBindings
oc get rolebindings -n entity-acme-corp
```

---

## Step 5 — Add a user to a Keycloak group

In Keycloak admin console:
1. Navigate to `Realm: sovereign-tenants → Users`
2. Find or create your test user
3. Go to user → Groups → Join Group
4. Select `acme-corp/acme-platform-admins`

Or use the user_dashboard: `Entities → acme-corp → RBAC → Entity Admin → Add`

---

## Step 6 — Verify access

```bash
# Login as the test user (oc login with Keycloak SSO)
oc login --token=<user-token> --server=<services-cluster-api>

# Verify they can list resources they should see
oc get cloudawss -n entity-acme-corp  # ✓ should work for cloudAWSView

# Verify they cannot access what they shouldn't
oc get cloudawss -n entity-globex-industries  # ✗ should be forbidden
```

---

## Cleanup / Delete flow

To remove a role binding, remove the Rbac CR name from the Entity `namespaceRbac` spec.
To remove the entire group, delete the Rbac CR — the finalizer will remove the Keycloak group.

```bash
oc delete rbac acme-platform-admins -n entity-acme-corp
# Finalizer runs: Keycloak group acme-corp/acme-platform-admins deleted
# If this was the last Rbac CR: parent group acme-corp also deleted
```

---

## Troubleshooting

| Symptom | Check |
|---|---|
| Rbac CR stuck not Ready | `oc describe rbac <name> -n <ns>` — check events for Keycloak errors |
| RoleBinding has no subjects | Rbac CR `status.group` is empty — check plugin-rbac pod logs |
| User has no access | Confirm user is in the Keycloak group (`oc get rbac <name> -n <ns> -o yaml`) |
| Parent group missing | Check if `RbacConfig` is Ready: `oc get rbacconfig -n sovereign-cloud-plugins` |
