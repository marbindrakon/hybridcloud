# Security Interaction Diagrams

**Date:** 2026-06-16  
**Purpose:** Visual reference for security boundaries — secrets flow, cross-cluster auth, EDA events, and OIDC chain.

Each diagram is intentionally small (≤15 nodes). No styling/colors applied per documentation standards.

---

## 1. Secrets Flow — Vault → ESO → Kubernetes Secret → Pod

```mermaid
flowchart LR
    subgraph VaultCentral["vault-central"]
        KV["KV v2 engine\ncentral/"]
        K8sAuth["Kubernetes auth mount"]
    end

    subgraph CentralCluster["Central cluster"]
        CSS["ClusterSecretStore\nvault-backend"]
        ESOc["ESO controller"]
    end

    subgraph ServicesCluster["Services cluster"]
        CSSs["ClusterSecretStore\nvault-backend"]
        ESOs["ESO controller"]
        ES["ExternalSecret\nresource"]
        K8sSec["Kubernetes Secret"]
        Pod["Workload Pod"]
    end

    KV --> K8sAuth
    K8sAuth --> CSS
    K8sAuth --> CSSs
    CSS --> ESOc
    CSSs --> ESOs
    ESOs --> ES
    ES --> K8sSec
    K8sSec --> Pod
```

**Notes:**

- vault-central is the single source of truth for platform credentials.
- ESO authenticates via Kubernetes auth — not the Vault root token.
- PushSecret (not shown) flows operator-generated secrets back to Vault KV.

---

## 2. Cross-Cluster Auth — Central ArgoCD SA → Services API Bearer Token

```mermaid
flowchart TB
    subgraph Central["Central cluster"]
        ArgoCD["ArgoCD application\ncontroller"]
        ClusterSecret["Secret\nargocd-cluster-services"]
        EDA["AAP EDA\nDecision Environment"]
    end

    subgraph Services["Services cluster"]
        APIServer["Kubernetes API server"]
        ArgocdMgr["SA argocd-manager\nkube-system"]
        CRs["hybridsovereign CRs\n+ namespace RBAC"]
    end

    ArgoCD -->|"cluster registration"| ClusterSecret
    ArgoCD -->|"remote sync"| APIServer
    ArgocdMgr -->|"cluster-admin token"| APIServer
    ClusterSecret -->|"bearerToken\n(read at runtime)"| EDA
    EDA -->|"status patch\nnamespace RBAC"| CRs
    CRs --> APIServer
```

**Security boundary:** The ArgoCD cluster secret token currently grants cluster-admin on the services cluster. EDA decision environments read this token to write CR status and provision namespace Roles.

---

## 3. EDA Event Security Boundaries

```mermaid
flowchart LR
    subgraph Services["Services cluster"]
        Operator["Platform operator\nemits K8s Event"]
        Forwarder["event-forwarder\nwatch-only RBAC"]
    end

    subgraph Central["Central cluster"]
        EventStream["AAP Event Stream\nbearer auth"]
        EDA["EDA controller"]
        EE["Decision Environment\nansible-runner"]
        GitRepo["Gitea rulebooks\n(no secrets)"]
    end

    Operator -->|"events.k8s.io/v1\n*Requested"| Forwarder
    Forwarder -->|"HTTPS POST\nVault-sourced token"| EventStream
    EventStream --> EDA
    EDA --> EE
    GitRepo --> EE
    EE -->|"cross-cluster API\nargocd-cluster-services token"| Operator
```

**Boundaries:**

- Forwarder: read-only ClusterRole (events + namespaces).
- Event Stream: bearer token from Vault via ExternalSecret.
- EE: credentials at runtime only; `no_log` on token extraction tasks.

---

## 4. OIDC Chain — Keycloak → OpenShift OAuth → Dashboard oauth-proxy → User

```mermaid
flowchart TB
    User["Browser user"]
    KC["Keycloak\nrhbk-services\nrealm sovereign-tenants"]
    OCPOAuth["OpenShift OAuth\nserver"]
    Route["OpenShift Route\nreencrypt TLS"]
    Proxy["ose-oauth-proxy\nsidecar"]
    Dashboard["Dashboard app\nNode.js"]
    K8sAPI["Services API server"]

    User -->|"1. Login"| KC
    KC -->|"2. OIDC token"| OCPOAuth
    User -->|"3. HTTPS"| Route
    Route --> Proxy
    Proxy -->|"4. Validates OAuth token"| OCPOAuth
    Proxy -->|"5. X-Forwarded-Access-Token"| Dashboard
    Dashboard -->|"6. User bearer token\n(not SA token)"| K8sAPI
```

**Notes:**

- OAuth client secrets stored in Vault (`central/dashboard-oauth`, `central/tenancy-dashboard-oauth`).
- Cookie flags: httponly, secure, samesite enforced on dashboard routes.
- Dashboard SA has impersonation rights but mutations use the forwarded user token.

---

## Related Documentation

- [09-vault.md](./09-vault.md) — full path inventory and sync waves
- [06-keycloak.md](./06-keycloak.md) — realm and client configuration
- [006-eda-architecture.md](./006-eda-architecture.md) — event-driven automation topology
- [security-state.md](../../hardeningcheck/security-state.md) — current findings
