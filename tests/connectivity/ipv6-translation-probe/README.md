# IPv6 translation probe

A test tool for the IPv6-only path of a site: one small probe VM per site, on an IPv6-only placement with
translation to IPv4, runs every minute the checks a tenant's IPv6-only guest depends on and serves the results. Use it
during acceptance, or after a change to a site's translation, its resolvers or the fabric's IPv6 egress, to see that
an IPv6-only guest still reaches the IPv4 internet. Nothing in the platform deploys, finds or reads it: you deploy it
on a tenant of your own, read it by hand ([Reading the result](#reading-the-result)) and remove it when done.

It works on OpenShift Virtualization and OpenStack sites that offer IPv6-only placements with translation (site
capabilities `ipv6Only` and `ipv6Translation`; see
[Translation](../../../docs/platform/ipv6-realization.md#translation-translation)). AWS and Google Cloud sites
translate with the provider's own services; the probe is not built for them.

## What it deploys

| File | What |
|---|---|
| [probe-objects.yaml](probe-objects.yaml) | The tenant objects: an IPv6-only network with IPv6 from the platform and internet access, an environment per site type, one placement of the network per site |
| [probe-user-data.yaml](probe-user-data.yaml) | The probe's cloud-config (Fedora): clatd as CLAT for IPv4 literals, the check script `shc-ipv6-probe` run every minute by a systemd timer, an HTTP server on `__PORT__` that serves the last run at `/metrics`, and `ignore-auto-dns` (below) |
| [vm-cloudvirt.yaml](vm-cloudvirt.yaml) | The probe on OpenShift Virtualization: a VirtualMachine with the l2bridge binding, user data in a Secret, network data with the translation resolvers |
| [server-cloudoso.md](server-cloudoso.md) | The probe on OpenStack: the security group and the `openstack server create` command, booted from a config drive |

1. **A tenant of your own.** An Entity for the tests (for example `example-platform`, namespace
   `entity-example-platform`), like any tenant ([entity/example-corp.yaml](../../../samples/entity/example-corp.yaml)).
2. **The network, environments and placements** ([probe-objects.yaml](probe-objects.yaml)): an IPv6-only network
   with IPv6 from the platform (`platformPrefixes` with `family: 6`, no `prefixes`) and `egress.internet: true`; an
   environment on every site to test; one placement of the network per environment with only `ipv6`. Wait until the
   placements are ready: their `status.ipv6.translation` names the site's translation prefix and resolvers, the
   values for `__PREF64__` and `__RESOLVERS__` below.
3. **The probe** on every placement, from [probe-user-data.yaml](probe-user-data.yaml) with the placeholders
   replaced:
   - OpenShift Virtualization: [vm-cloudvirt.yaml](vm-cloudvirt.yaml), in the site namespace the placement serves
     (the placement's `status.namespaces`), user data in a Secret.
   - OpenStack: [server-cloudoso.md](server-cloudoso.md), in the environment's project on the placement's network,
     booted from a config drive.

The probe's metrics port must be reachable from where you read it ([Through the gateway](#through-the-gateway)): on
OpenStack open it in the server's security group; on OpenShift Virtualization allow it in any network policy of the
namespace. The probe's clock must be right (Fedora keeps it with chrony): you compare the time of its last run with
your own.

Like a tenant's guest on an IPv6-only placement, the probe uses only the translation resolvers: its user data installs
`ignore-auto-dns`, a unit that runs at every boot and makes the NIC with the IPv6 default route ignore resolvers
handed out by DHCP (NetworkManager `ipv6.ignore-auto-dns` and `ipv4.ignore-auto-dns`, or systemd-networkd
`UseDNS=false`) while its connection has resolvers of its own from the network data. On OpenShift Virtualization
DHCPv6 also hands out the cluster's DNS service, which does no DNS64: without the unit, one brief outage of the
translation resolvers moves the guest to that server for good, and `pref64` and `name4` fail while `resolver` still
passes. `resolvectl status` in the guest lists the translation resolvers only. The unit writes what it did and the
link's resolvers to the console (OpenShift Virtualization: `oc logs <virt-launcher pod> -c guest-console-log`).

## Placeholders

| Placeholder | In | What to put there |
|---|---|---|
| `__PREF64__` | probe-user-data.yaml | The site's translation prefix, a /96 (the placement's `status.ipv6.translation.pref64`). One probe per site, so one value per copy. |
| `__RESOLVERS__` | probe-user-data.yaml, vm-cloudvirt.yaml | The site's translation resolvers, comma-separated (the placement's `status.ipv6.translation.resolvers`). |
| `__IPV4_ONLY_NAME__` | probe-user-data.yaml | A public name with an IPv4 address only, served over HTTPS: reached by name through the translator. |
| `__IPV4_LITERAL__` | probe-user-data.yaml | A public IPv4 address that answers HTTP on port 80: reached as a literal through the probe's CLAT. |
| `__IPV6_NAME__` | probe-user-data.yaml | A public name with an IPv6 address, served over HTTPS: reached natively. |
| `__IPV6_PING_TARGET__` | probe-user-data.yaml | A public IPv6 address that answers ping: the 1400-byte path check. |
| `__DENIED_IPV4__` | probe-user-data.yaml | An IPv4 address the platform's egress denies (one of its internal ranges): embedded into the translation prefix, it must stay unreachable. |
| `__PORT__` | probe-user-data.yaml, server-cloudoso.md | The metrics port (for example 9464). |
| `__WORKLOAD__` | vm-cloudvirt.yaml, server-cloudoso.md | The probe's name (the VM or the server), for example `probe`. |
| `__NAMESPACE__` | vm-cloudvirt.yaml | The site namespace the placement serves (its `status.namespaces`). |
| `__MAC__` | vm-cloudvirt.yaml | A locally administered MAC address for the VM's NIC (for example `02:00:00:00:00:01`); the network data matches it. |
| `__PLACEMENT_NETWORK__` | server-cloudoso.md | The placement's network on the site (the network shared to the environment's project). |
| `__IMAGE__`, `__FLAVOR__` | server-cloudoso.md | A Fedora cloud image (Fedora 44 or later: its clatd 2.1 keeps the single address the network assigned; older clatd releases fail under port security) and a small flavor of the site. |
| `__SECURITY_GROUP__` | server-cloudoso.md | A security group for the probe. |
| `__SCRAPE_SOURCE__` | server-cloudoso.md | The IPv6 range you read the probe from: through the gateway, the border gateway nodes' ICMPv6 sources (the realization's `core.icmpSource6`). |

## The checks

Each run writes `shc_ipv6_probe_check{check="<name>"}` 1 (passed) or 0 (failed) for every check below, and
`shc_ipv6_probe_run_timestamp_seconds`, the time the run finished (seconds since the epoch).

| Check | Passes when |
|---|---|
| `address` | The NIC has a global IPv6 address (router advertisements and DHCPv6 worked). |
| `resolver` | Every resolver of `__RESOLVERS__` answers an A query. |
| `pref64` | The AAAA answer for `ipv4only.arpa` lies inside `__PREF64__` (the resolvers synthesize with the site's prefix). |
| `name4` | `https://__IPV4_ONLY_NAME__/` answers over IPv6 (synthesized address, translated by the platform). |
| `literal4` | `http://__IPV4_LITERAL__/` answers over IPv4 through the CLAT (any HTTP status). |
| `name6` | `https://__IPV6_NAME__/` answers over native IPv6. |
| `mtu` | A ping of 1400 bytes on the wire with "don't fragment" reaches `__IPV6_PING_TARGET__`. |
| `deny` | `__DENIED_IPV4__` embedded in `__PREF64__` is refused (the check passes when the connection fails). |

Each run also writes the information metric `shc_ipv6_probe_resolver_info{server="<address>",translation="0|1"} 1`:
the resolver the guest's stub resolver used at the end of the run (systemd-resolved's current server) and whether it is
one of `__RESOLVERS__`. It is not a check; when `pref64` and `name4` fail, `translation="0"` says the guest has left
the translation resolvers.

## Reading the result

The probe **passes** when you reach it, its last run is at most 180 seconds older than your clock (it runs every
60 seconds), and every `shc_ipv6_probe_check` line reads 1. A last run that is older means the timer or the guest
stopped: treat it as failed whatever the checks say.

### The probe's address

- **OpenShift Virtualization:** on the site's cluster, the VirtualMachineInstance in the placement's site namespace
  (the probe's guest agent reports the addresses):
  ```bash
  oc get vmi __WORKLOAD__ -n __NAMESPACE__ -o jsonpath='{range .status.interfaces[*]}{.ipAddresses}{"\n"}{end}'
  ```
- **OpenStack:** the port of the server on the placement's network
  ([server-cloudoso.md](server-cloudoso.md)).

Take the first global IPv6 address: not link-local (`fe80::/10`), unique local (`fc00::/7`) or multicast
(`ff00::/8`).

### From the guest

The quickest read, from the guest's console (`virtctl console`, or the OpenStack console):

```bash
cat /var/lib/shc-ipv6-probe/metrics
date +%s    # compare with shc_ipv6_probe_run_timestamp_seconds
```

It shows what the guest sees, not whether the platform's side reaches the guest.

### Through the gateway

The read the fabric's own path takes: from the site's gateway on a border gateway node, inside the placement's
VRF. Run it on a node of the fabric (the HybridFabric's `spec.borderGateway.nodes`), as root.

1. **The gateway namespace.** The CloudGateway of the site on that fabric (`spec.cloudRef.name` is the site,
   `spec.fabricRef.name` the fabric); on the node its namespace is `gw-<CloudGateway name>`:
   ```bash
   oc get cloudgateway -n <platform namespace> -o json \
     | jq -r '.items[] | select(.spec.cloudRef.name == "<site>" and .spec.fabricRef.name == "<fabric>") | .metadata.name'
   ```
2. **The VRF.** The placement's `status.realization.vrf` (for example `t51001`).
3. **The source address.** The VRF device's own global IPv6 address in that namespace, the gateway's ICMPv6 source
   (`core.icmpSource6`). The probe's reply must route back to it; without it the kernel picks an address the site may
   not route back:
   ```bash
   ip -n gw-<gateway> -j -6 addr show dev <vrf> scope global | jq -r '.[0].addr_info[0].local'
   ```
4. **The read:**
   ```bash
   ip netns exec gw-<gateway> ip vrf exec <vrf> \
     curl -6 -s -f --max-time 5 --interface <source> 'http://[<probe address>]:<port>/metrics'
   date +%s
   ```

The gateways announce the networks' prefixes to the site, not each node's own source address. A site with more than
one path to the gateway (equal-cost paths over both nodes, flows spread by their addresses and ports) can send the
reply to the other node, which drops it. So a read that times out is not yet a failure: try up to three times from
the same node (each `curl` is a new connection, with a new source port), then from each other node in turn. The probe
is unreachable only when no node reaches it.

## Checking that the probe sees a failure

On one site, replace the probe with a copy whose user data names, among `__RESOLVERS__`, an address where no resolver
answers; keep the probe's name (OpenShift Virtualization: change the Secret and restart the VM; OpenStack: delete
the server and create it again). Its `resolver` check reads 0 within a minute, with the checks that need a name (the
probe has no other resolver). Put the original user data back; the next run reads 1 again. On OpenStack the
server's address may change: read it again.

## Removing it

Delete the probe (the VirtualMachine and its Secret, or the server), then the objects of
[probe-objects.yaml](probe-objects.yaml): the placements, the environments and the network.
