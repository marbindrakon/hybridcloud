# Hybrid Sovereign Cloud — Architecture Index

Checked by `make check-content` (words, links in scope) · **Source of truth for topology**: [c4/context.md](c4/context.md) → [c4/containers.md](c4/containers.md) → [c4/components/](c4/components/)

Use this page as a catalog. Prefer C4 for structure; use technical docs for component depth; use concepts for stakeholder narratives.

Start here instead if you use the platform: [Getting started](../../docs/tenant/getting-started.md) (tenants), [Onboard a tenant](../../docs/platform/onboard-a-tenant.md) (platform team), the [documentation index](../../docs/README.md). Some pages below describe earlier designs; the CRDs and `oc explain` are authoritative where they differ.

---

## C4 model

| Level | Document |
|-------|----------|
| L1 Context | [c4/context.md](c4/context.md) |
| L2 Containers | [c4/containers.md](c4/containers.md) |
| L3 Operator | [c4/components/operator.md](c4/components/operator.md) |
| L3 Event system | [c4/components/event-system.md](c4/components/event-system.md) |
| L3 Git export (iaac) | [c4/components/iaac.md](c4/components/iaac.md) |
| L3 UI | [c4/components/ui.md](c4/components/ui.md) |
| L3 Secrets & identity | [c4/components/vault-identity.md](c4/components/vault-identity.md) |
| L3 Platform services | [c4/components/platform-services.md](c4/components/platform-services.md) |
| L4 Entity lifecycle | [c4/code/entity-lifecycle.md](c4/code/entity-lifecycle.md) |

---

## Concepts (stakeholder)

| Document | Description |
|----------|-------------|
| [concepts/00-overview.md](concepts/00-overview.md) | Platform overview |
| [concepts/01-what-is-sovereign-cloud.md](concepts/01-what-is-sovereign-cloud.md) | Product framing |
| [concepts/02-how-it-works.md](concepts/02-how-it-works.md) | Simple flow |
| [concepts/03-security-model.md](concepts/03-security-model.md) | Security at a glance |
| [concepts/04-components-overview.md](concepts/04-components-overview.md) | Component map |
| [concepts/05-rbac-model.md](concepts/05-rbac-model.md) | Tenant roles narrative (earlier model; the role table is in [Onboard a tenant](../../docs/platform/onboard-a-tenant.md)) |
| [concepts/006-eda-overview.md](concepts/006-eda-overview.md) | EDA overview |
| [concepts/008-persona-overview.md](concepts/008-persona-overview.md) | Persona model |
| [concepts/10-component-interaction-map.md](concepts/10-component-interaction-map.md) | Interaction diagrams |
| [concepts/10-openshift-virtualization.md](concepts/10-openshift-virtualization.md) | CNV / MTV narrative |

---

## Technical (operators / implementers)

