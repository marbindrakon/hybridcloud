"""On the SRv6 core the root job removes the file that loads the MPLS modules at boot (a node that ran the MPLS core
before would otherwise load them at every boot, and the orphan check reports the file on an SRv6 fabric)."""
import os
import unittest

import play_helper as ph

TASKS = ph.load(os.path.join(ph.ROOT, "eda/rulebooks/roles/hybridfabric_provision/tasks/node_root.yml"))
FILE = "/etc/modules-load.d/shc-fabric.conf"


class ModulesLoadFile(unittest.TestCase):
    def test_written_on_mpls_only(self):
        t = ph.by_name(TASKS, "{{ bgw_node.name }}: load the MPLS modules at boot")
        self.assertEqual(t["ansible.builtin.copy"]["dest"], FILE)
        self.assertEqual(t["when"], "fab_node_transport | default('mpls') == 'mpls'")

    def test_removed_on_srv6_only(self):
        t = ph.by_name(TASKS, "{{ bgw_node.name }}: no MPLS modules at boot on the SRv6 core")
        self.assertEqual(t["ansible.builtin.file"], {"path": FILE, "state": "absent"})
        self.assertEqual(t["when"], "fab_node_transport | default('mpls') == 'srv6'")
        self.assertEqual(t["delegate_to"], "{{ bgw_node.name }}")

    def test_the_removal_follows_the_write(self):
        names = [t.get("name") for t in ph.walk(TASKS)]
        self.assertEqual(names.index("{{ bgw_node.name }}: no MPLS modules at boot on the SRv6 core"),
                         names.index("{{ bgw_node.name }}: load the MPLS modules at boot") + 1)


if __name__ == "__main__":
    unittest.main()
