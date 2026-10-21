# Hybrid Sovereign Cloud — Architecture

C4-model documentation for the `hybridcloud/` monorepo. Start here, then drill down by level.

## Documentation map (C4-first)

| Level | Document | Audience |
|-------|----------|----------|
| **L1 Context** | [docs/c4/context.md](docs/c4/context.md) | Everyone — actors, externals, two-cluster topology |
| **L2 Containers** | [docs/c4/containers.md](docs/c4/containers.md) | Architects / operators — deployable units per cluster |
| **L3 Components** | [docs/c4/components/](docs/c4/components/) | Engineers — operator, events, Git export, UI, secrets |
| **L4 Code** | [docs/c4/code/](docs/c4/code/) | Implementers — entity lifecycle and key flows |
| **Index** | [docs/architecture.md](docs/architecture.md) | Full catalog of concepts, technical, tutorials, ADRs |
| **Hardening** | [hardeningcheck/](hardeningcheck/) | Security posture retested against live lab |
| **Specs** | [../specs/README.md](../specs/README.md) | Feature specs `001`–`034` with relevance status |
| **Mocks** | [mocks/](mocks/) | UI design artifacts (not runtime architecture) |
| **Archive** | [docs/archive/](docs/archive/) · [hardeningcheck/archive/](hardeningcheck/archive/) | Superseded duplicates and one-shot checklists |

## Platform summary (current)

Two OpenShift clusters, one GitOps control plane:

| Plane | Cluster (lab) | Role |
|-------|---------------|------|
| Management | Central (`api.central…`) | ArgoCD app-of-apps, RHACM, Vault HA, Gitea, Keycloak, AMQ Streams, AAP Controller **+ EDA** |
| Workload | Services (`api.services…`) | Primary + namespace operators, dashboards, console plugins, AAP Controller, Git export (`iaac-git-sync`) |

**Non-negotiables:** no secrets in Git · never delete `sovereign-*` namespaces · post-bootstrap changes only via ArgoCD.

## Versions

Image and chart versions are pinned in [`bootstrap/helm/central/values.yaml`](../bootstrap/helm/central/values.yaml); this page does not repeat them.

Using the platform rather than building it? See the [documentation index](../docs/README.md).

## How to keep this accurate

1. Topology or placement change in `bootstrap/` → update L1/L2 C4 docs the same PR.
2. Operator / EDA / UI behavior change → update the matching L3 component + hardening check.
3. End of a work cycle → retest [hardeningcheck/security-state.md](hardeningcheck/security-state.md).
4. Do not resurrect archived docs; link from archive READMEs if historical context is needed.
