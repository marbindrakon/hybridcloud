# Translation canary

For the platform team. The translation canary tells the platform whether IPv6-only placements reach the IPv4
internet on every site that offers them: one small probe VM per site, on an IPv6-only network of the platform team's
own, runs the checks a tenant's guest depends on every minute and serves the results. The platform reads them from
the site's gateway and reports them on the fabric (condition `IPv6Translation`, Warning event
`IPv6TranslationCanaryFailed`); see [Translation canary](../../../docs/platform/ipv6-realization.md#translation-canary).

The canary is a workload like a tenant's: nothing in it is special to the platform except the realization keys that
name it. The platform never reads a probe it was not told about, and finds the probe's address itself (from the site,
not from labels on tenant objects).

## Deploying it

1. **A tenant of the platform team's own.** The platform team creates an Entity for itself (for example
   `example-platform`, namespace `entity-example-platform`), like any tenant
   ([entity/example-corp.yaml](../../entity/example-corp.yaml)).
2. **The network, environments and placements** ([canary-objects.yaml](canary-objects.yaml)): an IPv6-only network
   with IPv6 from the platform (`platformPrefixes` with `family: 6`, no `prefixes`) and `egress.internet: true`; an
   environment on every site that offers IPv6-only placements with translation (site capabilities `ipv6Only` and
   `ipv6Translation`); one placement of the network per environment with only `ipv6`. Wait until the placements are
   ready: their `status.ipv6.translation` names the site's translation prefix and resolvers, the values for
   `__PREF64__` and `__RESOLVERS__` below.
3. **The probe** on every placement, with the same name everywhere (here `canary`), from
   [probe-user-data.yaml](probe-user-data.yaml) with the placeholders replaced:
   - OpenShift Virtualization: [vm-cloudvirt.yaml](vm-cloudvirt.yaml), in the site namespace the placement serves
     (the placement's `status.namespaces`), user data in a Secret.
   - OpenStack: [server-cloudoso.md](server-cloudoso.md), in the environment's project on the placement's network,
     booted from a config drive.
4. **The realization** (the installation's `FABRIC_REALIZATION`):
   ```text
   canaries:
     ipv6Translation:
       network: entity-example-platform/ipv6-canary   # "<namespace>/<name>" of the network
       workload: canary                               # the probe's name on every placement
       port: 9464                                     # __PORT__
       alertMinutes: 5                                # failed or stale this long: a Warning event
   ```
   Then run each site again (Retry now on the site), then the fabric. The site records where its probe answers, the
   fabric reads it on its next run and every period after.

The probe's metrics port must be reachable from the site's gateway: on OpenStack open it in the server's security group
(the platform team knows the gateway's source range); on OpenShift Virtualization allow it in any network policy of the
namespace. The probe's clock must be right (Fedora keeps it with chrony): the fabric compares the time of the probe's
last run with its own.

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
| `__PORT__` | probe-user-data.yaml, server-cloudoso.md | The metrics port, the realization's `canaries.ipv6Translation.port` (default 9464). |
| `__WORKLOAD__` | vm-cloudvirt.yaml, server-cloudoso.md | The probe's name, the realization's `canaries.ipv6Translation.workload`. |
| `__NAMESPACE__` | vm-cloudvirt.yaml | The site namespace the placement serves (its `status.namespaces`). |
| `__MAC__` | vm-cloudvirt.yaml | A locally administered MAC address for the VM's NIC (for example `02:00:00:00:00:01`); the network data matches it. |
| `__PLACEMENT_NETWORK__` | server-cloudoso.md | The placement's network on the site (the network shared to the environment's project). |
| `__IMAGE__`, `__FLAVOR__` | server-cloudoso.md | A Fedora cloud image and a small flavor of the site. |
| `__SECURITY_GROUP__` | server-cloudoso.md | A security group for the probe. |
| `__SCRAPE_SOURCE__` | server-cloudoso.md | The IPv6 range the fabric reads the probes from. |

## The checks

Each run writes `shc_ipv6_canary_check{check="<name>"}` 1 (passed) or 0 (failed) for every check below, and
`shc_ipv6_canary_run_timestamp_seconds`, the time the run finished.

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

A canary is **healthy** when the fabric reached it, its last run is at most three minutes old and every check is 1;
**stale** when the last run is older or the probe could not be reached. A site failed or stale for `alertMinutes` gets
a Warning event on the fabric on every fabric run while it lasts.

## Testing the alert

On one site, replace the probe with a copy whose user data names, among `__RESOLVERS__`, an address where no resolver
answers; keep the probe's name, so the realization still names it (OpenShift Virtualization: change the Secret and
restart the VM; OpenStack: delete the server and create it again). Its `resolver` check reads 0 within a minute. On
the next fabric run the fabric's `IPv6Translation` condition turns False with reason `CanaryFailed`, naming the site
and the `resolver` check; once the site has been failing for `alertMinutes`, every fabric run adds a Warning event
`IPv6TranslationCanaryFailed` on the fabric. Put the original user data back; the next runs report the site healthy
again and the events stop. If the probe's address changed (OpenStack), run the site again first.
