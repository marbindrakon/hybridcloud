# Component Interaction Map — Hybrid Sovereign Cloud

**Audience:** Technical leadership, architects, onboarding engineers  
**Last updated:** 2026-06-16

This document provides five focused diagrams showing how Sovereign Cloud components connect. Each diagram stays within 15 nodes and omits styling so it renders consistently in any viewer.

---

## Diagram 1: Cluster Topology

The platform runs on two OpenShift clusters managed by a single central ArgoCD instance. The central cluster holds management tooling (ArgoCD, RHACM, Vault, Gitea, EDA/AAP). The services cluster runs tenant-facing operators, dashboards, and Keycloak. Both clusters share storage operators (ODF, Quay, Crunchy Postgres). Provisioned tenant clusters (spokes) register with RHACM on central.

```mermaid
flowchart TD
    Git["Git + OCI Registry"]
    Central["Central Cluster"]
    ArgoCD["ArgoCD App-of-Apps"]
    RHACM["RHACM Hub"]
    Vault["Vault + Gitea"]
    EDA["EDA + AAP"]
    Services["Services Cluster"]
    Ops["Operators + Dashboards"]
    Spoke1["Spoke: ocp-ses10"]
    Spoke2["Spoke: ocp-ses4"]

    Git --> ArgoCD
    ArgoCD --> Central
    ArgoCD --> Services
    Central --> Vault
    Central --> EDA
    Central --> RHACM
    Services --> Ops
    EDA --> Ops
    RHACM --> Spoke1
    RHACM --> Spoke2
```

---

## Diagram 2: Operator Placement and CR Dependency Graph

All `hybridsovereign.redhat` operators deploy to the **services cluster**. Helper operators (`helper.hybridsovereign.redhat`) on central are being decommissioned as part of Spec 008 — their logic moves into EDA roles. CR dependencies flow downward: an Entity creates the namespace foundation; Team, Project, and PlatformOpenshift CRs populate it; Assignment links them and drives spoke provisioning.

```mermaid
flowchart TD
    Entity["Entity CR"]
    NS["entity-namespace"]
    Persona["Persona CR"]
    Team["Team CR"]
    Project["Project CR"]
    PO["PlatformOpenshift CR"]
    Assign["Assignment CR"]
    Cloud["CloudOSO / CloudAWS CR"]

    Entity --> NS
    NS --> Persona
    NS --> Team
    NS --> Project
    NS --> PO
    NS --> Cloud
    Cloud --> PO
    Team --> Assign
    Project --> Assign
    PO --> Assign
```

**Placement summary**

| Location | Components |
|----------|------------|
| Services — `sovereign-cloud` | Primary operator, dashboards, console plugins |
| Services — `entity-<tenant>` | Namespace operator (tenant kinds: Team, Assignment, Project, PlatformOpenshift, environments, networks, ...) |
| Services — `sovereign-cloud-plugins` | `iaac-git-sync` (Git export); the `*Config` connection CRs |
| Central — `sovereign-cloud-jobs` | Ansible Jobs, EDA config, AAP controller config |
| Central — `aap` | AAP Controller, EDA controller |

---

## Diagram 3: Entity Lifecycle

Creating an Entity CR is the entry point for every tenant. The Entity operator emits an event; EDA/AAP creates the namespace, applies entity labels, sets up base RBAC scaffolding, and provisions ExternalSecrets for plugin credentials. Persona CRs (Spec 008) now handle per-role RoleBindings separately from Entity provisioning.

```mermaid
flowchart TD
    Admin["Admin creates Entity CR"]
    Op["Entity Operator"]
    Ev["EntityCreateRequested event"]
    EDA["EDA / AAP Job"]
    NS["entity-namespace created"]
    Labels["Entity labels applied"]
    RBAC["Base Roles created"]
    ES["ExternalSecrets synced"]
    Ready["Entity status: ready"]

    Admin --> Op
    Op --> Ev
    Ev --> EDA
    EDA --> NS
    NS --> Labels
    Labels --> RBAC
    RBAC --> ES
    ES --> Ready
```

**Key status fields:** `status.entity` (namespace name), `status.openshiftConsoleURL`, `status.ready`

---

## Diagram 4: Tenant Data Flow

An Assignment CR is the culmination of the tenant journey. The Assignment operator reads Team, Project, and PlatformOpenshift CRs in the same entity namespace, resolves RBAC group references, renders the `sovereign-assignment` Helm chart, and posts an ACM ConfigurationPolicy to the central hub. RHACM enforces the policy on the target spoke cluster, creating namespaces, RBAC, optional Argo CD, and service mesh resources.

```mermaid
flowchart TD
    Entity["Entity namespace"]
    Team["Team CR"]
    Project["Project CR"]
    PO["PlatformOpenshift CR"]
    Assign["Assignment CR"]
    Op["Assignment Operator"]
    Vault["Vault SA token"]
    ACM["ACM Policy on central"]
    Hive["Hive ClusterDeployment"]
    Spoke["Spoke cluster resources"]

    Entity --> Team
    Entity --> Project
    Entity --> PO
    Team --> Assign
    Project --> Assign
    PO --> Assign
    Assign --> Op
    Vault --> Op
    Op --> ACM
    PO --> Hive
    ACM --> Spoke
```

**Secret chain:** central ServiceAccount → PushSecret → Vault → ExternalSecret on services → Assignment operator reads token at reconcile time.

---

## Diagram 5: Plugin Operator Wiring

The plugin kinds (Rbac, Vault, AAPOrg, QuayOrg) are reconciled through the operators and EDA against the shared `*Config` CRs in `sovereign-cloud-plugins`. Each integrates with a platform service: Keycloak for access groups, Vault for secrets, AAP for automation orgs, Quay for registries. The Git export (`iaac-git-sync`) watches all tenancy CRs and syncs stripped YAML to Gitea on central; it does not use the EDA event path. (The Go Plugin SDX controller it replaced is retired.)

```mermaid
flowchart TD
    NS["Entity namespace CRs"]
    RBAC["Rbac (EDA rbac roles)"]
    VaultP["Vault / VaultKV (EDA vault roles)"]
    AAP["AAPOrg (EDA aaporg roles)"]
    Quay["QuayOrg (EDA quayorg roles)"]
    SDX["iaac-git-sync"]
    KC["Keycloak"]
    Vault["Vault"]
    AAPCtrl["AAP Controller"]
    QuaySvc["Quay Registry"]
    Gitea["Gitea tenancy_repo"]

    NS --> RBAC
    NS --> VaultP
    NS --> AAP
    NS --> Quay
    NS --> SDX
    RBAC --> KC
    VaultP --> Vault
    AAP --> AAPCtrl
    Quay --> QuaySvc
    SDX --> Gitea
```

---

## Related Documentation

| Topic | Document |
|-------|----------|
| Component catalogue | [04-components-overview.md](04-components-overview.md) |
| EDA architecture | [../technical/006-eda-architecture.md](../technical/006-eda-architecture.md) |
| Entity operator | [../technical/17-entity-operator.md](../archive/technical/17-entity-operator.md) |
| Assignment operator | [../technical/12-assignment-operator.md](../archive/technical/12-assignment-operator.md) |
| Persona operator | [../technical/45-persona-operator.md](../archive/technical/45-persona-operator.md) |
| Plugin SDX (retired) | [../archive/technical/25-plugin-iaac.md](../archive/technical/25-plugin-iaac.md) |
| Leadership deck | [09-leadership-demo-deck.md](../archive/concepts/09-leadership-demo-deck.md) |
