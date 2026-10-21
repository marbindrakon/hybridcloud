# Day-2 Operations: HashiCorp Vault

## Overview

This guide covers day-2 lifecycle management for the two Vault instances in Sovereign Cloud: `vault-central` (central cluster) and `vault-services` (services cluster). Both run in HA Raft mode (3 replicas) in the `vault` namespace.

## Prerequisites

- `oc` CLI logged into the central cluster as `sovereign-admin`
- `vault` CLI installed locally
- Access to the Vault UI at the OpenShift Route

---

## 1. Checking Vault Status

```bash
# Check vault-central pods
oc get pods -n vault -l app.kubernetes.io/name=vault

# Check raft cluster status
oc exec -n vault vault-0 -- vault operator raft list-peers

# Check seal status
oc exec -n vault vault-0 -- vault status
```

Expected output: `Initialized: true`, `Sealed: false`, `HA Mode: active` on the leader pod.

---

## 2. Unseal after Restart

Vault HA Raft **automatically unseals** using the stored Raft backend on restart, as long as at least 2 of 3 replicas are healthy. Manual unseal is only needed if all replicas restart simultaneously and the Raft backend state is lost.

### Manual Unseal (emergency only)

```bash
# Retrieve unseal key from vault-central namespace
UNSEAL_KEY=$(oc get secret vault-init-secrets-copy -n vault-central \
  -o jsonpath='{.data.unseal_key_1}' | base64 -d)

# Unseal all pods
for pod in vault-0 vault-1 vault-2; do
  oc exec -n vault $pod -- vault operator unseal "$UNSEAL_KEY"
done
```

---

## 3. Logging In via OIDC

```bash
# Set Vault address
export VAULT_ADDR=https://vault-central.apps.central.lab.example.com

# Login via OIDC (opens browser)
vault login -method=oidc role=sovereign-admin

# Verify identity
vault token lookup
```

This uses your Keycloak `sovereign-admin` group membership to obtain a Vault token with full access.

---

## 4. Adding New KV Secrets

All platform secrets live under the `central/` KV v2 engine at `central/data/<path>`.

```bash
# Write a new secret
vault kv put central/data/my-component \
  username="admin" \
  password="$(openssl rand -base64 24)"

# Read it back
vault kv get central/data/my-component

# List all secrets in central/
vault kv list central/
```

### Using ExternalSecret to deliver the new secret

After writing to Vault, create an `ExternalSecret` in the consuming namespace:

```yaml
apiVersion: external-secrets.io/v1beta1
kind: ExternalSecret
metadata:
  name: my-component-secret
  namespace: my-namespace
spec:
  refreshInterval: 1h
  secretStoreRef:
    name: vault-backend
    kind: ClusterSecretStore
  target:
    name: my-component-secret
  data:
    - secretKey: username
      remoteRef:
        key: central/data/my-component
        property: username
    - secretKey: password
      remoteRef:
        key: central/data/my-component
        property: password
```

---

## 5. Policy Management

### Create a custom policy

```hcl
# my-component-policy.hcl
path "central/data/my-component" {
  capabilities = ["read"]
}
path "central/metadata/my-component" {
  capabilities = ["read", "list"]
}
```

```bash
vault policy write my-component-policy my-component-policy.hcl
vault policy read my-component-policy
vault policy list
```

### Assign policy to a Kubernetes service account role

```bash
vault write auth/kubernetes-central/role/my-component \
  bound_service_account_names=my-sa \
  bound_service_account_namespaces=my-namespace \
  policies=my-component-policy \
  ttl=1h
```

---

## 6. Root Token Management

The root token is stored in `vault-init-secrets` (vault namespace) and in `vault-init-secrets-copy` (vault-central namespace) as a **post-init bootstrap bridge**. It must not be used for day-2 operations.

```bash
# Retrieve root token (emergency only)
ROOT_TOKEN=$(oc get secret vault-init-secrets -n vault \
  -o jsonpath='{.data.root_token}' | base64 -d)

# Check token capabilities
VAULT_TOKEN=$ROOT_TOKEN vault token lookup
```

> **Best practice:** Revoke the root token after completing OIDC configuration. Generate a new one only if the OIDC auth method becomes unavailable (use `vault operator generate-root`).

---

## 7. Kubernetes Auth Troubleshooting

### ESO ClusterSecretStore not Ready

```bash
# Check ClusterSecretStore status
oc get clustersecretstore vault-backend -o yaml | grep -A 20 status

# Check ESO SA token
oc get sa external-secrets-vault-sa -n external-secrets
oc describe clusterrolebinding external-secrets-vault-crb

# Test Vault k8s auth manually
SA_TOKEN=$(oc create token external-secrets-vault-sa -n external-secrets)
vault write auth/kubernetes-central/login \
  role=external-secrets \
  jwt=$SA_TOKEN
```

### ExternalSecret failing

```bash
# Check ExternalSecret events
oc describe externalsecret <name> -n <namespace>

# Look for specific error in ESO logs
oc logs -n external-secrets -l app.kubernetes.io/name=external-secrets | tail -50
```

Common causes:
- KV path does not exist yet in Vault (write the secret first)
- `ClusterSecretStore` not healthy (check Kubernetes auth mount)
- Wrong `property` name (use `vault kv get` to see exact keys)

---

## 8. Rotating a Secret

```bash
# Update the secret in Vault (creates a new version in KV v2)
vault kv patch central/data/my-component \
  password="$(openssl rand -base64 24)"

# Force ExternalSecret immediate refresh
oc annotate externalsecret my-component-secret -n my-namespace \
  force-sync=$(date +%s) --overwrite
```

ESO will automatically refresh the Kubernetes Secret within the configured `refreshInterval`.

---

## 9. Vault UI Access

| Vault | URL | Auth Method |
|-------|-----|-------------|
| vault-central | `https://vault-central.apps.central.lab.example.com/ui` | OIDC (sovereign-admin) |
| vault-services | `https://vault-services.apps.services.lab.example.com/ui` | OIDC (sovereign-admin) |

---

## Related Documentation

- [09-vault.md](../technical/09-vault.md) — Architecture reference
- [09-vault.md](../technical/09-vault.md) — Secrets flow overview
- [31-keycloak-vault-integration.md](../technical/31-keycloak-vault-integration.md) — Keycloak OIDC auth details
