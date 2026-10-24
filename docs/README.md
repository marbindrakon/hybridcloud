# Documentation

## Tenants

| Page | What it covers |
|---|---|
| [Getting started](tenant/getting-started.md) | Sign in, find your tenant, a network, a VM environment, the placement, the site console, DNS, adding a colleague, Retry now, deleting. |
| [Glossary](tenant/glossary.md) | The words, what each is in the API and in other clouds; which project is which; what to create for what. |
| [Access and delegation](tenant/access-and-delegation.md) | Access groups, tenant roles, access on one object, the access fields of team setups and app projects. |
| [Status reasons](tenant/status-reasons.md) | States, the reasons and who acts on them, Retry now, deletion order and protection. |
| [Hybrid networks tutorial](../architecture/docs/tutorial/09-hybrid-networks.md) | Networks across sites, placements on OpenStack and OpenShift Virtualization, internet egress, [IPv6 for a hybrid network](../architecture/docs/tutorial/09-hybrid-networks.md#ipv6-for-a-hybrid-network) (the three choices, internet access per family, IPv6 subnets, IPv6-only guests). |
| [Samples](../samples/README.md) | One example per kind of object. |

`oc explain <kind>` (for example `oc explain networkplacement.spec`) describes every field.

## Platform team

| Page | What it covers |
|---|---|
| [Onboard a tenant](platform/onboard-a-tenant.md) | Sites and allowed sites, the tenant and its profile, access groups, the role table, the access fields, membership, sign-in checks, offboarding. |
| [IPv6 realization](platform/ipv6-realization.md) | Platform IPv6 pools, block sizes, tenant allowance and hold-down, exclusive ranges, per-site switches and sources, site capabilities, translation, IPv6 egress. |
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
