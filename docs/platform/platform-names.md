# Names on the platforms

How the platform names what it makes on the clouds, the sites and its own services, where it writes the full
names beside a short one, and how to get from a name or a number you see on a platform back to the tenant, the
environment and the object. Tenants read the same rules in their glossary ([Names](../tenant/glossary.md#names)).
Example names: tenant `example-corp`, environment `prod`, network placement `core-prod`, cluster `web`, access
group `devs`.

## The scheme

1. **A name is for people.** The platform never finds an object by its name. It finds it by an id it keeps in the
   object's status, or by a mark it sets (a tag, a label, an attribute, a marked description). An object of the
   wanted name that does not carry the platform's mark is **refused**, never taken over and never deleted: the
   object's status says `NameTakenOnSite` (or the area's own refusal, such as `NamespaceNotOwned`), and nothing is
   made until the platform team removes or renames the other object.
2. **The person's own names, in one order**: the tenant, then the environment or the network placement, then the
   object. They are joined by a character the names cannot contain: `_` where the platform allows an underscore
   (Keystone, Neutron, AWS tags, Quay, the automation platform, client ids of the sign-in directory), `--` where
   only lower-case letters, digits and hyphens are allowed (Kubernetes names, Google Cloud names, DNS labels).
   Access groups keep `<tenant>.<group>`: that one name is matched by the sign-in directory, the sites and the
   clusters at every sign-in.
3. **Nothing random.** A name that does not fit its platform is cut and ends in six characters derived from the
   exact names (`-` or `_` and six hexadecimal digits), so that two different sets of names never give one name.
   The full names are then in the object's description, labels or tags. An access group name is never cut: one
   that does not fit is refused.
4. **An object keeps the name it was given**, for its life; the name is recorded in its status. A change of the
   scheme names new objects only. Where the platform allows a rename in place and nothing else holds the old name
   (the network, router and subnets of a network placement on OpenStack), the object is renamed.

The code that makes every name is one function, `eda/common/files/shc_names.py`; nothing is joined by hand.

## The rule for names a person chooses

Every kind whose name a person chooses and that reaches a platform takes a name of **a letter first, then
lower-case letters, digits and single hyphens, a letter or a digit last, at most 30 characters**. A gateway's name
(CloudGateway) is at most **21**: it becomes a Google Cloud service account id (6 to 30 characters) after the
beginning `shc-edge-`. With 30, every pair the platform joins fits Keystone's 64 (`<tenant>_<environment>`,
`<tenant>.<group>`), and a cluster's name fits the installer's 54 and Google Cloud's letter-first rule.

The rule is checked three times:

- by the API server, on `metadata.name` of Entity, CloudOSO, CloudVirt, CloudAWS, CloudGCP, HybridNetwork,
  NetworkPlacement, PlatformOpenshift, Rbac, Project, Team, Assignment, Vault, VaultKV, QuayOrg, AAPOrg,
  AttachmentCircuit, CloudGateway, CloudInfrastructure, HybridFabric and ProviderEdge. An object made before the
  rule keeps working (status writes, changes, deletion): the API server checks an unchanged name only when it
  changes, and a name never changes;
- by the dashboard's create forms;
- by each job before it makes anything, for an object made before the rule: the job stops with the reason
  `NameNotAllowed` and the rule in the message.

A cluster on Google Cloud (`spec.type: gcp`) may also not start with `goog` or contain `google` or a word like it
(`g00gle`, `go0gle`): the OpenShift installer refuses such a name for Google Cloud.

Not under the rule: the platform's configuration kinds (RbacConfig, AAPConfig, QuayConfig, DNSConfig: connections
made once at installation; UIHealthChecker, Iaac, Persona; TransportLink, which the platform names; the
experimental OpenStackMigration) and TenantProfile (always `shc`). Their names take the API server's own rule.

Access group lists and references to the tenant's objects in a spec (for example `spec.toolRbac`, an
assignment's `spec.team`, a cluster's `spec.openstack.environment`) take a DNS label of at most 63 characters, so
that an object made before the rule can still be named there.

An environment's name is unique within its tenant across the four environment kinds (reason
`EnvironmentNameTaken`); two tenants may use the same name.

## What is named how

