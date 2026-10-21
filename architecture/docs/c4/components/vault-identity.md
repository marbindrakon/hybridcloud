# C4 Level 3 — Secrets & Identity

**Scope**: Vault, External Secrets, Keycloak, OAuth proxies  
**Last updated**: 2026-07-15

---

## Purpose

All credentials live in Vault. Clusters receive them via ExternalSecret / PushSecret. Humans authenticate with Keycloak OIDC. No secrets in Git.

---

## Component diagram

```mermaid
C4Component
    title Secrets and Identity

    Container_Boundary(central, "Central") {
        Component(vaultC, "Vault Central", "HA Raft x3", "KV mount central/")
        Component(esoC, "ESO", "OLM", "ClusterSecretStore vault-backend")
        Component(kcC, "Keycloak Central", "RHBK", "realm sovereign-central")
        Component(jobs, "Sovereign Jobs", "Ansible", "vault-init, OIDC, k8s auth")
    }

    Container_Boundary(services, "Services") {
        Component(vaultS, "Vault Services", "HA Raft x3", "Local Vault instance")
        Component(esoS, "ESO", "OLM", "Reads vault-central via ClusterSecretStore")
        Component(kcS, "Keycloak Services", "RHBK", "realm sovereign-tenants")
        Component(dash, "Dashboards", "OAuth proxy", "user:full scope")
    }

    Rel(jobs, vaultC, "Init / unseal / policies")
    Rel(esoC, vaultC, "k8s auth")
    Rel(esoS, vaultC, "k8s auth (cross-cluster)")
    Rel(kcC, vaultC, "OIDC client secrets via ES")
    Rel(dash, kcS, "OAuth login")
    Rel(vaultC, kcC, "Human OIDC login to Vault UI")
```

---

## Placement (verified lab)

| Component | Namespace | Mode |
|-----------|-----------|------|
| Vault central | `central-vault` | HA Raft, 3 replicas |
| Vault services | `services-vault` | HA Raft, 3 replicas |
| ESO | both clusters | Operator + controller |
| Keycloak central | `central-rhbk` | 2 instances, Crunchy PG |
| Keycloak services | `services-rhbk` | 2 instances, Crunchy PG |

---

## Auth model

| Who | How |
|-----|-----|
| ESO / workloads | Vault Kubernetes auth (`kubernetes-central`, `kubernetes-services`) |
| Humans | Keycloak OIDC → Vault / OpenShift / dashboards |
| Automation Jobs | Short-lived SA + ExternalSecrets; never commit tokens |

Group `sovereign-admin` maps to elevated OpenShift and Vault policies.

---

## Rules

1. Never create plain `Secret` resources in Helm for credentials — use ExternalSecret with `creationPolicy: Owner`.
2. PushSecret moves cluster-generated secrets into Vault when needed.
3. Dashboard OAuth client secrets: Vault paths `dashboard-oauth`, `tenancy-dashboard-oauth`.
4. Lab TLS often uses edge termination / disabled in-pod TLS — track in [../../technical/deviations.md](../../technical/deviations.md).

---

## Related

- [../../technical/09-vault.md](../../technical/09-vault.md)
- [../../technical/09-vault.md](../../technical/09-vault.md)
- [../../technical/06-keycloak.md](../../technical/06-keycloak.md)
- Specs `022`, `023`
