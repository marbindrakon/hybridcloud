# Cluster Builds — OpenStack Path

> **DEPRECATED:** The OpenStack cluster build path via helper operators has been removed. This document is retained as historical reference only.

## Overview (Historical)

The `cluster_builds` Git repository federated **dual provider trees** underneath `builds/`. Operators rendered **ACM/Hive-compatible** payloads for both hyperscaler AWS and Sovereign-hosted OpenStack. This document contrasts the **`builds/oso/*`** traversal with existing AWS sequencing.

Canonical ApplicationSet Helm template lives at `bootstrap/helm/central/templates/centralCluster/cluster-builds-applicationset.yaml` alongside `cluster-builds-appproject.yaml` and `argocd-cluster-builds-repo.yaml`.

## Repository layout (`cluster_builds`)

```
cluster_builds/
├── builds/
│   ├── aws/
│   │   └── <cluster-name>/
│   │       ├── build.yaml
│   │       └── helm_values/build-values.yaml
│   └── oso/
│       └── <cluster-name>/
│           ├── build.yaml
│           └── helm_values/build-values.yaml
└── README (optional housekeeping)
```

- **AWS helpers** synthesize manifests under **`builds/aws/<name>/`**.  
- **OSO helpers** synthesize manifests under **`builds/oso/<name>/`** with parallel naming for Application metadata.

## ApplicationSet dual-path behaviour

Multi-source **`cluster-builds-clusters`** `ApplicationSet` instances discover **either** subtree:

- **`builds/aws/*/build.yaml`**
- **`builds/oso/*/build.yaml`**

`bootstrap/helm/central/templates/centralCluster/cluster-builds-applicationset.yaml` configures one `git.files` stanza enumerating **both** patterns; Helm **`valueFiles`** interpolate `$values/{{ index .path.segments 0 }}/…/helm_values/build-values.yaml` so AWS and OSO trees reuse one template stanza while keeping provider-specific path prefixes (`builds/aws/...` vs `builds/oso/...`). OSO payloads must still set **`provider.type: OpenStack`** in `helm_values/build-values.yaml`.

## Helm: `charts/charts/mce-cluster-build`

- **AWS path** sets `provider.type: AWS`.  
- **OSO path** sets `provider.type: OpenStack` with:

  - `clouds.yaml` credentials resolved from **`cluster/oso/{build}/build/provider-creds`**: the restricted application credential of the cluster's own site user, made for this cluster alone and deleted with it ([Sign-in to an OpenStack site](../../../docs/platform/openstack-sign-in.md#cluster-users)); the chart's ExternalSecret copies it to the hub.  
  - **External/provider network** identifiers required by installer + Hive validations.  
  - **Floating IP** assignments for ingress / API semantics called out inside values (mirrors helper allocation actions).

Charts remain single chart surface; divergence is wholly values-driven (`values.yaml`, `templates/cluster-deployment.yaml` OpenStack branch).

## Hive `ClusterDeployment` notes (OpenStack)

- Uses installer-compatible **`clouds.yaml`** bundle + OpenStack Keystone auth instead of IAM minted credentials.  
- Requires reachable **provider external network**, router/LB scaffolding, security groups compliant with Sovereign Ansible automation.  
- Cluster DNS remains **delegated Sovereign apex** with **Route53 `A`** records targeting **floating IP** VIPs versus AWS-alias records.

## Operational cross-checklist

| Item | Verification |
| ---- | ------------- |
| Gitea commits | Presence of **`build.yaml`** + sibling Helm values directories |
| Argo rendered app | Matches `cluster-builds` Project + multi-source Helm chart linkage |
| Provider switch | Inspect `helm_values/build-values.yaml` `provider.type` |
| Networking | Floating IP inventory reconciles with Hive machine status conditions |
