"""The F12 reload against a real FRR 8.5 (optional: skipped without podman or without the FRR image present locally;
it never pulls). SHC_FRR_IMAGE names the image (default quay.io/frrouting/frr:8.5.3, the upstream build; RHEL ships
the same frr-reload.py 8.5.3).

The root's rendered file of plan A (a network with internet egress and IPv6 egress, test_service_vrf's fixtures) is
loaded into FRR in a private network namespace (the VRF devices only: no xe/xo device, which the failure does not
depend on); then the network is deleted (plan B) and frr-reload.py --reload runs to B:
- the file of f98cbae1 (an interface neighbour with a description), no removal step: frr-reload.py fails on the
  egress VRF's instance ("we failed to remove this command"): gate s6 F12, pinned so an FRR upgrade that changes it
  is noticed;
- this release's file (no description), no removal step: no such failure (the template's guard alone);
- this release's file with the removal step (files/shc_frr_leaving.py) first: it removes the network's instance and
  the egress VRF's interface neighbour, and the reload has no such failure.
Each case starts a fresh container. The interface and IS-IS stanzas are left out (no core devices). The upstream
image's own differences from RHEL's build (its VNC defaults in the default instance) make the reload's exit status
not 0 by themselves, so the cases judge the reload's errors per BGP instance, not its exit status."""
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from play_helper import ROOT  # noqa: E402

IMAGE = os.environ.get("SHC_FRR_IMAGE", "quay.io/frrouting/frr:8.5.3")
BASE = "f98cbae1"
TEMPLATE = "eda/rulebooks/roles/hybridfabric_provision/templates/frr-root.conf.j2"


