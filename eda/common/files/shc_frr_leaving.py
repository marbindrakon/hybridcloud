# -*- coding: utf-8 -*-
"""What a fabric run takes out of the root's FRR itself, before the apply removes the kernel objects (node_root.yml).

frr-reload.py (FRR 8.5) cannot delete a BGP neighbour named by an interface ("neighbor xo<t> interface ...") that has
a second line at router level: it sends the defining line's "no" first (bgpd deletes the peer and all its lines),
then the second line's "no", which bgpd refuses for a name it no longer knows, and the reload exits 1. And bgpd drops
a neighbour's BFD session only while the neighbour's VRF still exists in the kernel. So the run removes, in one step
before the apply deletes any device or VRF:
  1. the BGP instance of each root VRF the desired file no longer has ("no router bgp <asn> vrf <name>"), for the
     VRFs the caller names (a pattern for the networks' VRFs, the service VRF by name);
  2. inside each BGP instance that stays, every neighbour named by an interface that the desired file no longer has
     in that instance ("no neighbor <name> interface"), unless the caller turns this off.
Nothing else: static routes, address neighbours, prefix-lists and route-maps the reload removes without an error
whether their device exists or not.

What FRR runs is compared with the desired file itself (the rendered template), never with an older record: after a
run that stopped between writing its records and the reload, only this comparison still sees what FRR holds. A
command is never returned for something the desired file has (a neighbour removed here that the reload adds back is
a session reset).

As a script (the fabric job, on the node): `python3 -c <this source> --pattern REGEX [--name VRF]... [--no-neighbours]` with the
desired file on stdin. Reads the running configuration (vtysh), sends each removal in its own vtysh call, prints
"removed: <context> <command>" per command FRR took and "refused: ..." per command it did not, and exits 0: a refusal
is left to the reload (and reported there). Nothing is done while FRR is not active or its configuration cannot be
read ("unreadable: ..."). Python 3.9, standard library only.
"""
import argparse
import re
import subprocess
import sys

INSTANCE = re.compile(r"^router bgp (\d+(?:\.\d+)?)(?: vrf (\S+))?\s*$")
IF_NEIGHBOUR = re.compile(r"^ neighbor (\S+) interface(?:\s|$)")
TIMEOUT = 30


def instances(text):
    """{vrf name ('' for the default instance): {"asn": asn, "ifs": [interface neighbour names in order]}} of an FRR
    configuration text (the running configuration or the file): every "router bgp" stanza at column 0 up to its
    "exit" (or the next line at column 0); the neighbours named by interface are its lines " neighbor <name>
    interface ..." (router level, one space)."""
    out, cur = {}, None
    for line in (text or "").splitlines():
        line = line.rstrip()
        if not line:
            continue
        if not line.startswith((" ", "\t")):
            m = INSTANCE.match(line)
            if m:
                cur = out.setdefault(m.group(2) or "", {"asn": m.group(1), "ifs": []})
            else:
                cur = None
            continue
        if cur is not None:
            n = IF_NEIGHBOUR.match(line)
            if n and n.group(1) not in cur["ifs"]:
                cur["ifs"].append(n.group(1))
    return out


def leaving(running, desired, pattern, neighbours=True, names=()):
    """The removals, in order: [(context, command)], context "" for a command at the configuration level, else the
    instance line to enter first ("router bgp <asn> vrf <name>"). First the instances of VRFs matching `pattern`
    (a regular expression, matched whole) or named in `names` that the desired file lacks, then (with neighbours) the
    interface neighbours the desired file lacks in an instance it keeps. The default instance is never removed."""
    run, want = instances(running), instances(desired)
    rx = re.compile(pattern) if pattern else None
    out = []
    for vrf, inst in run.items():
        if vrf and vrf not in want and ((rx is not None and rx.fullmatch(vrf)) or vrf in names):
            out.append(("", "no router bgp %s vrf %s" % (inst["asn"], vrf)))
    if neighbours:
        for vrf, inst in run.items():
            if vrf not in want:
                # an instance the file lacks: removed whole above, or (outside the pattern) left to the reload
                continue
            keep = set(want[vrf]["ifs"])
            ctx = "router bgp %s%s" % (inst["asn"], (" vrf " + vrf) if vrf else "")
            for name in inst["ifs"]:
                if name not in keep:
                    out.append((ctx, "no neighbor %s interface" % name))
    return out


def run_cmd(cmd, timeout=TIMEOUT):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, p.stdout + p.stderr
    except subprocess.TimeoutExpired:
        return 124, "no answer in %d s" % timeout
    except OSError as e:
        return 127, str(e)


def main(argv, desired, run=run_cmd, out=sys.stdout):
    ap = argparse.ArgumentParser()
    ap.add_argument("--pattern", default="")
    ap.add_argument("--name", action="append", default=[])
    ap.add_argument("--no-neighbours", action="store_true")
    a = ap.parse_args(argv)
    if run(["systemctl", "-q", "is-active", "frr"])[0] != 0:
        print("frr is not active: nothing to take out", file=out)
        return 0
    rc, text = run(["vtysh", "-c", "show running-config"])
    if rc != 0 or not text.strip():
        print("unreadable: the running configuration (exit %d); left to the reload" % rc, file=out)
        return 0
    for ctx, cmd in leaving(text, desired, a.pattern, not a.no_neighbours, a.name):
        args = ["vtysh", "-c", "configure terminal"] + (["-c", ctx] if ctx else []) + ["-c", cmd]
        rc, said = run(args)
        what = ((ctx + ": ") if ctx else "") + cmd
        if rc == 0 and "%" not in said:
            print("removed: " + what, file=out)
        else:
            print("refused: %s (%s)" % (what, " ".join(said.split()) or "exit %d" % rc), file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], sys.stdin.read()))