| Object | Name | Found again by | Full names written in |
|---|---|---|---|
| OpenStack project of an environment | `<tenant>_<environment>` (Keystone: 64) | CloudOSO `status.projectId`; a project tag of the platform | the project's description; CloudOSO `status.projectName` |
| Network, router of a placement on OpenStack | `<tenant>_<placement>`; subnets `…_v4`, `…_v6` | NetworkPlacement `status.siteNetwork.id`, `status.subnets[].id` | the description of the network, the router and each subnet; `status.siteNetwork.name` |
| A cluster's OpenStack user and application credential | `<tenant>_<environment>_<cluster>` | PlatformOpenshift `status.credential.userId`, `.id` | their descriptions |
| A cluster's floating IPs | (no name) | `status.floatingIPs.api.id`, `.ingress.id` | description `cluster <cluster>: API`, `cluster <cluster>: applications` |
| Namespace of a VM environment on an OpenShift Virtualization site | `<tenant>--<environment>--<namespace>` (63; cut with the ending) | its labels `hybridsovereign.redhat/entity`, `/cloudvirt`, `/cloudvirt-namespace` | the same labels |
| Access group on the services cluster, OpenStack and VM sites, tenant clusters | `<tenant>.<group>` (never cut) | the client role's attributes (`entity`, `owner`) | the client role's description |
| Access group in the sign-in directory | `/<tenant>/<group>` under `/<tenant>` | Rbac `status.groupId`; the group attribute `entity` | Rbac `status.group` |
| Quay organization, AAP organization | `<tenant>_<name>` | `status.orgName` (and the AAP organization's `status.orgId`) | the organization's description |
| Sign-in client of a Vault | `vault_<tenant>_<name>` | Vault `status.oidcClientId` | the client's name `Vault <tenant>/<name>` |
| Network placement on AWS (VPC, subnets, route tables) | `Name` tag `<tenant>_<placement>` | the tag `shc-placement=<namespace>/<placement>` | tags naming the tenant, the environment and the network |
| Network placement on Google Cloud | `shc-<tenant>--<placement>` (63, with room for the longest ending `-default6`; cut with the ending) | NetworkPlacement `status.realization.site.vpc` | the labels `shc-entity`, `shc-network` and the description |
| Google Cloud DNS zone of an environment (resource name) | `shc-<environment>--<tenant>` | CloudGCP `status.dnsZoneId` | the zone's description |
| Reverse DNS labels of gateways and placements | the names joined with `--` (63, cut with the ending) | — | — |
| A cluster's build on the hub (namespace, Helm release, cloud identities) | `status.buildName`, e.g. `shc-oso-<tenant>-<cluster>-<8 hex>`, kept for the cluster's life | `status.buildName` | the identities' descriptions and display names |
| DNS | `<environment>.<tenant>.<tenant DNS domain>`; a cluster's `api.<cluster>.<environment>.<tenant>.<tenant DNS domain>` | the zone id in status | — |
| The network object of a placement on an OpenShift site; the gateways' devices; the AWS hub's objects | a number: `hn<n>`, `t<n>`, `e<n>`, `shc-net-<n>` (next section) | labels and tags with the number | an annotation, FRR description lines, tags naming the tenant, the network and the placement |

What the OpenShift installer names inside a tenant's project or account (`<cluster>-<five characters>-master-0`)
is the installer's own form; the platform adds tags naming the tenant, the environment and the cluster where the
installer takes tags.

To see the name the function gives, run it with the kind and the names (the kinds are `KINDS` in the file):

```bash
echo '{"action": "name", "kind": "gcp-placement", "parts": ["example-corp", "core-prod"]}' \
  | python3 -I eda/common/files/shc_names.py
```

The answer says the name, whether it is exact or cut (`"exact": false`), and the full names as they are written
beside it (`"display": "example-corp / core-prod"`).

## From a number back to the tenant

Some names on sites and devices are numbers, because a device name on Linux has at most 15 characters and
OpenShift derives device names from the network object's name. The number is the placement's or the network's
number on the fabric.

| You see | The number is | Where |
|---|---|---|
| `hn<n>` (the network object on an OpenShift site and its route advertisement), `t<n>` (the placement's VRF on the gateways and the cloud edge routers), `br<n>`, `vx<n>`, `lo<n>`, `gre<n>` (cloud edge routers), `shc-net-<n>`, `shc-egress-<n>` (the AWS hub) | the placement's number: NetworkPlacement `status.l3Vni` | sites, gateways, cloud edge routers, the AWS hub |
| `e<n>`, `xe<n>`, `xo<n>` (a network's egress on the gateways) | the network's number: HybridNetwork `status.realization.vpnTarget` | gateways |

The placement with a given number, and its tenant, network and environment:

```bash
oc get networkplacements -A -o json | jq -r --argjson n 51003 \
  '.items[] | select(.status.l3Vni == $n) | [.metadata.namespace, .metadata.name, .spec.network, .spec.backend.kind, .spec.backend.name] | @tsv'
```

The network with a given number:

```bash
oc get hybridnetworks -A -o json | jq -r --argjson n 51003 \
  '.items[] | select(.status.realization.vpnTarget == $n) | [.metadata.namespace, .metadata.name] | @tsv'
```

On an OpenShift site, the objects of a placement also carry the labels `hybridsovereign.redhat/entity`,
`/networkplacement`, `/hybridnetwork` and `/l3vni`: `oc get clusteruserdefinednetworks -l
hybridsovereign.redhat/l3vni=51003 --show-labels`. A number is reused after its placement is deleted: read it
together with the object's creation time.

## From a cut name back to the tenant

A cut name ends in six hexadecimal digits. Three ways back, the first one first:

1. **The status that recorded it.** The job wrote the name it made into the object's status (the table above:
   `status.siteNetwork.name`, `status.realization.site.vpc`, `status.buildName`, `status.orgName`,
   `status.oidcClientId`). Search the kind for it:

   ```bash
   oc get networkplacements -A -o json | jq -r --arg v shc-example-corp--core-prod \
     '.items[] | select(.status.realization.site.vpc == $v) | [.metadata.namespace, .metadata.name] | @tsv'
   ```

2. **The full names beside it**: the description, the labels or the tags on the platform's object (the last column
   of the table).
3. **The ending itself.** It is the first six hexadecimal digits of the SHA-256 of the fixed beginning and the
   names, each separated by a NUL character. To check a guess, give the function the names and compare: the same
   names always give the same ending, and different names never meet in practice.

## Old objects and earlier label keys

An object made before the scheme keeps its name, recorded in its status, and is found by its id or its mark as
before. Label and annotation keys of the platform are under `hybridsovereign.redhat/`; objects made with the
earlier keys of the same word under another domain are still found by a teardown, which looks for both keys.
