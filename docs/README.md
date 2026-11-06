# Documentation

## Tenants

| Page | What it covers |
|---|---|
| [Getting started](tenant/getting-started.md) | Sign in, find your tenant, a network, a VM environment, the placement, the site console, DNS, adding a colleague, Retry now, deleting. |
| [Glossary](tenant/glossary.md) | The words, what each is in the API and in other clouds; which project is which; what to create for what. |
| [Access and delegation](tenant/access-and-delegation.md) | Access groups, tenant roles, access on one object, the access fields of team setups and app projects. |
| [Status reasons](tenant/status-reasons.md) | States, the reasons and who acts on them, Retry now, deletion order and protection. |
| [Hybrid networks tutorial](../architecture/docs/tutorial/09-hybrid-networks.md) | Networks across sites, placements on OpenShift Virtualization, OpenStack, AWS and Google Cloud, internet egress, [IPv6 for a hybrid network](../architecture/docs/tutorial/09-hybrid-networks.md#ipv6-for-a-hybrid-network) (the three choices, internet access per family, IPv6 subnets, IPv6-only guests). |
| [Samples](../samples/README.md) | One example per kind of object. |

`oc explain <kind>` (for example `oc explain networkplacement.spec`) describes every field.

## Platform team

| Page | What it covers |
|---|---|
| [Onboard a tenant](platform/onboard-a-tenant.md) | Sites and allowed sites, the tenant and its profile, access groups, the role table, the access fields, membership, sign-in checks, offboarding. |
| [IPv6 realization](platform/ipv6-realization.md) | Platform IPv6 pools, block sizes, tenant allowance and hold-down, exclusive ranges, per-site switches and sources, site capabilities, AWS and Google Cloud placements, translation, IPv6 egress, the dual-stack core, reverse DNS, the AWS and Google Cloud edges. |
| [Border gateway nodes](platform/border-gateway-nodes.md) | What the fabric does on a border gateway node on either core transport: VRFs kept from the node's own addresses, what the host must not have, the TCP MSS clamps, a network leaving a node, the orphan check, teardown. |
| [SRv6 core transport](platform/srv6-core-transport.md) | The fabric's core transport switch, the SID block and locators, what a border gateway node needs, the guard and the routing rule, readiness, status fields, changing transport. |
| [Convert an OpenShift Virtualization site to dual-stack](platform/metal-dual-stack-conversion.md) | The day-2 conversion of a bare-metal OpenShift cluster: pre-checks, the node step, the cluster step, the roll, post steps, verification, rollback by rebuild, known product issues. |
| [Bootstrap](../bootstrap/README.md) | Building the charts and seeding Argo CD on a new installation. |
| [Disconnected deploy](disconnected-deploy.md) | What a disconnected installation needs. |
| [Lab configuration policy](lab-config.md) | Where installation-specific values live (never in Git). |
| [Architecture index](../architecture/docs/architecture.md) | C4 model, technical references, decisions, retired components. |

## Contributors

| Page | What it covers |
|---|---|
| [Content rules](content/style.md) | Voice, terms, CRD description conventions, placeholders. |
| [Terms](content/terms.yaml) | Display terms per kind, banned words, retired components (read by `make check-content`). |
| [Feature specs](../specs/README.md) | The original specifications (historical; the CRDs are authoritative). |
