# Hardening Check: 006 Platform EDA Rebuild

**Feature**: Platform EDA Rebuild — Event-Driven Automation  
**Original date**: 2026-06-09  
**Retested**: 2026-07-15  

> **Retest note**: Event Forwarder is **retired** (`eventForwarder.enabled: false`). Rows 1–12 below are historical for the chart still in-tree. Live path = operator → Kafka (`amq_publish.yml`) → EDA `ansible.eda.kafka` activations. See [operators-events.md](operators-events.md) and [aap-split.md](aap-split.md).

**Scope (live)**: AMQ Streams, Decision Environments, EDA controller on central, dashboard refresh

---

## CIS/NIST Validation Matrix (includes historical forwarder rows)

| # | Component | Check | Status | Notes |
|---|-----------|-------|--------|-------|
| 1 | Event Forwarder | Image provenance (Red Hat UBI base) | ✅ | `registry.access.redhat.com/ubi9/python-311` in Containerfile |
| 2 | Event Forwarder | Non-root container | ✅ | `USER 1001` in Containerfile |
| 3 | Event Forwarder | Pod securityContext enforced | ✅ | Chart sets `runAsNonRoot`, `allowPrivilegeEscalation: false`, `seccompProfile: RuntimeDefault`, `capabilities.drop: ALL` |
| 4 | Event Forwarder | NetworkPolicy egress restriction | ⚠️ | No NetworkPolicy yet; forwarder needs egress only to central Event Stream URL and kube-apiserver — add chart template |
| 5 | Event Forwarder | RBAC least privilege (watch only) | ✅ | ClusterRole: `events.k8s.io/events` get/list/watch + `namespaces` get/list |
| 6 | Event Forwarder | No wildcard verbs or resources | ✅ | Explicit resource list; no `*` verbs |
| 7 | Event Forwarder | Event Stream token via ExternalSecret | ✅ | Vault path `central/event-forwarder`; `creationPolicy: Owner` |
| 8 | Event Forwarder | No secrets in Git | ✅ | Token delivered at runtime from Vault via ESO |
| 9 | Event Forwarder | Dedup prevents replay storms | ✅ | LRU cache (10,000 entries) keyed by Event UID |
| 10 | Event Forwarder | Retry with bounded backoff | ✅ | 3 retries, 1/2/4s backoff; 30s HTTP timeout |
| 11 | Event Forwarder | Filtered watch scope | ✅ | Namespace patterns `entity-*`, `sovereign-cloud-plugins`; `*-operator` controller; `*Requested` reason |
| 12 | Event Forwarder | Deployed via GitOps only | ✅ | ArgoCD Application `event-forwarder` in `sovereign-cloud-plugins` |
| 13 | Decision Environments | Red Hat base image | ✅ | `ansible-automation-platform-25/de-minimal-rhel9:latest` |
| 14 | Decision Environments | Minimal collection set | ✅ | Entity DE: `kubernetes.core`, `ansible.controller` only |
| 15 | Decision Environments | No embedded secrets in image | ✅ | Credentials read at runtime from K8s Secrets / Vault |
| 16 | Decision Environments | `no_log` on credential tasks | ✅ | Token extraction from `argocd-cluster-services` uses `no_log: true` |
| 17 | Decision Environments | Private OCI registry | ✅ | DE images in private Quay repos (`de-<operator>`) |
| 18 | Decision Environments | Per-operator image isolation | ✅ | Separate DE per operator; no shared bloated image |
| 19 | Cross-Cluster Status Writes | ServiceAccount scope limited | ✅ | EDA uses ArgoCD-registered `argocd-cluster-services` bearer token |
| 20 | Cross-Cluster Status Writes | Write scope to status subresources | ✅ | `kubernetes.core.k8s` patches CR status on services cluster only |
| 21 | Cross-Cluster Status Writes | Token not stored in Git | ✅ | Token read from `openshift-gitops` Secret at runtime |
| 22 | Cross-Cluster Status Writes | `no_log` on token extraction | ✅ | All bearer-token `set_fact` tasks use `no_log: true` |
| 23 | Cross-Cluster Status Writes | Token rotation path documented | ⚠️ | Token rotates when ArgoCD cluster secret is refreshed; document rotation runbook |
| 24 | Cross-Cluster Status Writes | Idempotent status updates | ✅ | EDA sets `observedGeneration` to stop operator re-emit |
| 25 | EDA Controller | Admin credentials via ExternalSecret | ✅ | `eda-admin-credentials` from Vault; `no_log` in eda-config role |
| 26 | EDA Controller | Event Stream credential isolation | ✅ | Dedicated `sovereign-operator-events-credential` (Token type) |
| 27 | EDA Controller | Activation isolation per operator | ✅ | Each activation bound to operator-specific DE (`de-<operator>`) |
| 28 | EDA Controller | Rulebook project from Git (not inline) | ✅ | `sovereign-eda-rulebooks` project references Gitea repo |
| 29 | EDA Controller | Authentication on Event Stream ingress | ✅ | Bearer token required on POST; token from Vault |
| 30 | EDA Controller | `no_log` on all credential registration tasks | ✅ | eda-config role marks credential/stream/activation tasks `no_log: true` |
| 31 | EDA Controller | Restart policy on failure | ✅ | `restart_policy: on-failure` on rulebook activations |
| 32 | Dashboard Refresh | Rate limiting on API endpoints | ✅ | `apiLimiter` 120/min; `mutationLimiter` 30/min in tenancy dashboard |
| 33 | Dashboard Refresh | X-Forwarded-Access-Token only | ✅ | k8s-proxy rejects calls without user OAuth token |
| 34 | Dashboard Refresh | No ServiceAccount token for mutations | ✅ | Refresh PATCH uses logged-in user's token, not pod SA |
| 35 | Dashboard Refresh | Annotation cleared after reconcile | ✅ | Operator nulls `reconcileNow` after handling |
| 36 | Dashboard Refresh | Helmet security headers | ✅ | CSP, HSTS, referrer-policy configured |
| 37 | Operators (refactored) | Shrunk RBAC (no external API verbs) | ✅ | Entity operator dropped Keycloak/Vault/namespace-create verbs |
| 38 | Operators (refactored) | Event emission only | ✅ | Create/delete/reconcile events; no provisioning in operator |
| 39 | Operators (refactored) | `watchAnnotationsChanges: true` | ✅ | Required for forced reconcile via dashboard |
| 40 | Global Tests | End-to-end EDA validation | ✅ | `global_tests/playbooks/validate-eda.yml` + `check_eda_events` role |

