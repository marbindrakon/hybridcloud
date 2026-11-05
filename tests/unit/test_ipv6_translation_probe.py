"""The IPv6 translation probe (tests/connectivity/ipv6-translation-probe), a test tool: its user data parses, its check
script passes a shell syntax check and writes the metrics its README tells a tester to read, ignore-auto-dns keeps the
guest on the network data's resolvers (NetworkManager and systemd-networkd, stubbed), the VM and the tenant objects
have the documented shape, and the placeholders are the documented set. Documentation prefixes only."""
import os
import re
import subprocess
import sys
import unittest

import yaml

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from play_helper import ROOT  # noqa: E402

PROBE = os.path.join(ROOT, "tests", "connectivity", "ipv6-translation-probe")
CHECKS = ["address", "resolver", "pref64", "name4", "literal4", "name6", "mtu", "deny"]


class Probe(unittest.TestCase):
    PLACEHOLDER = re.compile(r"__[A-Z0-9]+(?:_[A-Z0-9]+)*__")

    def text(self, name):
        with open(os.path.join(PROBE, name)) as f:
            return f.read()

    def user_data(self):
        text = self.text("probe-user-data.yaml")
        self.assertTrue(text.startswith("#cloud-config\n"))
        return yaml.safe_load(text)

    def test_user_data(self):
        ud = self.user_data()
        self.assertEqual(sorted(ud["packages"]), sorted(["clatd", "python3", "bind-utils", "iputils", "curl", "qemu-guest-agent"]))
        files = {f["path"]: f["content"] for f in ud["write_files"]}
        self.assertEqual(files["/etc/clatd.conf"], "plat-prefix=__PREF64__\n")
        self.assertIn("ExecStart=/usr/bin/python3 -m http.server __PORT__ --bind :: --directory /var/lib/shc-ipv6-probe",
                      files["/etc/systemd/system/shc-ipv6-probe-http.service"])
        self.assertIn("OnUnitActiveSec=60s", files["/etc/systemd/system/shc-ipv6-probe.timer"])
        script = files["/usr/local/bin/shc-ipv6-probe"]
        subprocess.run(["bash", "-n"], input=script, text=True, check=True)
        self.assertNotIn("set -u", script)
        names = re.findall(r"^\s*result (\w+) check_\1$", script, re.M)
        self.assertEqual(names, ["address", "resolver", "pref64", "name4", "literal4", "name6", "mtu", "deny"])
        self.assertIn("ping -6 -M do -c 2 -W 3 -s 1352", script)
        self.assertIn('mv -f "$tmp" "$DIR/metrics"', script)

    def test_check_script_output_parses_as_the_readme_reads_it(self):
        """The script, with its network commands replaced by stubs, writes the metric lines the README tells a tester to
        read: one check line per check, the run's time and the resolver in use."""
        page = self.run_check_script("2001:db8::54")
        checks = dict(re.findall(r'^shc_ipv6_probe_check\{check="(\w+)"\} ([01])$', page, re.M))
        self.assertEqual(checks, {k: "1" for k in CHECKS})
        self.assertRegex(page, r"(?m)^shc_ipv6_probe_run_timestamp_seconds \d+$")
        # the resolver in use: an information metric, not a check
        self.assertIn('shc_ipv6_probe_resolver_info{server="2001:db8::54",translation="1"} 1', page.splitlines())

    def test_the_readme_reads_what_the_probe_writes(self):
        """The README's checks table is the script's checks, and its reading procedure names the metrics, the file and
        the gateway read the probe serves."""
        readme = self.text("README.md")
        table = readme[readme.index("## The checks"):readme.index("## Reading the result")]
        self.assertEqual(re.findall(r"^\| `(\w+)` \|", table, re.M), CHECKS)
        for needle in ("shc_ipv6_probe_check", "shc_ipv6_probe_run_timestamp_seconds", "shc_ipv6_probe_resolver_info",
                       "cat /var/lib/shc-ipv6-probe/metrics", "ip netns exec gw-<gateway> ip vrf exec <vrf>",
                       "--interface <source>", "at most 180 seconds"):
            self.assertIn(needle, readme)

    def test_the_resolver_in_use_after_the_guest_left_the_translation_resolvers(self):
        page = self.run_check_script("2001:db8:ffff::3331%2")
        self.assertIn('shc_ipv6_probe_resolver_info{server="2001:db8:ffff::3331",translation="0"} 1', page.splitlines())

    def run_check_script(self, current_resolver):
        """The check script with ip, dig, curl, ping and resolvectl stubbed; returns the metrics page it wrote."""
        files = {f["path"]: f["content"] for f in self.user_data()["write_files"]}
        script = files["/usr/local/bin/shc-ipv6-probe"].replace("DIR=/var/lib/shc-ipv6-probe", 'DIR="$PROBE_DIR"')
        import tempfile
        tmp = tempfile.mkdtemp(prefix="shc-probe-")
        try:
            bindir = os.path.join(tmp, "bin")
            os.makedirs(bindir)
            stubs = {"ip": "echo '2: eth0    inet6 2001:db8:4005:4200::10/64 scope global dynamic'",
                     "dig": 'case "$*" in *AAAA*) echo 2001:db8:66::c000:aa;; *) echo 192.0.0.170;; esac',
                     "curl": 'case "$*" in *2001:db8:66::c633:6407*) exit 7;; *-w*) echo 200;; esac; exit 0',
                     "ping": "exit 0",
                     "resolvectl": "printf 'Link 2 (eth0)\\nCurrent DNS Server: %s\\n' '" + current_resolver + "'"}
            for name, body in stubs.items():
                with open(os.path.join(bindir, name), "w") as f:
                    f.write("#!/bin/bash\n" + body + "\n")
                os.chmod(os.path.join(bindir, name), 0o755)
            script = (script.replace("__PREF64__", "2001:db8:66::/96").replace("__RESOLVERS__", "2001:db8::53, 2001:db8::54")
                      .replace("__DENIED_IPV4__", "198.51.100.7"))
            path = os.path.join(tmp, "probe")
            with open(path, "w") as f:
                f.write(script)
            env = dict(os.environ, PATH=bindir + ":" + os.environ["PATH"], PROBE_DIR=os.path.join(tmp, "out"))
            subprocess.run(["bash", path], env=env, check=True, timeout=60)
            with open(os.path.join(tmp, "out", "metrics")) as f:
                page = f.read()
        finally:
            import shutil
            shutil.rmtree(tmp, ignore_errors=True)
        return page

    NM = "\n".join([
        'case "$*" in',
        '  "-g RUNNING general") echo running ;;',
        '  "-g GENERAL.CON-UUID device show enp1s0") echo 5f2b0c9e-0000-4000-8000-000000000001 ;;',
        '  "-g ipv4.dns,ipv6.dns connection show 5f2b0c9e-0000-4000-8000-000000000001") printf "\\n%s\\n" "$DNS" ;;',
        '  "-g ipv"[46]".ignore-auto-dns connection show 5f2b0c9e-0000-4000-8000-000000000001") echo "$IGNORED" ;;',
        "esac"])

    def run_ignore_auto_dns(self, nmcli, networkctl="exit 1", network_file=None, existing=None, rc=0):
        """The user data's ignore-auto-dns script with ip, sleep, nmcli and networkctl stubbed; returns its output, the
        stubs' calls and the drop-ins it wrote under a stand-in for /etc/systemd/network."""
        script = {f["path"]: f["content"] for f in self.user_data()["write_files"]}["/usr/local/sbin/ignore-auto-dns"]
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp(prefix="shc-dns-")
        try:
            bindir, etc, log = os.path.join(tmp, "bin"), os.path.join(tmp, "etc-network"), os.path.join(tmp, "calls")
            netfile = os.path.join(tmp, "10-netplan-enp1s0.network")
            os.makedirs(bindir)
            stubs = {"ip": "echo 'default via fe80::1 dev enp1s0 proto ra metric 100 expires 1798sec pref medium'",
                     "sleep": "exit 0", "nmcli": nmcli, "networkctl": networkctl,
                     "resolvectl": "echo 'Link 2 (enp1s0): 2001:db8::53'"}
            for name, body in stubs.items():
                with open(os.path.join(bindir, name), "w") as f:
                    f.write('#!/bin/bash\necho "%s $*" >> "$CALLS"\n%s\n' % (name, body))
                os.chmod(os.path.join(bindir, name), 0o755)
            if network_file is not None:
                with open(netfile, "w") as f:
                    f.write(network_file)
            if existing is not None:   # a drop-in an earlier boot wrote
                os.makedirs(os.path.join(etc, "10-netplan-enp1s0.network.d"))
                with open(os.path.join(etc, "10-netplan-enp1s0.network.d", "90-ignore-auto-dns.conf"), "w") as f:
                    f.write(existing)
            path = os.path.join(tmp, "ignore-auto-dns")
            with open(path, "w") as f:
                f.write(script.replace("/etc/systemd/network", etc))
            env = dict(os.environ, PATH=bindir + ":" + os.environ["PATH"], CALLS=log, NETFILE=netfile)
            done = subprocess.run(["bash", path], env=env, timeout=60, capture_output=True, text=True)
            self.assertEqual(done.returncode, rc, done.stderr)
            out = done.stdout
            calls = []
            if os.path.exists(log):
                with open(log) as f:
                    calls = f.read().splitlines()
            drops = {}
            for root, _, names in os.walk(etc):
                for n in names:
                    with open(os.path.join(root, n)) as f:
                        drops[os.path.relpath(os.path.join(root, n), etc)] = f.read()
            return out, calls, drops
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_ignore_auto_dns_networkmanager(self):
        """NetworkManager: the connection on the IPv6 default route's device ignores the resolvers DHCP hands out (both
        families: the probe's placement is IPv6-only) and is re-activated; one without resolvers of its own is left.
        The link's resolvers are logged last (the unit writes to the console)."""
        out, calls, drops = self.run_ignore_auto_dns("DNS='2001:db8::53,2001:db8::54'; IGNORED=no\n" + self.NM)
        uuid = "5f2b0c9e-0000-4000-8000-000000000001"
        self.assertIn("nmcli connection modify %s ipv6.ignore-auto-dns yes ipv4.ignore-auto-dns yes" % uuid, calls)
        # a full reactivation: NetworkManager before 1.58 keeps a running DHCPv6 lease's DNS on "device reapply"
        # (seen on a probe: the unit succeeded and the cluster's resolver stayed on the link)
        self.assertEqual(calls[-2:], ["nmcli connection up %s" % uuid, "resolvectl dns enp1s0"])
        self.assertFalse([c for c in calls if "reapply" in c])
        self.assertIn("enp1s0: ignores the resolvers handed out from now on", out)
        self.assertIn("Link 2 (enp1s0): 2001:db8::53", out)
        self.assertEqual(drops, {})
        # the reactivation fails: the unit fails, so the console shows it
        _, calls, _ = self.run_ignore_auto_dns(
            "DNS='2001:db8::53'; IGNORED=no\n" + self.NM.replace("esac", '  "connection up "*) exit 4 ;;\nesac'), rc=1)
        self.assertNotIn("resolvectl dns enp1s0", calls)
        # the next boot: already set, so the connection is not re-applied
        out, calls, _ = self.run_ignore_auto_dns("DNS='2001:db8::53'; IGNORED=yes\n" + self.NM)
        self.assertFalse([c for c in calls if " modify " in c or "reapply" in c])
        self.assertIn("enp1s0: already ignores the resolvers handed out", out)
        out, calls, _ = self.run_ignore_auto_dns("DNS=''; IGNORED=no\n" + self.NM)
        self.assertFalse([c for c in calls if " modify " in c or "reapply" in c])
        self.assertIn("enp1s0: no resolvers configured: nothing changed", out)

    def test_ignore_auto_dns_networkd(self):
        """systemd-networkd (NetworkManager not running): a drop-in for the device's .network file turns the DHCP and
        router advertisement resolvers off, then networkd reloads and reconfigures the device."""
        networkctl = 'case "$1" in status) echo "       Network File: $NETFILE" ;; esac'
        _, calls, drops = self.run_ignore_auto_dns("exit 0", networkctl, "[Network]\nDNS=2001:db8::53\n")
        want = "[DHCPv6]\nUseDNS=false\n\n[IPv6AcceptRA]\nUseDNS=false\n\n[DHCPv4]\nUseDNS=false\n"
        self.assertEqual(drops, {"10-netplan-enp1s0.network.d/90-ignore-auto-dns.conf": want})
        self.assertEqual(calls[-3:], ["networkctl reload", "networkctl reconfigure enp1s0", "resolvectl dns enp1s0"])
        # the next boot: the drop-in is in place, networkd is not reloaded
        out, calls, _ = self.run_ignore_auto_dns("exit 0", networkctl, "[Network]\nDNS=2001:db8::53\n", existing=want)
        self.assertFalse([c for c in calls if "reload" in c or "reconfigure" in c])
        self.assertIn("enp1s0: already ignores the resolvers handed out", out)
        # a .network file without resolvers of its own: nothing written
        _, _, drops = self.run_ignore_auto_dns("exit 0", networkctl, "[Network]\nDHCP=ipv6\n")
        self.assertEqual(drops, {})

    def test_ignore_auto_dns_unit(self):
        ud = self.user_data()
        files = {f["path"]: f["content"] for f in ud["write_files"]}
        unit = files["/etc/systemd/system/ignore-auto-dns.service"]
        for line in ("ExecStart=/usr/local/sbin/ignore-auto-dns", "After=network-online.target", "WantedBy=multi-user.target",
                     "StandardOutput=journal+console"):
            self.assertIn(line, unit.splitlines())
        subprocess.run(["bash", "-n"], input=files["/usr/local/sbin/ignore-auto-dns"], text=True, check=True)
        # enabled (and run) right after the units are loaded, before clatd and the checks start
        self.assertEqual(ud["runcmd"][:2], [["systemctl", "daemon-reload"], ["systemctl", "enable", "--now", "ignore-auto-dns.service"]])

    def test_vm(self):
        vm = yaml.safe_load(self.text("vm-cloudvirt.yaml"))
        spec = vm["spec"]["template"]["spec"]
        self.assertEqual(spec["domain"]["devices"]["interfaces"][0]["binding"], {"name": "l2bridge"})
        ci = next(v for v in spec["volumes"] if v["name"] == "cloudinit")["cloudInitNoCloud"]
        self.assertEqual(ci["secretRef"], {"name": "__WORKLOAD__-user-data"})
        nd = yaml.safe_load(ci["networkData"])
        eth = nd["ethernets"]["primary"]
        self.assertEqual((nd["version"], eth["dhcp4"], eth["dhcp6"], eth["accept-ra"]), (2, False, True, True))
        self.assertEqual(eth["match"], {"macaddress": "__MAC__"})
        self.assertEqual(eth["nameservers"]["addresses"], ["__RESOLVERS__"])

    def test_objects(self):
        docs = [d for d in yaml.safe_load_all(self.text("probe-objects.yaml")) if d]
        net = next(d for d in docs if d["kind"] == "HybridNetwork")
        self.assertEqual(net["spec"]["platformPrefixes"], [{"family": 6}])
        self.assertNotIn("prefixes", net["spec"])
        self.assertTrue(net["spec"]["egress"]["internet"])
        for p in (d for d in docs if d["kind"] == "NetworkPlacement"):
            self.assertNotIn("ipv4", p["spec"])
            self.assertEqual(p["spec"]["ipv6"], {"prefixLength": 64})

    def test_placeholders_are_documented(self):
        readme = self.text("README.md")
        table = readme[readme.index("## Placeholders"):readme.index("## The checks")]
        documented = set(self.PLACEHOLDER.findall(table))
        used = set()
        for name in ("probe-user-data.yaml", "vm-cloudvirt.yaml", "server-cloudoso.md"):
            used |= set(self.PLACEHOLDER.findall(self.text(name)))
        self.assertEqual(used, documented)
        self.assertEqual(documented, {"__PREF64__", "__RESOLVERS__", "__IPV4_ONLY_NAME__", "__IPV4_LITERAL__", "__IPV6_NAME__",
                                      "__IPV6_PING_TARGET__", "__DENIED_IPV4__", "__PORT__", "__WORKLOAD__", "__NAMESPACE__",
                                      "__MAC__", "__PLACEMENT_NETWORK__", "__IMAGE__", "__FLAVOR__", "__SECURITY_GROUP__",
                                      "__SCRAPE_SOURCE__"})


if __name__ == "__main__":
    unittest.main()
