# Engineering highlights

Moved verbatim from the repository README, where it sat between the documentation map and the
contributing notes. It describes the project's engineering history, not how to use the platform;
counts in it are as they were when it was written.

## Engineering Highlights (Resume Context)

> Use these bullets when describing involvement with this platform.

- **Designed and implemented a two-tier Kubernetes operator architecture** consolidating 13 legacy Ansible operators into primary (cluster-scoped) and namespace (per-tenant) tiers with event-driven reconciliation
- **Built an event-driven provisioning pipeline** using AMQ Streams Kafka, AAP EDA (33 rulebook activations), and domain-isolated Decision Environment container images
- **Delivered multi-cluster GitOps** with a single ArgoCD app-of-apps managing 26+ Helm charts across central (management) and services (workload) OpenShift clusters
- **Implemented a full-stack UI platform** — React/TypeScript dashboards, OpenShift dynamic console plugins, shared CRD type library, OAuth-proxied Kubernetes API access with RBAC-aware UI
- **Established security-first patterns** — Vault HA on both clusters, External Secrets Operator, zero secrets in Git, Keycloak OIDC, 14-role namespace RBAC model
- **Authored comprehensive platform documentation** — C4 architecture model, ADRs, 32 feature specs, hardening checklists, agent-executable test specifications
- **Enabled sovereign/disconnected deployment** — private OCI registry, in-cluster Gitea SCM, air-gap checklist, no runtime public internet dependency
- **Automated hybrid cloud provisioning** — OpenShift via RHACM/Hive, AWS and OpenStack environments, VMware-to-CloudOSO VM migration via MTV
