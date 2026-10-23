# Observability — Prometheus Metrics & Kubernetes Events

## Overview

Operators in the Hybrid Sovereign Cloud platform expose Prometheus metrics via controller-runtime (port `8443`). Ansible-based operators use the Ansible Operator SDK; the Go-based SDX operator (`plugin-sdx`) uses controller-runtime directly. Operators emit structured Kubernetes Events for audit and troubleshooting.

## Deployed Operators and Monitoring Resources

### Tenancy Operators (sovereign-cloud namespace)

| Operator | ServiceMonitor | PrometheusRule |
|----------|---------------|----------------|
| Entity Operator | `entity-operator-metrics` | `entity-operator-rules` |
| Team Operator | `team-operator-metrics` | `team-operator-rules` |
| Assignment Operator | `assignment-operator-metrics` | `assignment-operator-rules` |
| Project Operator | `project-operator-metrics` | `project-operator-rules` |
| PlatformOpenshift Operator | `platformopenshift-operator-metrics` | `platformopenshift-operator-rules` |
| CloudOSO Operator | `cloudoso-operator-metrics` | `cloudoso-operator-rules` |

### Plugin Operators (sovereign-cloud-plugins namespace)

| Operator | ServiceMonitor | PrometheusRule |
|----------|---------------|----------------|
| Plugin RBAC | `plugin-rbac-metrics` | `plugin-rbac-rules` |
| Plugin AAP | `plugin-aap-metrics` | `plugin-aap-rules` |
| Plugin Quay | `plugin-quay-metrics` | `plugin-quay-rules` |
| Plugin Vault | `plugin-vault-metrics` | `plugin-vault-rules` |
| Plugin SDX (retired) | `plugin-sdx-metrics` | `plugin-sdx-rules` |

## Standard Prometheus Metrics (All Operators)

All operators built on the Ansible Operator SDK expose these controller-runtime metrics:

| Metric | Type | Description |
|--------|------|-------------|
| `controller_runtime_reconcile_total{controller,result}` | Counter | Total reconciliations by controller and result (success/error/requeue) |
| `controller_runtime_reconcile_errors_total{controller}` | Counter | Total reconciliation errors |
| `controller_runtime_reconcile_time_seconds{controller}` | Histogram | Reconciliation duration |
| `controller_runtime_max_concurrent_reconciles{controller}` | Gauge | Max concurrent reconciles |
| `workqueue_depth{name}` | Gauge | Current work queue depth |
| `workqueue_adds_total{name}` | Counter | Total items added |
| `workqueue_queue_duration_seconds{name}` | Histogram | Time items spend in queue |
| `workqueue_work_duration_seconds{name}` | Histogram | Processing time per item |
| `workqueue_retries_total{name}` | Counter | Total retries |
| `leader_election_master_status` | Gauge | Whether this instance is the leader |
| `rest_client_requests_total{code,host,method}` | Counter | K8s API requests |

## PrometheusRule Alerts

### Per-Operator Alerts (applied to all operators)

| Alert | Expression | Severity | Description |
|-------|-----------|----------|-------------|
| `<Operator>ReconcileErrors` | `increase(controller_runtime_reconcile_total{result="error"}[5m]) > 0` | warning | Reconciliation errors in last 5 minutes |
| `<Operator>OperatorDown` | `up{job="<operator>-metrics"} == 0` | critical | Operator metrics endpoint unreachable for 5 minutes |

## Kubernetes Events

All operators emit structured events via the `events.k8s.io/v1` API.

### Tenancy Operators

| Operator | Event Reason | Action | Description |
|----------|-------------|--------|-------------|
| Entity | Created/Updated | Reconcile | Entity namespace created/updated |
| Entity | Deleted | Delete | Entity namespace cleanup |
| Team | Created/Updated | Reconcile | Team CR synced |
| Team | Deleted | Delete | Team cleanup |
| Assignment | Reconciled | Reconcile | Assignment CR linked teams/projects/platforms |
| Assignment | Deleted | Delete | Assignment cleanup |
| Project | Created/Updated | Reconcile | Project CR reconciled |
| Project | Deleted | Delete | Project cleanup |
| PlatformOpenshift | Created/Updated | Reconcile | Platform registration updated |
| PlatformOpenshift | Deleted | Delete | Platform cleanup |
| CloudOSO | Created/Updated | Reconcile | CloudOSO configuration synced |
| CloudOSO | Deleted | Delete | CloudOSO cleanup |

### Plugin Operators

| Operator | Event Reason | Action | Description |
|----------|-------------|--------|-------------|
| Plugin RBAC (Rbac) | Created/Updated | Reconcile | Keycloak group created/updated |
| Plugin RBAC (Rbac) | Deleted | Delete | Keycloak group removed |
| Plugin RBAC (RbacConfig) | Created/Updated | Reconcile | Keycloak client and Secret synced |
| Plugin RBAC (RbacConfig) | Deleted | Delete | Keycloak client removed |
| Plugin AAP (AAPOrg) | Created/Updated | Reconcile | AAP organization synced |
| Plugin AAP (AAPConfig) | Created/Updated | Reconcile | AAP connection configured |
| Plugin Quay (QuayOrg) | Created/Updated | Reconcile | Quay organization synced |
| Plugin Quay (QuayConfig) | Created/Updated | Reconcile | Quay connection configured |
| Plugin Vault (VaultKV) | Created/Updated | Reconcile | Vault KV engine synced |
| Plugin Vault (VaultConfig) | Created/Updated | Reconcile | Vault connection configured |
| Plugin SDX (retired) | SyncComplete | Reconcile | Full CR sync to Gitea completed (`Iaac.status.conditions`) |

## Querying Metrics

### Example PromQL Queries

```promql
# Reconciliation rate across all operators
sum(rate(controller_runtime_reconcile_total[5m])) by (controller, result)

# Error rate for a specific operator
rate(controller_runtime_reconcile_total{controller="entity",result="error"}[5m])

# Average reconciliation duration
histogram_quantile(0.95, rate(controller_runtime_reconcile_time_seconds_bucket[5m]))

# Work queue depth across operators
workqueue_depth

# Leader election status
leader_election_master_status == 1
```

### Viewing Events

```bash
# All events from a specific operator
oc get events.events.k8s.io -n entity-acme-corp --field-selector reason=Reconciled

# All events for a specific CR
oc get events.events.k8s.io -A | grep "plugin-rbac"

# Recent operator events across all namespaces
oc get events.events.k8s.io -A --sort-by='.metadata.creationTimestamp' | tail -20
```
