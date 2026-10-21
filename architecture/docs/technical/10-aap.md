# Ansible Automation Platform (AAP)

## Overview

AAP provides automation capabilities including Controller (job execution),
EDA (event-driven automation), and Automation Hub (content management).

## Deployment

| Component | Cluster | Namespace | Notes |
|-----------|---------|-----------|-------|
| Operator | Central | `aap` | OLM Subscription, channel `stable-2.5` |
| Controller | Central | `aap` | Primary job execution for EDA provisioning |
| EAP EDA | Central | `aap` | Event Stream, rulebook activations, `run_job_template` |
| Hub | Central | `aap` | Content management (optional) |
| Tenant AAP instance | Services | `aap` | Separate `AnsibleAutomationPlatform` CR for tenant automation via `AAPOrg` plugin |

> **Placement clarification**: Platform EDA automation (operators → Kafka → EDA activations → AAP job templates) runs entirely on the **central cluster** in namespace `aap`. See [006 EDA Architecture](./006-eda-architecture.md). The services-cluster AAP instance is provisioned by the Plugin AAP operator for tenant-specific automation and is not part of the platform EDA pipeline.

## Chart: `bootstrap/helm/charts/aap`

- **Operator**: OLM Subscription, channel `stable-2.5`
- **Instance**: `AnsibleAutomationPlatform` CR with controller, EDA, and hub enabled

## ArgoCD Applications

| Application | Cluster | Purpose |
|-------------|---------|---------|
| `aap-central` | Central | Platform Controller + EDA + Hub |
| `aap-services` | Services | Tenant AAP instance (plugin-driven) |

Both are deployed via central ArgoCD using `destination.server` pointing to the target cluster API.

## Related docs

- [006 EDA Architecture](./006-eda-architecture.md) — EDA event flow and central AAP placement
- [48 AAP Job Templates](./48-aap-job-templates.md) — job template catalog
- [22 Plugin AAP](./22-plugin-aap.md) — tenant AAP org provisioning
