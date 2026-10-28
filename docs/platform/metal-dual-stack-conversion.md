# Convert an OpenShift Virtualization site to dual-stack

For the platform team. An OpenShift Virtualization site can carry IPv6 placements only when its cluster is
dual-stack: OVN-Kubernetes refuses an IPv6 primary network on a single-stack cluster, and the site's probe keeps
`status.capabilities.ipv6` false whatever the realization switch says
([IPv6 realization, Placements](ipv6-realization.md#placements-placement)). A new cluster is built dual-stack from its
values (`provider.agent.networking` in [mce-cluster-build](../../charts/charts/mce-cluster-build/README.md)). This
page converts a running bare-metal cluster in place, day 2, without rebuilding it. The platform's automation does
none of this: the cluster is yours, the steps are `oc` and NetworkManager.

What the conversion does: every node gets an IPv6 address on the node network, the cluster declares an IPv6 machine
network, cluster network and service network, OVN-Kubernetes becomes dual-stack and the Machine Config Operator
reboots every node once. What it does not need: IPv6 VIPs (API and ingress stay reachable over IPv4), an IPv6 VTEP
(the site's EVPN sessions stay on IPv4 and carry the IPv6 prefixes as Type-5 routes), a migration network, or any
change to the site gateway. What stays afterwards: the cluster is dual-stack for good; going back means a rebuild
(see [Rollback](#rollback-a-rebuild)).

The page uses documentation addresses: node network `198.51.100.0/24` and `2001:db8:1:238::/64` with the router at
`::1` and the nodes at `::21`, `::22`, `::23`; cluster network `fd01::/48` (hostPrefix 64) and service network
`fd02::/112`, OpenShift's own examples. Your installation's values live outside Git
([Lab configuration policy](../lab-config.md)).

## Before you start

**What you need**
- An IPv6 prefix for the node network (a /64 on the nodes' VLAN) and a router address in it that answers from every
  node. The router must not expect the nodes to autoconfigure: either its router advertisements mark the prefix
  `no-autoconfig`, or the node step below turns router advertisements off on the nodes (it does both). Check from
  anywhere on that VLAN: `ping -6 <router>`.
- One address per node, chosen by a rule you can read back later (the IPv4 host part, `::21` for `.21`).
- The cluster network and the service network for IPv6: `fd01::/48` with hostPrefix 64 and `fd02::/112` unless the
  installation uses other ranges. They must not overlap any tenant network's IPv6 space; the placement allocator
  reads them from the cluster afterwards and steps around them by itself, so no realization entry is needed.
- The name of the NetworkManager profile `br-ex` is built on (the VLAN profile, `bond0.100` in the examples): on a
  node, `nmcli -g connection.id,device connection show --active`.
- cluster-admin on the site's cluster, and a window of about ten minutes per node (two nodes in parallel on a
  cluster with separate control-plane and worker pools; one at a time on a compact cluster).

**Pre-checks** (every one must hold before the cluster step):
1. Both machine config pools are updated and none is paused: `oc get mcp` shows `UPDATED True`, `UPDATING False`,
   `DEGRADED False`; `oc get co` shows every operator available and not degraded.
2. **Every EVPN network on the site selects exactly one namespace.** With two or more namespaces on one network the
   node's VTEP cannot be re-created when OVN-Kubernetes restarts on the node (product issue 1 below), and the whole
   site loses the fabric until the extra namespaces are gone. The placement job creates one network per placement
   that selects the namespaces naming the network; check that no network name appears twice:
   ```sh
   oc get net-attach-def -A -o custom-columns=NAMESPACE:.metadata.namespace,NAME:.metadata.name --no-headers \
     | awk '{print $2}' | sort | uniq -d          # must print nothing
   ```
3. **Stop the VMs, or accept the crash-loops.** A node that receives live-migrated VMs during the drains
   crash-loops OVN-Kubernetes until it is itself rebooted (product issue 2): new pods on it get no network for the
   rest of the roll. Either stop the VMs for the roll (`runStrategy: Halted`, note the value each VM had) or let
   them migrate and expect the crash-loops to clear as the roll reaches those nodes.
4. If a descheduler runs with automatic VM migration (`KubeDescheduler` with the `KubeVirtRelieveAndMigrate`
   profile), set its `mode` to `Predictive` for the duration of the roll and back afterwards: automatic migrations
   feed product issue 2.
5. The site's placements are Ready, their networks report `NetworkCreated` and their route advertisements
   `Accepted`, and the site gateway shows every node's EVPN session established. Record the counts: the verification
   compares against them.
6. Keep a probe running during the roll: a pod on a placement network pinging a VM on another node, and the site
   gateway's session counts. A loss of a few seconds per node is expected; minutes are not.

## 1. Node step: IPv6 on the node network interface

Give every node its address and default route on `br-ex`, through NetworkManager so they survive reboots and the
`configure-ovs` rebuild of `ovs-if-br-ex` at boot, and live through `ip(8)` so nothing is re-activated. Change the
two profiles `ovs-if-br-ex` **and** the profile `br-ex` is built on; never `nmcli device reapply br-ex` (it drops the
node's networking), never an NMState policy on `br-ex`, never the bond itself.

```sh
# node-v6.sh, one node at a time:  oc debug node/<node> -- chroot /host bash -s < node-v6.sh
ADDR=2001:db8:1:238::21/64     # this node's address in the node network
GW=2001:db8:1:238::1           # the node network's IPv6 router
BASE=bond0.100                 # the profile br-ex is built on (nmcli -g connection.id,device connection show --active)
for c in ovs-if-br-ex "$BASE"; do
  nmcli connection modify "$c" ipv6.method manual ipv6.addresses "$ADDR" ipv6.gateway "$GW" \
    ipv6.addr-gen-mode eui64 ipv6.ip6-privacy 0 ipv6.never-default no
done
sysctl -w net.ipv6.conf.br-ex.disable_ipv6=0 net.ipv6.conf.br-ex.accept_ra=0 net.ipv6.conf.br-ex.autoconf=0
ip -6 addr replace "$ADDR" dev br-ex nodad
ip -6 route replace default via "$GW" dev br-ex
ip -br -6 addr show br-ex; ip -6 route show default
```

Then, from the node: `ping -6 -c 3 <router>`, `ping -6 -c 3 <another node>`, and a don't-fragment ping at the
node network's MTU (`ping -6 -M do -s 8952 -c 3 <router>` for an MTU of 9000: payload = MTU − 48). Every node must
pass before the cluster step. The nodes keep `net.ipv6.conf.all.forwarding=1` (set by the cluster's global IP
forwarding) and take no router advertisements.

Why first: OVN-Kubernetes reads the IPv6 default route of `br-ex` when it rebuilds its gateway configuration during
the cluster step. Without it the node's gateway has one next hop only and IPv6 from the pods goes nowhere.

## 2. Cluster step: declare the IPv6 networks

Two patches, nothing else. Merge patches replace the lists, so write the IPv4 entries again.

```sh
oc patch infrastructure cluster --type=merge -p '{"spec":{"platformSpec":{"baremetal":{"machineNetworks":
  ["198.51.100.0/24","2001:db8:1:238::/64"]}}}}'
oc patch network.config cluster --type=merge -p '{"spec":{"clusterNetwork":
  [{"cidr":"10.128.0.0/14","hostPrefix":23},{"cidr":"fd01::/48","hostPrefix":64}],
  "serviceNetwork":["172.30.0.0/16","fd02::/112"]}}'
```

- `status.platformStatus.baremetal.machineNetworks` follows within seconds; the API and ingress VIPs don't change
  and don't blip.
- `network.config` `status.clusterNetwork` and `status.serviceNetwork` show both families within a minute. The node
  annotations gain an IPv6 node subnet (`fd01:0:0:<n>::/64`) and an IPv6 primary address at the same time.
- **Do not restart the OVN-Kubernetes node pods by hand.** The network operator does not roll the daemon set after
  the patch (its generation is unchanged, the pods keep running), and it doesn't have to: the Machine Config
  Operator renders new configurations at once (the kubelet gains `--node-ip=${KUBELET_NODE_IPS}`) and reboots
  every node, which restarts the pods one node at a time with fresh EVPN devices. Restarting them all at once only
  takes the whole site down for a few seconds and meets product issue 1 everywhere at the same instant.

## 3. The roll

The Machine Config Operator drains and reboots each node, about ten minutes per node, one node per pool at a time.
Pause the control-plane pool (`oc patch mcp master --type=merge -p '{"spec":{"paused":true}}'`) to roll the workers
first and the control plane afterwards; unpause at the end. Watch:

```sh
oc get mcp; oc get nodes
oc -n openshift-ovn-kubernetes get pods -l app=ovnkube-node -o wide         # one restart per node, with its reboot
oc get network.config cluster -o jsonpath='{.status.clusterNetwork}{" "}{.status.serviceNetwork}{"\n"}'
```

What you will see:
- Each rebooted node comes back with two `InternalIP` addresses, its EVPN devices (the VXLAN device and the per-network
  bridges) re-created, and `fd69::2/112` added to `br-ex` by OVN-Kubernetes next to the address from the node step.
- With VMs running, the nodes that received live-migrated VMs crash-loop their `ovnkube-controller` container
  (product issue 2). Pods scheduled to them meanwhile get no network. The roll still completes: the crash-loop ends
  when the node is itself drained and rebooted. Nothing to do but wait; don't delete the pods.
- The probe loses a few seconds at each node's reboot. The site gateway's EVPN session to the node drops and comes
  back; the placements' prefixes stay in the gateway's VRF from the other nodes.

The roll is complete when both pools are `UPDATED True` and every node is `Ready` with the new configuration.

## 4. Post steps

1. **Make the cluster DNS dual-stack.** The DNS operator leaves `dns-default` single-stack, and the running DNS pods
   keep their IPv4-only network annotations across the reboots (daemon set pods keep their objects), so an IPv6
   service address would have no endpoints. Both steps are needed; without them guests on IPv6-only placements get
   no resolver (OVN-Kubernetes offers the DNS service's IPv6 address over DHCPv6 only once it exists):
   ```sh
   oc -n openshift-dns patch svc dns-default --type=merge \
     -p '{"spec":{"ipFamilyPolicy":"PreferDualStack","ipFamilies":["IPv4","IPv6"]}}'
   oc -n openshift-dns delete pods -l dns.operator.openshift.io/daemonset-dns=default
   ```
   The DNS operator accepts the change (Available, not Degraded). Other pods that must serve IPv6 need the same
   re-creation; application pods usually don't.
2. Resume the descheduler (`mode: Automatic`) and start the VMs you stopped, with the `runStrategy` each one had.
3. **Let the site see both families:** Retry now on the site (write a new value into the annotation
   `ansible.sdk.operatorframework.io/reconcileNow` of its `CloudInfrastructure`). The site job probes the cluster
   network status; with both families present and the realization switch still off, `status.capabilities.ipv6`
   stays false and nothing changes for tenants.
4. **Turn IPv6 on for the site** when its data path is ready (the fabric transport and the site gateway carry IPv6):
   `placement.ipFamiliesBySite.<site>: [4, 6]` in the installation's `FABRIC_REALIZATION`, then Retry now on the
   site again ([IPv6 realization, Site capabilities](ipv6-realization.md#site-capabilities)). From then on the site
   offers IPv6 placements, and tenants' forms show the IPv6 inputs for it.

## 5. Verification

```sh
oc get nodes -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.addresses[?(@.type=="InternalIP")].address}{"\n"}{end}'
oc get network.config cluster -o jsonpath='{.status.clusterNetwork}{" "}{.status.serviceNetwork}{"\n"}'
oc get clusteruserdefinednetwork -o custom-columns=NAME:.metadata.name,CREATED:'.status.conditions[?(@.type=="NetworkCreated")].status'
oc get routeadvertisements -o custom-columns=NAME:.metadata.name,ACCEPTED:'.status.conditions[?(@.type=="Accepted")].status'
oc -n openshift-dns get svc dns-default -o jsonpath='{.spec.ipFamilyPolicy} {.spec.clusterIPs}{"\n"}'
```

- Every node lists an IPv4 and an IPv6 `InternalIP`; the network status shows both families; every network is
  `True`/`True` and the DNS service has two cluster IPs.
- The IPv4 path is unchanged: the site gateway holds the placements' IPv4 prefixes from every node again, with as
  many EVPN sessions as before, and the probe from pre-check 6 passes as it did before the roll.
- After the realization switch: the site reports `status.capabilities.ipv6: true`, and a test placement with
  `ipv6: {prefixLength: 64}` goes Ready; a VM on it gets its address by DHCPv6 (its link-local address must be the
  EUI-64 one, see
  [Guests on IPv6](../../architecture/docs/tutorial/09-hybrid-networks.md#guests-on-ipv6)). Delete the test
  placement afterwards.

## Rollback: a rebuild

The product allows removing the added cluster and service network entries again, which starts another roll; the
machine network entry and the node addresses are not covered, and the path is untested here. Treat the conversion
as one way: the clean rollback of a converted site is a rebuild, single-stack or dual-stack, from its build values
(`provider.agent.networking` and, when wanted, `cluster.apiVIPs` / `cluster.ingressVIPs` in
[mce-cluster-build](../../charts/charts/mce-cluster-build/README.md)). A rebuild loses what was installed by hand on
the cluster and every VM on it; move or back up the VMs first and keep a list of the hand-installed pieces. A
dual-stack cluster is harmless for IPv4-only tenants, so there is rarely a reason to go back.

## Known product issues (OpenShift 4.22, OVN-Kubernetes with EVPN)

Both were met during the conversion, both are independent of IPv6, and both are reported to the vendor.

1. **Two namespaces on one EVPN network break the node's VTEP at the next restart.** A Layer2 EVPN primary network
   whose namespace selector matches two or more namespaces is listed once per namespace by the EVPN node
   controller, and the netlink device manager refuses the VXLAN device with `duplicate VID <n>` the next time
   OVN-Kubernetes (re)starts on a node, on a reboot too. The node then has no VXLAN device and no network bridges:
   every placement on that node is cut off from the fabric. **Symptom:** `oc -n openshift-ovn-kubernetes logs
   <ovnkube-node pod> -c ovnkube-controller` repeats `failed to apply VXLAN ... duplicate VID`, the site gateway
   loses the site's prefixes. **Repair:** remove the extra namespaces (or their label naming the network); the devices
   come back within seconds. The placement job keeps one namespace per network; the check is pre-check 2.
2. **OVN-Kubernetes panics on a node that hosts a live-migrated VM whose source pod is gone.** Once the source
   launcher pod of a migration has been deleted, the migration status it derives has no source pod, and the EVPN pod
   controller dereferences it (`evpn_pod_controller.go`, `shouldDeleteNeighbors`) at every start of
   `ovnkube-controller` on the target node. **Symptom:** the container crash-loops with `panic: ... nil pointer
   dereference`; new pods on the node get no network. **Repair:** reboot the node (the roll does it), or remove the
   VM's launcher pod. Until fixed, roll EVPN sites with the VMs stopped, or expect each drain target to crash-loop
   until its own reboot.
