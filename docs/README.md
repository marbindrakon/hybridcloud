# Documentation

## Tenants

| Page | What it covers |
|---|---|
| [Getting started](tenant/getting-started.md) | Sign in, find your tenant, a network, a VM environment, the placement, the site console, DNS, adding a colleague, Retry now, deleting. |
| [Hybrid networks tutorial](../architecture/docs/tutorial/09-hybrid-networks.md) | Networks across sites, placements on OpenStack and OpenShift Virtualization, internet egress. |
| [Samples](../samples/README.md) | One example per kind of object. |

`oc explain <kind>` (for example `oc explain networkplacement.spec`) describes every field.

## Platform team

| Page | What it covers |
|---|---|
| [Onboard a tenant](platform/onboard-a-tenant.md) | Sites, the tenant, access groups, the role table, membership, sign-in checks, offboarding. |
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