def have_image():
    if not shutil.which("podman"):
        return False
    try:
        return subprocess.run(["podman", "image", "exists", IMAGE], capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


# in the container: start FRR (zebra, bgpd, staticd) on an empty file, load A, (the removal step), reload to B; prints
# the reload's errors of B's reload (frr-reload.log ERROR lines) and the removal step's output
SCRIPT = r"""#!/bin/bash
cd /w
for v in e10000:100 e10001:101 e10002:102 egress:300 service:200; do
  ip link add "${v%%:*}" type vrf table "${v##*:}" && ip link set "${v%%:*}" up
done
printf 'zebra=yes\nbgpd=yes\nstaticd=yes\nvtysh_enable=yes\n' > /etc/frr/daemons
printf 'frr defaults traditional\n' > /etc/frr/frr.conf
touch /etc/frr/vtysh.conf; chown -R frr:frr /etc/frr
/usr/lib/frr/frrinit.sh start >/dev/null 2>&1
for i in $(seq 30); do vtysh -c 'show bgp vrfs' >/dev/null 2>&1 && break; sleep 1; done
R="python3 /usr/lib/frr/frr-reload.py --bindir /usr/bin --confdir /etc/frr --rundir /var/run/frr"
$R --reload a.conf >/dev/null 2>&1
vtysh -c 'show running-config' | grep -E '^ neighbor x[eo][0-9]+ ' | sed 's/^/loaded:/'
# the unit's reload for a daemon (frrinit.sh reload: frr-reload.py of the file on disk) at its place before the step
if [ "$2" = unitfirst ]; then $R --reload a.conf >/dev/null 2>&1; fi
if [ "$1" = yes ]; then python3 helper.py --pattern 'e[0-9]+' --name service < b.conf; fi
: > /var/log/frr/frr-reload.log
$R --reload b.conf >/dev/null 2>&1
echo "rc=$?"
grep ' ERROR: ' /var/log/frr/frr-reload.log | sed 's/^.* ERROR: /error:/'
vtysh -c 'show running-config' | grep -E '^ neighbor x[eo][0-9]+ ' | sed 's/^/after:/'
"""

HELPER = """import sys
sys.path.insert(0, "/w")
import shc_frr_leaving as L
def run(cmd):
    return (0, "") if cmd[:2] == ["systemctl", "-q"] else L.run_cmd(cmd)
sys.exit(L.main(sys.argv[1:], sys.stdin.read(), run=run))
"""


def keep(text):
    """The rendered file without the stanzas that need the core's devices (interfaces, IS-IS, segment routing)."""
    out, skip = [], False
    for line in text.splitlines():
        if line == "exit" and skip:
            skip = False
            continue
        if not line.startswith((" ", "!")):
            skip = line.startswith(("interface ", "router isis", "segment-routing"))
        if not skip:
            out.append(line)
    return "\n".join(out) + "\n"


@unittest.skipUnless(have_image(), "podman or the FRR image %s is not present (never pulled)" % IMAGE)
class RealReload(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import test_frr_templates as T
        import test_service_vrf as S
        cls.T, cls.S = T, S
        p = subprocess.run(["git", "-C", ROOT, "show", "%s:%s" % (BASE, TEMPLATE)], capture_output=True, text=True)
        cls.old_t = p.stdout if p.returncode == 0 else None

    def files(self, template_text=None):
        S, T = self.S, self.T
        real = S.realization(S.CORE6, True)

        def render(nets):
            egp, att, vrfs, svc = S.pipeline(S.F, real, True, nets=nets)
            v = S.template_vars(real, egp, att, vrfs, svc, "bgw-1", True)
            return keep(T.render_text(template_text, v) if template_text else T.render(T.RT, v))
        return render(S.NETS), render([n for n in S.NETS if n["metadata"]["name"] != "core"])

    def run_case(self, a, b, helper, unit_first=False):
        d = tempfile.mkdtemp(prefix="shc-frr-real-")
        self.addCleanup(shutil.rmtree, d, True)
        for name, text in (("a.conf", a), ("b.conf", b), ("run.sh", SCRIPT), ("helper.py", HELPER)):
            with open(os.path.join(d, name), "w") as f:
                f.write(text)
        shutil.copy(os.path.join(ROOT, "eda/common/files/shc_frr_leaving.py"), d)
        os.chmod(d, 0o755)
        for f in os.listdir(d):
            os.chmod(os.path.join(d, f), 0o755)
        p = subprocess.run(["podman", "run", "--rm", "--network", "none", "--cap-add", "NET_ADMIN,SYS_ADMIN,NET_RAW",
                            "-v", "%s:/w:Z" % d, "--entrypoint", "/bin/bash", IMAGE, "/w/run.sh",
                            "yes" if helper else "no", "unitfirst" if unit_first else "-"],
                           capture_output=True, text=True, timeout=300)
        self.assertIn("rc=", p.stdout, p.stdout + p.stderr)
        lines = p.stdout.splitlines()
        return ([l[6:] for l in lines if l.startswith("error:")], [l for l in lines if l.startswith("loaded:")],
                [l for l in lines if l.startswith(("removed: ", "refused: "))], [l for l in lines if l.startswith("after:")])

    # the upstream image's own artefact (its BGP VNC defaults in the default instance); RHEL's build has no VNC
    IMAGE_ARTEFACTS = ('"router bgp 65010 --  no" we failed to remove this command', "% Unknown command:  no vnc defaults")

    def instance_errors(self, errors):
        """Every error but the image's artefact (Review L7: an error in any other context counts too)."""
        return [e for e in errors if e.strip() and e.strip() not in self.IMAGE_ARTEFACTS]

    def test_the_release_before_fails_on_the_interface_neighbour(self):
        if self.old_t is None:
            self.skipTest("%s not in this checkout" % BASE)
        a, b = self.files(self.old_t)
        errors, loaded, _, after = self.run_case(a, b, helper=False)
        self.assertIn("loaded: neighbor xo10000 description IPv6 egress of acme/core", loaded)
        self.assertEqual(self.instance_errors(errors), ['"router bgp 65011 vrf egress --  no" we failed to remove this command',
                                                        "% Specify remote-as or peer-group commands first"])
        self.assertEqual(after, [])  # the neighbour went with its first line: a failure in name only

    def test_without_the_description_the_reload_removes_it(self):
        a, b = self.files()
        errors, loaded, _, after = self.run_case(a, b, helper=False)
        self.assertIn("loaded: neighbor xo10000 interface v6only remote-as external", loaded)
        self.assertFalse([l for l in loaded if " description " in l])
        self.assertEqual(self.instance_errors(errors), [])
        self.assertEqual(after, [])

    def test_the_removal_step_first(self):
        a, b = self.files()
        errors, loaded, done, after = self.run_case(a, b, helper=True)
        self.assertEqual(done, ["removed: no router bgp 65010 vrf e10000",
                                "removed: router bgp 65011 vrf egress: no neighbor xo10000 interface"])
        self.assertEqual(self.instance_errors(errors), [])
        self.assertEqual(after, [])

    def test_the_unit_reload_before_the_removal_step_puts_nothing_back(self):
        # Review M1: the unit's reload for a newly enabled daemon now runs before the removal step; it loads the file on
        # disk (the previous run's, with the network): what the step then removes stays removed, and the job's reload
        # has no error
        a, b = self.files()
        errors, _, done, after = self.run_case(a, b, helper=True, unit_first=True)
        self.assertEqual(done, ["removed: no router bgp 65010 vrf e10000",
                                "removed: router bgp 65011 vrf egress: no neighbor xo10000 interface"])
        self.assertEqual(self.instance_errors(errors), [])
        self.assertEqual(after, [])

    def test_the_removal_step_on_a_node_the_release_before_configured(self):
        # the first run after the release: FRR still holds the descriptions; the step removes the neighbour whole
        if self.old_t is None:
            self.skipTest("%s not in this checkout" % BASE)
        a, _ = self.files(self.old_t)
        _, b = self.files()
        errors, _, done, after = self.run_case(a, b, helper=True)
        self.assertIn("removed: router bgp 65011 vrf egress: no neighbor xo10000 interface", done)
        self.assertEqual(self.instance_errors(errors), [])
        self.assertEqual(after, [])


if __name__ == "__main__":
    unittest.main()