| Document | Description |
|----------|-------------|
| [technical/02-bootstrap-flow.md](technical/02-bootstrap-flow.md) | Bootstrap `make` sequence |
| [technical/03-central-cluster.md](technical/03-central-cluster.md) | Central cluster reference |
| [technical/04-services-cluster.md](technical/04-services-cluster.md) | Services cluster reference |
| [technical/05-oci-registry.md](technical/05-oci-registry.md) | Quay / OCI access |
| [technical/06-keycloak.md](technical/06-keycloak.md) | RHBK realms and clients |
| [technical/07-ansible-runner.md](technical/07-ansible-runner.md) | Ansible runner image |
| [technical/08-acs.md](technical/08-acs.md) | RHACS (central) |
| [technical/09-vault.md](technical/09-vault.md) | Vault HA Raft |
| [technical/10-aap.md](technical/10-aap.md) | AAP split (central EDA + services controller) |
| [technical/11-odf.md](technical/11-odf.md) | ODF / Noobaa |
| [technical/12-quay.md](technical/12-quay.md) | Quay registries |
| [technical/13-crunchy-postgres.md](technical/13-crunchy-postgres.md) | PGO |
| [technical/14-gitea.md](technical/14-gitea.md) | Gitea (central) |
| [technical/15-sovereign-dashboard.md](technical/15-sovereign-dashboard.md) | Admin dashboard |
| [technical/16-dashboard-api-reference.md](technical/16-dashboard-api-reference.md) | Dashboard API |
| [technical/19-plugin-rbac.md](technical/19-plugin-rbac.md) | RbacConfig / Rbac |
| [technical/20-tenancy-dashboard.md](technical/20-tenancy-dashboard.md) | Tenant dashboard |
| [technical/21-plugin-vault.md](technical/21-plugin-vault.md) | Vault / VaultKV CRs |
| [technical/22-plugin-aap.md](technical/22-plugin-aap.md) | AAPConfig / AAPOrg |
| [technical/23-plugin-quay.md](technical/23-plugin-quay.md) | QuayConfig / QuayOrg |
| [technical/26-observability.md](technical/26-observability.md) | Metrics / alerts |
| [technical/27-operator-performance.md](technical/27-operator-performance.md) | Reconcile tuning |
| [technical/28-cleanup-procedures.md](technical/28-cleanup-procedures.md) | Cleanup (never delete sovereign ns) |
| [technical/29-deployment-sequence.md](technical/29-deployment-sequence.md) | Phased ArgoCD rollout |
| [technical/30-app-of-apps-structure.md](technical/30-app-of-apps-structure.md) | App-of-apps layout |
| [technical/31-keycloak-vault-integration.md](technical/31-keycloak-vault-integration.md) | OIDC + k8s auth |
| [technical/33-cluster-builds-appset.md](technical/33-cluster-builds-appset.md) | Cluster builds |
| [technical/36-cluster-builds-oso.md](technical/36-cluster-builds-oso.md) | OpenStack cluster build |
| [technical/37-ocp-base-chart.md](technical/37-ocp-base-chart.md) | Spoke base chart |
| [technical/39-rbac-design.md](technical/39-rbac-design.md) | RBAC design |
| [technical/40-platformopenshift-oidc.md](technical/40-platformopenshift-oidc.md) | Spoke OIDC |
| [technical/41-two-layer-rbac.md](technical/41-two-layer-rbac.md) | Two-layer RBAC |
| [technical/42-per-cr-rbac-pattern.md](technical/42-per-cr-rbac-pattern.md) | Per-CR RBAC |
| [technical/48-aap-job-templates.md](technical/48-aap-job-templates.md) | AAP job templates |
| [technical/50-security-interaction-diagrams.md](technical/50-security-interaction-diagrams.md) | Security diagrams |
| [technical/51-qa-test-checklist.md](technical/51-qa-test-checklist.md) | QA checklist |
| [technical/56-openstack-migration.md](technical/56-openstack-migration.md) | OpenStack migration |
| [technical/57-hybridvpc-uihealth.md](technical/57-hybridvpc-uihealth.md) | Networks, sites and endpoint health checks in the UI |
| [technical/006-eda-architecture.md](technical/006-eda-architecture.md) | EDA architecture |
| [technical/007-services-aap-scope.md](technical/007-services-aap-scope.md) | Services AAP scope |
| [technical/deviations.md](technical/deviations.md) | Known deviations tracker |
| [technical/10-openshift-cnv.md](technical/10-openshift-cnv.md) | CNV |
| [technical/10-openshift-mtv.md](technical/10-openshift-mtv.md) | MTV |

CN V tutorials / ansible variants: [technical/10-openshift-cnv-tutorial.md](technical/10-openshift-cnv-tutorial.md), [technical/10-openshift-cnv-ansible.md](technical/10-openshift-cnv-ansible.md).

---

## Decisions (ADRs)

| ADR | Title |
|-----|-------|
| [ADR-001](decisions/ADR-001-monorepo.md) | Monorepo consolidation |
| [ADR-002](decisions/ADR-002-multi-tier-operator.md) | Multi-tier operator |
| [ADR-003](decisions/ADR-003-kafka-events.md) | Kafka event bus |
| [ADR-004](decisions/ADR-004-aap-config-as-code.md) | AAP config-as-code |

---

## Tutorials

