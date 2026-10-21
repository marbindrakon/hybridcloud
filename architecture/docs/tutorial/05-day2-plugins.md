# Day-2 Operations: Plugin Operators

> **Superseded.** This page describes an earlier access model and earlier components. Use
> [Onboard a tenant](../../../docs/platform/onboard-a-tenant.md) and
> [Getting started](../../../docs/tenant/getting-started.md); the CRDs and `oc explain` are
> authoritative.

## Overview

Plugin operators run in `sovereign-cloud-plugins` on the services cluster. They manage per-entity resources (Keycloak groups, Vault instances, AAP orgs, Quay orgs) tied to entity namespaces.

| Operator | CRDs | Purpose |
|----------|------|---------|
| Plugin RBAC | `RbacConfig`, `Rbac` | Keycloak OIDC clients and groups |
| Plugin Vault | `Vault`, `VaultKV` | Per-tenant Vault instances and KV engines |
| Plugin AAP | `AAPConfig`, `AAPOrg` | AAP organizations per entity |
| Plugin Quay | `QuayConfig`, `QuayOrg` | Quay registry organizations per entity |
| Plugin SDX | `Iaac` | CR-to-Gitea sync (tenancy state export) |

**Naming rule:** Entity CRs in entity namespaces are prefixed with the entity name. e.g., `AAPOrg` named `automation` in `entity-acme-corp` → AAP org `acme-corp-automation`.

---

## 1. Plugin RBAC

### RbacConfig (singleton per cluster)

The `RbacConfig` CR in `sovereign-cloud-plugins` connects the operator to Keycloak:

```yaml
apiVersion: hybridsovereign.redhat/v1alpha1
kind: RbacConfig
metadata:
  name: default-rbac-config
  namespace: sovereign-cloud-plugins
spec:
  keycloakUrl: "https://rhbk-services.apps.services.lab.example.com"
  realm: "sovereign-tenants"
  adminSecretName: "rhbk-services-admin"   # ExternalSecret from Vault
  adminSecretNamespace: "sovereign-cloud-plugins"
```

### Rbac (per entity, per group)

```yaml
apiVersion: hybridsovereign.redhat/v1alpha1
kind: Rbac
metadata:
  name: acme-developers
  namespace: entity-acme-corp
  annotations:
    created-by: admin-user
spec:
  config: default-rbac-config
  description: "Developer group for Acme Corp"
```

The operator creates a Keycloak group `acme-corp/acme-developers` in the `sovereign-tenants` realm. The `status.group` field contains the full group path.

```bash
# Check Rbac status
oc get rbac acme-developers -n entity-acme-corp -o yaml | grep -A 10 status
```

### Troubleshooting RBAC

```bash
# Operator logs
oc logs -n sovereign-cloud-plugins -l name=plugin-rbac --tail=50

# Common issue: entity namespace missing label
oc get namespace entity-acme-corp -o jsonpath='{.metadata.labels}'
# Required: hybridsovereign.redhat/entity: acme-corp

# Keycloak connectivity
oc exec -n sovereign-cloud-plugins deploy/plugin-rbac -- \
  curl -sk https://rhbk-services.apps.services.lab.example.com/realms/sovereign-tenants \
  | jq .realm
```

---

## 2. Plugin Vault

### Creating a tenant Vault instance

```yaml
apiVersion: hybridsovereign.redhat/v1alpha1
kind: Vault
metadata:
  name: acme-vault
  namespace: entity-acme-corp
spec:
  ha: true
  rbacConfig: default-rbac-config
```

The operator:
1. Deploys a Vault StatefulSet in the entity namespace
2. Initializes Vault and stores the root token in a Secret
3. Creates a Keycloak OIDC client `vault-acme-corp-acme-vault`
4. Configures OIDC auth on the new Vault instance

```bash
# Check Vault deployment status
oc get vault acme-vault -n entity-acme-corp -o yaml | grep -A 20 status

# Access the tenant Vault UI
oc get route -n entity-acme-corp | grep vault
```

### Creating a KV engine

```yaml
apiVersion: hybridsovereign.redhat/v1alpha1
kind: VaultKV
metadata:
  name: acme-secrets
  namespace: entity-acme-corp
spec:
  vault: acme-vault
  vaultAdminRbac:
    - acme-developers
  vaultReaderRbac:
    - acme-readers
```

The operator creates a KV v2 engine and RBAC policies. Members of `acme-corp/acme-developers` group in Keycloak get admin access; `acme-corp/acme-readers` get read-only access.

