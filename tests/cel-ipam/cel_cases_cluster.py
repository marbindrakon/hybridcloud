#!/usr/bin/env python3
"""CEL envtest for the PlatformOpenshift network choice and address ranges (spec.openstack.network /
externalNetwork, spec.networking), on the runner of cel_cases.py (same arguments; group "cnet-cluster").

  python3 -I tests/cel-ipam/cel_cases_cluster.py --crds <crds.yaml or a file of the CRD bases> --expect candidate

Documentation ranges and example names only.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import cel_cases as K  # noqa: E402

API = K.API


def po(name="c", type_="openstack", **blocks):
    spec = {"type": type_}
    if type_ == "openstack" and "openstack" not in blocks:
        blocks["openstack"] = {"environment": "env"}
    spec.update(blocks)
    return {"apiVersion": API, "kind": "PlatformOpenshift", "metadata": {"name": name}, "spec": spec}


G = "cnet-cluster"
OS = {"environment": "env"}
NETS = {"clusterNetwork": "10.128.0.0/14", "hostPrefix": 23, "serviceNetwork": "172.30.0.0/16",
        "machineNetwork": "10.0.0.0/16"}
C, upd, setk, delk = K.C, K.upd, K.setk, K.delk

K.CASES[:] = [
    C("CN-01", G, "OpenStack cluster without a network choice", [("create", po(), "ok")]),
    C("CN-02", G, "OpenStack cluster on a hybrid network", [("create", po(openstack=dict(OS, network="n1")), "ok")]),
    C("CN-03", G, "OpenStack cluster on a named external network",
      [("create", po(openstack=dict(OS, externalNetwork="public")), "ok")]),
    C("CN-04", G, "both network and externalNetwork refused",
      [("create", po(openstack=dict(OS, network="n1", externalNetwork="public")), "deny:not both")]),
    C("CN-05", G, "empty network name refused", [("create", po(openstack=dict(OS, network="")), "deny")]),
    C("CN-06", G, "network cannot change",
      [("create", po(openstack=dict(OS, network="n1")), "ok"),
       (*upd("PlatformOpenshift", "c", setk(["openstack", "network"], "n2"))[:2], "deny:immutable")]),
    C("CN-07", G, "network cannot be added",
      [("create", po(), "ok"),
       (*upd("PlatformOpenshift", "c", setk(["openstack", "network"], "n1"))[:2], "deny:cannot be added")]),
    C("CN-08", G, "network cannot be removed",
      [("create", po(openstack=dict(OS, network="n1")), "ok"),
       (*upd("PlatformOpenshift", "c", delk(["openstack", "network"]))[:2], "deny:cannot be added")]),
    C("CN-09", G, "externalNetwork cannot be added later",
      [("create", po(), "ok"),
       (*upd("PlatformOpenshift", "c", setk(["openstack", "externalNetwork"], "public"))[:2], "deny:cannot be added")]),
    C("CN-10", G, "externalNetwork cannot change",
      [("create", po(openstack=dict(OS, externalNetwork="public")), "ok"),
       (*upd("PlatformOpenshift", "c", setk(["openstack", "externalNetwork"], "other"))[:2], "deny:immutable")]),
    C("CN-11", G, "other spec fields still change", [("create", po(openstack=dict(OS, network="n1")), "ok"),
       (*upd("PlatformOpenshift", "c", setk(["openstack", "workerCount"], 0))[:2], "ok")]),
    C("CN-20", G, "networking with every field", [("create", po(networking=dict(NETS)), "ok")]),
    C("CN-21", G, "networking non-canonical refused",
      [("create", po(networking={"clusterNetwork": "10.128.0.1/14"}), "deny:canonical form")]),
    C("CN-22", G, "networking IPv6 refused",
      [("create", po(networking={"serviceNetwork": "fd00:1::/112"}), "deny:IPv4 network prefix")]),
    C("CN-23", G, "clusterNetwork /23 refused", [("create", po(networking={"clusterNetwork": "10.128.0.0/23"}), "deny:/8 to /22")]),
    C("CN-24", G, "serviceNetwork /11 refused", [("create", po(networking={"serviceNetwork": "172.0.0.0/11"}), "deny:/12 to /24")]),
    C("CN-25", G, "machineNetwork /28 refused", [("create", po(networking={"machineNetwork": "192.0.2.0/28"}), "deny:/16 to /27")]),
    C("CN-26", G, "hostPrefix 29 refused", [("create", po(networking={"hostPrefix": 29}), "deny")]),
    C("CN-27", G, "hostPrefix not longer than clusterNetwork refused",
      [("create", po(networking={"clusterNetwork": "10.128.0.0/16", "hostPrefix": 16}), "deny:must be longer")]),
    C("CN-28", G, "networking immutable",
      [("create", po(networking=dict(NETS)), "ok"),
       (*upd("PlatformOpenshift", "c", setk(["networking", "hostPrefix"], 24))[:2], "deny:immutable")]),
    C("CN-29", G, "networking cannot be added later",
      [("create", po(), "ok"),
       (*upd("PlatformOpenshift", "c", setk(["networking"], {"hostPrefix": 24}))[:2], "deny:cannot be added")]),
    C("CN-30", G, "AWS cluster on a network may not set machineNetwork",
      [("create", po(type_="aws", aws={"environment": "a", "network": "n1"}, networking={"machineNetwork": "10.0.0.0/16"}),
        "deny:placement's subnet")]),
    C("CN-31", G, "AWS cluster without a network may set machineNetwork",
      [("create", po(type_="aws", aws={"environment": "a"}, networking={"machineNetwork": "10.0.0.0/16"}), "ok")]),
    C("CN-32", G, "GCP cluster on a network may not set machineNetwork",
      [("create", po(type_="gcp", gcp={"environment": "g", "network": "n1"}, networking={"machineNetwork": "10.0.0.0/16"}),
        "deny:placement's subnet")]),
    C("CN-33", G, "GCP on a network: cluster and service networks allowed",
      [("create", po(type_="gcp", gcp={"environment": "g", "network": "n1"},
                     networking={"clusterNetwork": "10.128.0.0/14", "serviceNetwork": "172.30.0.0/16"}), "ok")]),
    C("CN-34", G, "OpenStack on a network may set machineNetwork",
      [("create", po(openstack=dict(OS, network="n1"), networking={"machineNetwork": "10.0.0.0/16"}), "ok")]),
    C("CN-40", G, "OpenStack compact cluster (0 workers) accepted",
      [("create", po(openstack=dict(OS, workerCount=0, controlPlaneCount=3)), "ok")]),
    C("CN-41", G, "AWS 0 workers accepted", [("create", po(type_="aws", aws={"environment": "a", "workerCount": 0}), "ok")]),
    C("CN-42", G, "AWS 0 control plane refused",
      [("create", po(type_="aws", aws={"environment": "a", "controlPlaneCount": 0}), "deny")]),
    C("CN-43", G, "GCP 0 control plane refused",
      [("create", po(type_="gcp", gcp={"environment": "g", "controlPlaneCount": 0}), "deny")]),
    C("CN-50", G, "status keys of the record accepted",
      [("create", po(openstack=dict(OS, network="n1")), "ok"),
       ("status", ("PlatformOpenshift", "c", {
           "buildName": "shc-oso-acme-c", "network": "n1", "placement": "ns/p", "publish": "Internal",
           "machineNetwork": "10.0.0.0/16", "clusterNetwork": "10.128.0.0/14", "serviceNetwork": "172.30.0.0/16",
           "platformPrefixes": ["192.0.2.15/32", "192.0.2.16/32"],
           "floatingIPs": {"network": "net-1", "api": {"address": "192.0.2.15", "id": "fip-1"},
                           "ingress": {"address": "192.0.2.16", "id": "fip-2"}}}), "ok")]),
]

if __name__ == "__main__":
    sys.exit(K.main())