| Document | Description |
|----------|-------------|
| [tutorial/01-day2-vault.md](tutorial/01-day2-vault.md) | Vault day-2 |
| [tutorial/02-day2-keycloak.md](tutorial/02-day2-keycloak.md) | Keycloak day-2 |
| [tutorial/03-day2-operators.md](tutorial/03-day2-operators.md) | Operator day-2 |
| [tutorial/04-day2-argocd.md](tutorial/04-day2-argocd.md) | ArgoCD day-2 |
| [tutorial/05-day2-plugins.md](tutorial/05-day2-plugins.md) | Plugin day-2 |
| [tutorial/06-user-dashboard.md](tutorial/06-user-dashboard.md) | Admin dashboard |
| [tutorial/07-tenancy-dashboard.md](tutorial/07-tenancy-dashboard.md) | Tenant dashboard |
| [tutorial/08-entity-rbac-tutorial.md](tutorial/08-entity-rbac-tutorial.md) | Entity RBAC |
| [tutorial/006-eda-developer-guide.md](tutorial/006-eda-developer-guide.md) | EDA developer guide |
| [tutorial/09-hybrid-networks.md](tutorial/09-hybrid-networks.md) | Hybrid networks across sites (current) |

---

## Hardening

| Document | Description |
|----------|-------------|
| [../hardeningcheck/README.md](../hardeningcheck/README.md) | Index + retest method |
| [../hardeningcheck/security-state.md](../hardeningcheck/security-state.md) | Current security state (retested) |

---

## Quick topology

| Cluster | Runs |
|---------|------|
| **Central** | ArgoCD, RHACM, Vault HA (`central-vault`), ESO, Gitea, RHBK (`central-rhbk`), AAP Controller + **EDA**, AMQ Streams, CNV/MTV, ACS Central, Sovereign Jobs |
| **Services** | RHBK (`services-rhbk`), Vault HA (`services-vault`), ESO, AAP Controller (no EDA), primary + namespace operators, Git export (`iaac-git-sync`), admin/tenant dashboards + console plugins |
| **Both** | ODF/Noobaa, Quay workload, Crunchy Postgres |

### Namespace rules

| Cluster | Namespaces |
|---------|------------|
| Central | `sovereign-cloud-jobs`, `sovereign-cloud-helpers`, plus component ns (`aap`, `gitea`, `amq-streams`, `central-vault`, …) |
| Services | `sovereign-cloud`, `sovereign-cloud-plugins`, `sovereign-cloud-jobs`, `sovereign-cloud-helpers`, `entity-*` |

**Never delete** any `sovereign-*` namespace.

### Plugin CR placement

| CR type | Namespace |
|---------|-----------|
| `RbacConfig`, `AAPConfig`, `QuayConfig` | `sovereign-cloud-plugins` |
| `Rbac`, `AAPOrg`, `QuayOrg`, `Vault`, `VaultKV`, Team/Project/… | `entity-<name>` |

Entity namespaces must carry label `hybridsovereign.redhat/entity`.

### Dashboard API rules

- User-facing K8s API calls use the logged-in OAuth access token (never the pod SA for user actions).
- OAuth proxy requests `user:full` scope.
- Secrets for OAuth clients come from Vault via ExternalSecret only.

---

## Retired components

Components of earlier releases that no longer run. Pages that still show them describe history.

<!-- retired:begin -->
| Retired | Replaced by |
|---|---|
| Event Forwarder (`eventForwarder.enabled: false`) | the operators publish their events to Kafka and the automation reads them there |
| Plugin SDX (Go plugin-iaac, `pluginIaac.enabled: false`) | the namespace operator and the automation; the Git export is `iaacGitSync` |
| OSOHelper and AWSHelper (and the helper-creator ServiceAccount) | the environment objects (CloudOSO, CloudAWS) do the work themselves |
| Hybrid VPC naming | networks (HybridNetwork) and network placements (NetworkPlacement) |
| BobsTeam role key | auditor |
<!-- retired:end -->

## Archive

Superseded duplicates live under [archive/](archive/). Do not update them for new work — update C4 or the matching technical doc instead.
