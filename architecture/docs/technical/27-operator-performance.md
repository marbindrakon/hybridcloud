# Operator Performance — Concurrent Reconciles

## Configuration

All operators built by this repository are configured for **10 concurrent reconciles** (`maxConcurrentReconciles: 10` in `operator/watches.yaml`).

## Operator Table

| Operator | Kind(s) | maxConcurrentReconciles | Memory Limit | Notes |
|----------|---------|------------------------|--------------|-------|
| `Entity` | Entity | 10 | 1Gi | Provisions tenant namespaces |
| `Team` | Team | 10 | 1Gi | |
| `Assignment` | Assignment | 10 | 1Gi | |
| `Projects` | Project | 10 | 1Gi | |
| `PlatformOpenshift` | PlatformOpenshift | 10 | 1Gi | |
| `plugin_rbac` | RbacConfig, Rbac | 10 | 1Gi | 2 CRD kinds |
| `plugin_aap` | AAPConfig, AAPOrg | 10 | 1Gi | 2 CRD kinds |
| `plugin_quay` | QuayConfig, QuayOrg | 10 | 1Gi | 2 CRD kinds |
| `plugin_sdx` (retired) | Iaac | 1 | 256Mi | Go controller; replaced by `iaac-git-sync` |
| `plugin_vault` | Vault, VaultKV | 10 | **2Gi** | Stateful: deploys Vault instances |
| `CloudOSO` | CloudOSO | 10 | **2Gi** | Stateful: interacts with OpenStack |

## Memory Guidance

Operators that create external infrastructure (Vault instances, OpenStack resources) have elevated memory limits:
- **plugin_vault**, **CloudOSO**: 2Gi limit / 512Mi request — handles 10 Vault instance reconcile goroutines
- All others: 1Gi limit / 256Mi request

## Tuning Background

- `maxConcurrentReconciles` determines how many CRs can be reconciled in parallel.
- With 10 concurrent reconciles and typical reconcile durations of 5–30s, a single operator pod handles 20–120 CR reconciles per minute.
- `reconcilePeriod` controls re-queue frequency on no-change events. Ranges from 5m (plugins, SDX) to 30m (Entity).
- Memory: each concurrent reconcile goroutine peaks at ~50–100MB for operators that spawn subprocesses (Ansible Runner containers). 2Gi headroom for stateful operators prevents OOMKill under load.

## Image Tags (Phase 2)

After bumping `maxConcurrentReconciles`, all operator images must be rebuilt with `make all` from each operator directory:

```bash
# Example for Entity operator
cd Entity && make all
# Repeat for: Team, Assignment, Projects, PlatformOpenshift,
#             plugin_rbac, plugin_aap, plugin_quay, plugin_sdx,
#             plugin_vault, CloudOSO
```

The new image tags are reflected in `bootstrap/helm/central/values.yaml`.
