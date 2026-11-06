# The IPv6 translation probe on an OpenStack site

One Fedora server in the probe environment's project (the OpenStack environment `probe-osp` of
[probe-objects.yaml](probe-objects.yaml)), on the network of the probe network's placement there, named
`__WORKLOAD__`. Replace every placeholder first ([README](README.md)).

The server boots from a config drive: the placement's IPv6 subnet carries the translation resolvers, so the drive's
network data configures the address, the route and the resolvers without DHCPv4 or the metadata service.

```bash
# the probe's metrics port, open to where the tester reads it from (README.md, "Reading the result")
openstack security group create __SECURITY_GROUP__
openstack security group rule create --ethertype IPv6 --protocol tcp --dst-port __PORT__ \
  --remote-ip __SCRAPE_SOURCE__ __SECURITY_GROUP__
openstack security group rule create --ethertype IPv6 --protocol ipv6-icmp __SECURITY_GROUP__

openstack server create --config-drive true --user-data probe-user-data.yaml \
  --network __PLACEMENT_NETWORK__ --image __IMAGE__ --flavor __FLAVOR__ \
  --security-group __SECURITY_GROUP__ __WORKLOAD__
```

The probe's address is the first global IPv6 fixed address of the server's port on the placement's network:

```bash
openstack port list --server __WORKLOAD__ --network __PLACEMENT_NETWORK__ -c "Fixed IP Addresses" -f json
```

Replace the server (delete it and create it again with the same name) to change the user data; its address may change.
