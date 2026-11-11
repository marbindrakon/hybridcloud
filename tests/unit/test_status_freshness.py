"""Status that must not go stale: the network wakes its placements when its IPv6 internet row changes
(hybridnetwork_provision main.yml; the placements copy the row, ipv6_status.yml); the realization defaults document the
keys the code reads (eda/common/vars/fabric_realization.yml). The plays run in ansible-playbook (skipped without
it). Generic names only."""
import os
import sys
import unittest

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import HAVE_ANSIBLE, ROOT, by_name, load, run_play, walk, write_out  # noqa: E402

ROLES = os.path.join(ROOT, "eda", "rulebooks", "roles")
HN = os.path.join(ROLES, "hybridnetwork_provision", "tasks")
DEFAULTS = os.path.join(ROOT, "eda", "common", "vars", "fabric_realization.yml")
WAKE = "Wake the placements whose IPv6 internet row changed"


def placement(name, network="n1", source="Network", family=6, deleting=False):
    md = {"name": name}
    if deleting:
        md["deletionTimestamp"] = "2026-01-01T00:00:00Z"
    subnets = [{"family": 4, "prefix": "10.60.1.0/24"}]
    if family == 6:
        subnets.append({"family": 6, "prefix": "2001:db8:1::/64", "source": source})
    return {"metadata": md, "spec": {"network": network}, "status": {"subnets": subnets}}


ROW = {"family": 6, "state": "ready", "mode": "Routed"}


@unittest.skipUnless(HAVE_ANSIBLE, "ansible-playbook not installed")
class PlacementWake(unittest.TestCase):
    def wake(self, before, after, placements):
        t = by_name(load(os.path.join(HN, "main.yml")), WAKE)
        probe = {"ansible.builtin.set_fact": {"_due": "{{ %s }}" % t["when"].strip(),
                                              "_crs": t["vars"]["nudge_crs"]}}
        out = run_play([dict(probe, vars={k: v for k, v in t["vars"].items() if k != "nudge_crs"}),
                        write_out("{'due': _due, 'crs': _crs}")],
                       {"fab_st": {"egress": {"families": before}} if before is not None else {},
                        "hn_egress": {"families": after} if after is not None else {},
                        "fab_name": "n1", "fab_namespace": "acme",
                        "_hn_placements": {"resources": placements}})
        return out["due"], out["crs"]

    def test_row_changes_wake_the_fabric_path_placements(self):
        pls = [placement("p-net"), placement("p-prov", source="Provider"), placement("p-v4", family=4),
               placement("p-other", network="n2"), placement("p-gone", deleting=True)]
        degraded = dict(ROW, reason="Degraded", message="m")
        for before, after in (([ROW], [degraded]), ([degraded], [ROW]), ([ROW], []), (None, [ROW]),
                              ([ROW], [dict(ROW, state="removing")])):
            due, crs = self.wake(before, after, pls)
            self.assertTrue(due, (before, after))
            self.assertEqual(crs, [{"kind": "NetworkPlacement", "namespace": "acme", "name": "p-net"}])

    def test_no_wake_while_the_row_stays(self):
        due, _ = self.wake([ROW, {"family": 4, "state": "ready"}], [{"family": 4, "state": "pending"}, ROW],
                           [placement("p-net")])
        self.assertFalse(due)
        self.assertFalse(self.wake(None, None, [])[0])

    def test_after_the_network_status_write(self):
        names = [t.get("name") for t in walk(load(os.path.join(HN, "main.yml")))]
        self.assertLess(names.index("Report the network ready"), names.index(WAKE))


class RealizationDefaults(unittest.TestCase):
    def test_documented_keys_keep_behaviour(self):
        with open(DEFAULTS) as fh:
            d = yaml.safe_load(fh)["fabric_realization_defaults"]
        # platform reach is retired (infrastructure.hub and the service VRF replace it, off by default; the ports the
        # code falls back to); the site's own domain
        self.assertNotIn("platformSources", d["egress"])
        self.assertNotIn("platformPorts", d["egress"])
        self.assertEqual(d["infrastructure"], {"hub": []})
        self.assertFalse(d["service"]["enabled"])
        self.assertEqual(d["service"]["ports"], [6443, 443])
        self.assertEqual(d["openstack"]["projectDomain"], "")
        sys.path.insert(0, os.path.join(ROOT, "eda", "common", "files"))
        import shc_fabric as F
        with open(F.__file__) as fh:
            src = fh.read()
        self.assertIn('sv.get("ports") or [6443, 443]', src)
        with open(os.path.join(ROLES, "cloudgateway_provision", "tasks", "site_openstack.yml")) as fh:
            site = fh.read()
        self.assertIn("fab_real.openstack.projectDomain | default((ci_spec.openstack | default({})).projectDomain, true)",
                      site)


if __name__ == "__main__":
    unittest.main()
