# Samples

One small example per kind of object, named `example-*`, for the tenant `example-corp` (tenant
namespace `entity-example-corp`). Read them to see the shape of an object, copy one, change the
names and apply that one file into your own tenant namespace:

```bash
oc apply -n entity-<tenant> -f my-network.yaml
```

There is no "apply everything" here on purpose: every sample creates real infrastructure or real
access when applied, and several depend on objects that must exist first (sites, the network,
the environment).

The words used below are explained in [Getting started](../docs/tenant/getting-started.md).

## Tenant objects

Apply these into your tenant namespace, in this order when you build from scratch. Your access
groups need the matching role (see [Onboard a tenant](../docs/platform/onboard-a-tenant.md)).

| Sample | Kind | What it shows |
|---|---|---|
| [rbac/example-admins.yaml](rbac/example-admins.yaml) and the other three in `rbac/` | Rbac | An access group. The platform team adds people to it. |
| [hybridnetwork/example-net.yaml](hybridnetwork/example-net.yaml) | HybridNetwork | A network with one IPv4 address space. |
| [hybridnetwork/example-net-own-ipv6.yaml](hybridnetwork/example-net-own-ipv6.yaml) | HybridNetwork | A dual-stack network with the tenant's own IPv6 prefix (carried between sites, no IPv6 internet). |
| [hybridnetwork/example-net-platform-ipv6.yaml](hybridnetwork/example-net-platform-ipv6.yaml) | HybridNetwork | A dual-stack network whose IPv6 block the platform assigns, with internet access. |
| [cloudvirt/example-vms.yaml](cloudvirt/example-vms.yaml) | CloudVirt | A VM environment on the site `site-vm`, its namespace bound to the network. |
| [cloudoso/example-osp.yaml](cloudoso/example-osp.yaml) | CloudOSO | An OpenStack environment on the site `site-osp`. |
| [cloudaws/example-aws.yaml](cloudaws/example-aws.yaml) | CloudAWS | An AWS environment (billable). |
| [cloudgcp/example-gcp.yaml](cloudgcp/example-gcp.yaml) | CloudGCP | A Google Cloud environment (billable). |
| [networkplacement/example-net-vms.yaml](networkplacement/example-net-vms.yaml) | NetworkPlacement | The network attached to the VM environment, with a /24. |
| [attachmentcircuit/example-edge.yaml](attachmentcircuit/example-edge.yaml) | AttachmentCircuit | An edge connection to the tenant's own router. |
| [platformopenshift/example-cluster.yaml](platformopenshift/example-cluster.yaml) | PlatformOpenshift | An OpenShift cluster in the OpenStack environment. |
| [team/example-team.yaml](team/example-team.yaml) | Team | A team setup, with access groups that may manage it (access fields: available with this release). |
| [project/example-app.yaml](project/example-app.yaml) | Project | An app project, with its access fields (available with this release). |
| [assignment/example-team-on-cluster.yaml](assignment/example-team-on-cluster.yaml) | Assignment | The team setup and app project on the cluster, with access. |
| [vault/example-vault.yaml](vault/example-vault.yaml) | Vault | A Vault of the tenant's own. |
| [vaultkv/example-kv.yaml](vaultkv/example-kv.yaml) | VaultKV | A KV secrets engine in that Vault. |
| [aaporg/example-automation.yaml](aaporg/example-automation.yaml) | AAPOrg | An AAP organization. |
| [quayorg/example-images.yaml](quayorg/example-images.yaml) | QuayOrg | A Quay organization. |
| [experimental/example-openstackmigration.yaml](experimental/example-openstackmigration.yaml) | OpenStackMigration | Experimental: a VM migration to OpenStack. |

Site names (`site-vm`, `site-osp`, `site-aws`, `site-gcp`) and the edge name are placeholders:
ask the platform team for the real ones. The samples do not name the platform's identity
provider, automation or registry connections: the platform uses its default connection for
each.

## Platform objects (reference only)

[platform/](platform/) shows the shape of the objects the platform team manages in Git: sites,
the fabric, site gateways, the provider edge, tenant DNS settings and the connections to the
identity provider, automation and registry. The tenant itself
([entity/example-corp.yaml](entity/example-corp.yaml)) is created by the platform team too. Do
not apply platform objects by hand; the platform's GitOps owns them.

[platform/ipv6-canary/](platform/ipv6-canary/README.md) is different: the translation canary, a
probe workload the platform team deploys on a tenant of its own and names in the realization.

## Checking the samples

Every sample passes a strict server-side dry run (nothing is created):

```bash
KUBECONFIG=<services kubeconfig> OC=oc SAMPLES_NS=entity-<an existing tenant> \
  python3 -I scripts/check-manifests.py samples
```

Some kinds have no sample on purpose: Persona (deprecated), TransportLink and Iaac (not
implemented).
