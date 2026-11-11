# PlatformOpenshift Operator — Keycloak OIDC Integration

## Overview

Every `PlatformOpenshift` CR that reaches `status.clusterInstalled: true` automatically gets a Keycloak OIDC client created and an ACM `ConfigurationPolicy` pushed to configure SSO on the spoke cluster.

## Implementation

### Task file: `keycloak_oidc.yml`

Located at: `PlatformOpenshift/operator/roles/platformopenshift/tasks/keycloak_oidc.yml`

**Flow:**
1. Read Keycloak admin credentials from Secret `rhbk-admin-credentials` (populated via ExternalSecret from Vault)
2. Obtain Keycloak admin token from `KEYCLOAK_INTERNAL_URL/realms/master`
3. Create OIDC client `{cluster-name}-keycloak-oidc` in `sovereign-tenants` realm
4. Add `groups` protocol mapper (propagates Keycloak groups to JWT `groups` claim)
5. Build ACM `Policy`, `Placement`, `PlacementBinding` YAML
6. POST resources to central cluster API using `osohelper-creator-sa` token

### Operator Environment Variables

| Variable | Default | Description |
|---|---|---|
| `KEYCLOAK_INTERNAL_URL` | `http://rhbk-services-service.rhbk.svc:8080` | Internal Keycloak URL |
| `KEYCLOAK_EXTERNAL_URL` | `https://rhbk-services.apps.services.lab.example.com` | External Keycloak URL for OIDC issuer |
| `KEYCLOAK_REALM` | `sovereign-tenants` | Keycloak realm |

### RBAC Requirements

The `osohelper-creator-sa` ServiceAccount on the central cluster requires:
```yaml
- apiGroups: [policy.open-cluster-management.io]
  resources: [policies, placementbindings]
  verbs: [create, delete, get, list, watch, update, patch]
- apiGroups: [cluster.open-cluster-management.io]
  resources: [placements]
  verbs: [create, delete, get, list, watch, update, patch]
```

This cross-cluster ServiceAccount configuration is managed via the Helm chart for the relevant operator.

### ACM Resources Created

| Resource | Name | Namespace |
|---|---|---|
| Policy | `{acm-base}-keycloak-oidc` | `openshift-gitops` |
| Placement | `{acm-base}-keycloak-oidc-placement` | `openshift-gitops` |
| PlacementBinding | `{acm-base}-keycloak-oidc-binding` | `openshift-gitops` |

`{acm-base}` is the cluster's build name when the policy names fit ACM's limit (the policy namespace and name
together at most 62 characters), else the head of the build name and 8 hex digits of its SHA-256 (at most 32
characters). The cluster RBAC policy (`{acm-base}-cluster-rbac` and its placement and binding) follows the same rule.

The Policy uses `Placement.matchLabels.name: {cluster-name}` to target the correct ManagedCluster.

### OAuth Config Pushed to Spoke

```yaml
apiVersion: config.openshift.io/v1
kind: OAuth
metadata:
  name: cluster
spec:
  identityProviders:
    - name: keycloak-sso
      type: OpenID
      openID:
        clientID: "{cluster-name}-keycloak-oidc"
        clientSecret:
          name: openid-client-secret-keycloak
        extraScopes: [profile, email]
        claims:
          preferredUsername: [preferred_username]
          name: [name]
          email: [email]
          groups: [groups]
        issuer: "https://rhbk-services.apps.services.lab.example.com/realms/sovereign-tenants"
```

The OIDC client secret is also pushed as a Kubernetes `Secret` named `openid-client-secret-keycloak` in `openshift-config` namespace via the ACM ConfigurationPolicy.

### PlatformOpenshift Status Fields

| Field | Type | Description |
|---|---|---|
| `status.oidcConfigured` | boolean | Whether OIDC client and ACM policy have been created |
| `status.oidcClientId` | string | The Keycloak OIDC client ID for this cluster |

## Hardening Notes

- Client secrets are stored in Kubernetes Secrets (not ConfigMaps) on the spoke cluster
- Keycloak admin credentials pulled from Vault via ExternalSecret (not hardcoded)
- `no_log: true` on all tasks handling credentials
- OIDC client uses `service-accounts-enabled: false` (no machine auth via OIDC)
- Redirect URIs scoped to cluster console and OAuth callback URLs only