### Troubleshooting Plugin Vault

```bash
# Check operator logs
oc logs -n sovereign-cloud-plugins -l name=plugin-vault --tail=50

# Vault CR not reconciling — check entity label
oc get namespace entity-acme-corp --show-labels

# OIDC client creation failing — check Keycloak connectivity
oc exec -n sovereign-cloud-plugins deploy/plugin-vault -- \
  curl -sk "https://rhbk-services.apps.services.lab.example.com/health/live" | jq .
```

---

## 3. Plugin AAP

### AAPConfig (singleton per cluster)

```yaml
apiVersion: hybridsovereign.redhat/v1alpha1
kind: AAPConfig
metadata:
  name: default-aap-config
  namespace: sovereign-cloud-plugins
spec:
  url: "https://aap.apps.services.lab.example.com"
  adminSecretName: "aap-admin-credentials"
  adminSecretNamespace: "sovereign-cloud-plugins"
```

### AAPOrg (per entity)

```yaml
apiVersion: hybridsovereign.redhat/v1alpha1
kind: AAPOrg
metadata:
  name: automation
  namespace: entity-acme-corp
spec:
  config: default-aap-config
  description: "Automation org for Acme Corp"
```

This creates an AAP organization named `acme-corp-automation`.

```bash
# Verify AAPOrg status
oc get aaporg automation -n entity-acme-corp -o jsonpath='{.status}'
```

---

## 4. Plugin Quay

### QuayConfig (singleton per cluster)

```yaml
apiVersion: hybridsovereign.redhat/v1alpha1
kind: QuayConfig
metadata:
  name: default-quay-config
  namespace: sovereign-cloud-plugins
spec:
  url: "https://quay.example.com"
  adminSecretName: "quay-admin-credentials"
  adminSecretNamespace: "sovereign-cloud-plugins"
```

### QuayOrg (per entity)

```yaml
apiVersion: hybridsovereign.redhat/v1alpha1
kind: QuayOrg
metadata:
  name: registry
  namespace: entity-acme-corp
spec:
  config: default-quay-config
```

This creates a Quay organization named `acme-corp-registry`.

> **Known issue:** Quay app pods may be in `Pending` state due to cluster memory exhaustion. If `QuayOrg` CRs fail to reconcile, check Quay pod status first: `oc get pods -n quay-enterprise`.

---

## 5. Plugin SDX

SDX is deployed as a singleton controller in `sovereign-cloud-plugins`. The `Iaac` CR anchors sync status; `spec` is reserved for future configuration.

```yaml
apiVersion: hybridsovereign.redhat/v1alpha1
kind: Iaac
metadata:
  name: sdx-sync
  namespace: sovereign-cloud-plugins
spec: {}
```

```bash
oc get iaac sdx-sync -n sovereign-cloud-plugins -o jsonpath='{.status.ready}{"\n"}{.status.totalCRsSynced}{"\n"}'
```

---

## 6. General Operator Lifecycle

### Upgrade a plugin operator

1. Build and push new image: `make docker-build docker-push` from operator dir
2. Bump chart version: `helm/Chart.yaml` → `appVersion` and `version`
3. Push chart: `make upload-plugin-rbac-chart` (from `bootstrap/`)
4. Update `bootstrap/helm/central/values.yaml`: bump `chartVersion`
5. Commit, push — ArgoCD deploys

### Force re-reconcile a CR

```bash
oc annotate rbac acme-developers -n entity-acme-corp \
  force-sync=$(date +%s) --overwrite
```

### Check reconciliation metrics

```bash
# Port-forward to metrics endpoint
oc port-forward -n sovereign-cloud-plugins svc/plugin-rbac-metrics 8443:8443
curl -sk https://localhost:8443/metrics | grep controller_runtime_reconcile
```

---

## Related Documentation

- [19-plugin-rbac.md](../technical/19-plugin-rbac.md) — Plugin RBAC reference
- [21-plugin-vault.md](../technical/21-plugin-vault.md) — Plugin Vault reference
- [22-plugin-aap.md](../technical/22-plugin-aap.md) — Plugin AAP reference
- [23-plugin-quay.md](../technical/23-plugin-quay.md) — Plugin Quay reference
- [25-plugin-iaac.md](../archive/technical/25-plugin-iaac.md) — Plugin SDX reference
- [03-day2-operators.md](03-day2-operators.md) — General operator operations