---

## NIST Control Mapping (Summary)

| NIST Family | Control Area | How Addressed |
|-------------|--------------|---------------|
| AC | Access Control | Least-privilege RBAC on forwarder; user-token-only dashboard mutations |
| AU | Audit | K8s Events retained; forwarder logs event UID + reason (no token leakage) |
| CM | Configuration Management | All components deployed via ArgoCD GitOps |
| IA | Identification & Authentication | Event Stream bearer token; EDA admin token from Vault |
| SC | System & Communications Protection | TLS to Event Stream; NetworkPolicy gap tracked (#4) |
| SI | System Integrity | Image provenance via Red Hat base images; private registry |

---

## Deviations from Best Practice

| Deviation | Why | Where | Remediation / Production Target |
|-----------|-----|-------|----------------------------------|
| Custom event forwarder (non-Red Hat product) | RHACM does not forward `events.k8s.io/v1`; no supported central→services K8s watch | `eda/event-forwarder/` | Evaluate Red Hat supported alternatives when available; maintain forwarder with security review cadence |
| Cross-cluster status writes | EDA on central must update CR status on services | `eda/*/roles/*_provision/`, `*_teardown/` | Acceptable deviation; token scoped via ArgoCD cluster registration |
| No NetworkPolicy on forwarder | Initial chart focused on functional delivery | `bootstrap/helm/charts/event-forwarder/` | Add egress-only NetworkPolicy restricting to Event Stream host + kube-apiserver |
| `validate_certs: false` on cross-cluster API | ArgoCD cluster secret may not include CA bundle | EDA provision/teardown roles | Enable cert validation when CA is stored alongside bearer token |

---

## Related Documentation

- [006 EDA Architecture](../docs/technical/006-eda-architecture.md)
- [006 EDA Overview](../docs/concepts/006-eda-overview.md)
- [006 EDA Developer Guide](../docs/tutorial/006-eda-developer-guide.md)
- Event contract: event-contract.md (not published in this repository)
- Status handshake: operator-eda-handshake.md (not published in this repository)
