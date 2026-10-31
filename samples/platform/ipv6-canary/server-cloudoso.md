# The translation canary probe on an OpenStack site

One Fedora server in the canary environment's project (the OpenStack environment `canary-osp` of
[canary-objects.yaml](canary-objects.yaml)), on the network of the canary network's placement there. Its name is the
realization's `canaries.ipv6Translation.workload`. Replace every placeholder first ([README](README.md)).

The server boots from a config drive: the placement's IPv6 subnet carries the translation resolvers, so the drive's
network data configures the address, the route and the resolvers without DHCPv4 or the metadata service.

```bash
# the probe's metrics port, open to the fabric's readers (the platform team knows the source range)
openstack security group create __SECURITY_GROUP__
openstack security group rule create --ethertype IPv6 --protocol tcp --dst-port __PORT__ \
  --remote-ip __SCRAPE_SOURCE__ __SECURITY_GROUP__
openstack security group rule create --ethertype IPv6 --protocol ipv6-icmp __SECURITY_GROUP__

openstack server create --config-drive true --user-data probe-user-data.yaml \
  --network __PLACEMENT_NETWORK__ --image __IMAGE__ --flavor __FLAVOR__ \
  --security-group __SECURITY_GROUP__ __WORKLOAD__
```

The site job finds the server by its name in the environment's project and takes its first global IPv6 address on the
placement's network; nothing else needs to be recorded. Replace the server (delete it and create it again with the
same name) to change the user data; the next site run records the new address.
