# -*- coding: utf-8 -*-
"""Read-only health reader of one border gateway node (fabric health job, eda/rulebooks/roles/fabric_health).

Run on the node as `timeout -k 5 <s> python3 - <base64 JSON input> <<'SHCHEALTH' ... SHCHEALTH` (the job inlines this
file; nothing is left on the node). It changes nothing: every command below only reads, and each has its own time
limit (CMD_TIMEOUT, or the input's cmdTimeout), inside the job's limit on the whole reader.

Input (JSON, base64 in argv[1]):
  instances      [{name: root|gw-<gateway>, kind: root|gateway, siteCidr?: <site underlay CIDR>, sitePeers?: [upstream
                 route reflectors]}] (gateways of sites
                 that are paused, being deleted, or not realized yet are not listed)
  transport      mpls|srv6 (the SRv6 core guard units exist only with srv6)
  windowSeconds  how far back a routing daemon crash, or an automatic restart of a routing unit, counts
  cmdTimeout     limit of each command (s)
  frrPendingHelper  optional: the source of files/shc_frr_pending.py (FRR running configuration vs file)
  frrIgnore      the realization's nodes.frrLoadCheck.ignore patterns
Output: one JSON line (see read_node).

Python 3.9 (the nodes' python3): standard library only. tests/unit/test_fabric_health_reader.py runs it with every
command replaced.
"""
import base64
import difflib
import hashlib
import json
import os
import re
import subprocess
import sys
import time

CMD_TIMEOUT = 15
ETC = "/etc/shc-fabric"
APPLY = "/usr/local/sbin/shc-fabric-apply"
FRR_DAEMONS = ("zebra", "bgpd", "isisd", "bfdd", "staticd", "mgmtd", "ospfd", "ospf6d", "pathd", "pimd")
UNIT_PROPS = "Id,LoadState,ActiveState,SubState,Result,NRestarts,UnitFileState,InvocationID"
START_LINE = re.compile(r"^(shc-fabric-apply|transport srv6|warning): ")
COREDUMP_ID = "fc2e22bc6ee647b6b90729ab34a250b1"
# systemd's "Scheduled restart job" record: a restart systemd decided itself (Restart=), never a start or a restart
# asked for (a job's systemctl restart); N_RESTARTS is the unit's counter
RESTART_ID = "5eb03494b6584870a536b337290809b3"
MAX_LINES = 10


def make_run(timeout):
    def run(cmd, t=None):
        """(returncode, stdout) of a read-only command; 124 when it ran out of time, 127 when it does not exist."""
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=t or timeout)
            return p.returncode, p.stdout
        except subprocess.TimeoutExpired:
            return 124, ""
        except OSError:
            return 127, ""
    return run


def read_file(path):
    try:
        with open(path) as f:
            return f.read()
    except OSError:
        return None


def digest(paths, read=read_file):
    """sha256 of the files' contents in order, as `cat a b | sha256sum` gives it (a missing file adds nothing)."""
    h = hashlib.sha256()
    for p in paths:
        h.update((read(p) or "").encode())
    return h.hexdigest()


def units_of(inst, transport, egress6):
    """The units an instance needs: (start unit, routing unit, others)."""
    if inst["kind"] == "root":
        others = (["shc-egress6-guard.timer"] if egress6 else []) + (["shc-core-guard@root.timer"] if transport == "srv6" else [])
        return "shc-fabric-apply@root.service", "frr.service", others
    unit = "shc-%s.service" % inst["name"].replace("gw-", "gw@", 1)
    return unit, unit, (["shc-core-guard@%s.timer" % inst["name"]] if transport == "srv6" else [])


def parse_show(text):
    """systemctl show of several units: {Id: {prop: value}}."""
    out, cur = {}, {}
    for line in (text or "").splitlines() + [""]:
        if not line.strip():
            if cur.get("Id"):
                out[cur["Id"]] = cur
            cur = {}
            continue
        k, _, v = line.partition("=")
        cur[k] = v
    return out


def unit_state(props):
    def num(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0
    return {"active": props.get("ActiveState", "unknown"), "sub": props.get("SubState", ""),
            "result": props.get("Result", ""), "restarts": num(props.get("NRestarts")),
            "enabled": props.get("UnitFileState", ""), "loaded": props.get("LoadState", "") == "loaded"}


def start_lines(run, unit, invocation):
    """The apply's own lines (refusals, failures, warnings) of the unit's current invocation in this boot."""
    if not invocation:
        return []
    _, out = run(["journalctl", "-b", "-o", "cat", "--no-pager", "-u", unit, "_SYSTEMD_INVOCATION_ID=" + invocation])
    return [l for l in out.splitlines() if START_LINE.match(l)][-5:]


def watchfrr(run, pathspace):
    """{daemon: state} from `show watchfrr` ({} when it can't be read)."""
    cmd = ["vtysh"] + (["-N", pathspace] if pathspace else []) + ["-c", "show watchfrr"]
    rc, out = run(cmd)
    if rc != 0:
        return None
    states = {}
    for line in out.splitlines():
        w = line.split()
        if len(w) >= 2 and w[0] in FRR_DAEMONS:
            states[w[0]] = w[1].rstrip(",:")
    return states


def coredumps(run, since):
    """Crashes of FRR daemons since `since` (epoch s): [{daemon, at, pathspace}] from systemd-coredump's journal entries."""
    rc, out = run(["journalctl", "--no-pager", "-o", "json", "--since=@%d" % int(since), "MESSAGE_ID=" + COREDUMP_ID,
                   "--output-fields=COREDUMP_EXE,COREDUMP_CMDLINE,COREDUMP_TIMESTAMP"])
    found = []
    for line in out.splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        exe = str(e.get("COREDUMP_EXE") or "")
        if "/frr/" not in exe:
            continue
        cmd = str(e.get("COREDUMP_CMDLINE") or "")
        m = re.search(r"(?:-N|--pathspace)[ =](\S+)", cmd)
        try:
            at = int(int(e.get("COREDUMP_TIMESTAMP") or e.get("__REALTIME_TIMESTAMP") or 0) / 1000000)
        except (TypeError, ValueError):
            at = 0
        found.append({"daemon": os.path.basename(exe), "at": at, "pathspace": m.group(1) if m else ""})
    return found


def restarts(run, since, units):
    """Automatic restarts of the routing units since `since` (epoch s): {unit: [{at, count}]} from systemd's own
    records (an F12-like reload failure, watchfrr ending: no core dump). One read for all units, with its own limit;
    one that fails or runs out of time finds none."""
    if not units:
        return {}
    rc, out = run(["journalctl", "--no-pager", "-o", "json", "--since=@%d" % int(since), "MESSAGE_ID=" + RESTART_ID]
                  + ["UNIT=" + u for u in units] + ["--output-fields=UNIT,N_RESTARTS"])
    found = {}
    for line in (out if rc == 0 else "").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        unit = str(e.get("UNIT") or "")
        if unit not in units:
            continue
        try:
            at = int(int(e.get("__REALTIME_TIMESTAMP") or 0) / 1000000)
        except (TypeError, ValueError):
            at = 0
        try:
            count = int(e.get("N_RESTARTS") or 0)
        except (TypeError, ValueError):
            count = 0
        found.setdefault(unit, []).append({"at": at, "count": count})
    return found


OPERSTATE = re.compile(r"^(\S+)\s+(UP|DOWN|UNKNOWN|LOWERLAYERDOWN|DORMANT|NOTPRESENT|TESTING)(\s|$)")
LINKLOCAL = re.compile(r"\s+fe80::\S+")


def comparable_state(text):
    """The apply's state text without what follows the links rather than the configuration: a device's operational
    state (a bridge is down until its first port comes up, which the gateways add after the root's boot apply) and
    the kernel's IPv6 link-local addresses."""
    out = []
    for line in (text or "").splitlines():
        if re.match(r"^state [A-Z-]+$", line.strip()):
            continue
        line = LINKLOCAL.sub("", OPERSTATE.sub(lambda m: m.group(1) + m.group(3), line))
        out.append(" ".join(line.split()))
    return out


def kernel_drift(run, name, read=read_file):
    """What changed in the state the apply manages since its last successful up: None when the apply left no record
    (an older release, or never applied since), else the changed lines (empty: no drift)."""
    rec = read("%s/%s.applied" % (ETC, name))
    if rec is None:
        return None
    rc, now = run([APPLY, name, "snapshot"], 20)
    if rc != 0:
        return ["the state could not be read (exit %d)" % rc]
    diff = [l for l in difflib.unified_diff(comparable_state(rec), comparable_state(now), lineterm="", n=0)
            if l[:1] in "+-" and not l.startswith(("+++", "---"))]
    return diff[:MAX_LINES]


def frr_files(inst):
    if inst["kind"] == "root":
        return ["/etc/frr/frr.conf", "/etc/frr/daemons"], "%s/root.frr-loaded" % ETC
    return ["/etc/frr/%s/frr.conf.shc" % inst["name"]], "%s/%s.frr-loaded" % (ETC, inst["name"])


def frr_pending(run, inst, helper=None, ignore=()):
    """FRR's running configuration against the desired file: the lines a reload would still change, as the fabric's
    post-reload verification computes them (files/shc_frr_pending.py, whose source the job passes in the input; it
    uses shc-frr-check on the node and reads its note without writing it). None: not checked."""
    if not helper:
        return None
    ns = {"__name__": "shc_frr_pending"}
    note = "%s/%s.frr-unapplied" % (ETC, inst["name"])
    try:
        exec(compile(helper, "shc_frr_pending.py", "exec"), ns)  # noqa: S102 - the platform's own helper
        lines = ns["frr_pending_difference"](run, "" if inst["kind"] == "root" else inst["name"], frr_files(inst)[0][0],
                                             note=note, ignore=ignore)
    except Exception:  # noqa: BLE001 - a helper that fails tells nothing; the record check still runs
        return None
    return None if lines is None else [str(l) for l in lines][:MAX_LINES]


def egress6(run, conf):
    """The IPv6 egress as the root's own configuration names it (same shape as the fabric job's node record)."""
    eg = (conf or {}).get("egress") or {}
    v6 = eg.get("ipv6") or {}
    if not eg.get("vrf") or not v6.get("address"):
        return None
    ev, addr = eg["vrf"], v6["address"]
    nets = [n["vrf"] for n in eg.get("networks") or [] if n.get("ipv6") and n.get("vrf")]
    errors = []
    ip = addr.split("/")[0].lower()
    _, out = run(["ip", "-j", "-6", "addr", "show", "dev", "egress0"])
    try:
        links = json.loads(out or "[]")
    except ValueError:
        links = []
    have = [a for l in links for a in l.get("addr_info", []) if str(a.get("local", "")).lower() == ip]
    if not have:
        errors.append("egress0 lacks %s" % addr)
    elif have[0].get("tentative"):
        errors.append("egress0: %s is still tentative" % addr)
    table = run(["nft", "list", "table", "inet", "shc_egress6"])[0] == 0
    if not table:
        errors.append("the filter table is not loaded")
    rc, out = run(["vtysh", "-c", "show bgp vrf %s ipv6 unicast summary json" % ev])
    try:
        peers = (json.loads(out or "{}") if rc == 0 else {}).get("peers", {})
    except ValueError:
        peers = {}
    sessions = [{"address": a, "asn": p.get("remoteAs"), "state": p.get("state", "Missing")} for a, p in sorted(peers.items())]

    def bgp_default(vrf):
        rc, out = run(["vtysh", "-c", "show ipv6 route vrf %s ::/0 json" % vrf])
        try:
            routes = (json.loads(out or "{}") if rc == 0 else {}).get("::/0") or []
        except ValueError:
            routes = []
        return any(r.get("protocol") == "bgp" for r in routes)
    default = bgp_default(ev)
    if default:
        errors += ["network VRF %s lacks the default route from the egress VRF" % v for v in nets if not bgp_default(v)]
    up = [s for s in sessions if s["state"] == "Established"]
    return {"address": addr, "upstream": sessions, "default": default, "table": table, "networks": len(nets),
            "healthy": bool(table and default and up and not errors), "errors": errors}


def service_sessions(run, conf):
    """The platform circuits' sessions in the service VRF as the root's own configuration names them: None without a
    service VRF on the node, else {vrf, established, known, peers: [{address, state}], links: [{network, vrf,
    address, state}]} (show bgp vrf <service> summary json; a configured CE missing from it counts as known and not
    established). links: the iBGP session of each network's service link, the service VRF's end (its neighbour is the
    network VRF's address on the link), from the same summary; a session missing from it is "Missing"."""
    sv = (conf or {}).get("service") or {}
    if (conf or {}).get("serviceMissing") and not (isinstance(sv, dict) and sv.get("vrf")):
        # the fabric keeps the service while a setting is wrong, and this node has no block to keep (Review R4 V2-M1)
        return {"vrf": "", "established": 0, "known": 1, "read": False, "peers": [], "links": [],
                "missing": str(conf["serviceMissing"])}
    if not isinstance(sv, dict) or not sv.get("vrf") or not sv.get("circuits"):
        return None
    want = [str(c.get("ce")) for c in sv["circuits"] if c.get("ce")]
    rc, out = run(["vtysh", "-c", "show bgp vrf %s summary json" % sv["vrf"]])
    try:
        doc = json.loads(out or "{}") if rc == 0 else {}
    except ValueError:
        doc = {}
    # FRR prints the summary per address family ({"ipv4Unicast": {peers}}), or the peers directly
    peers = {}
    for v in (doc.values() if isinstance(doc, dict) else []):
        if isinstance(v, dict) and isinstance(v.get("peers"), dict):
            peers.update(v["peers"])
    if isinstance(doc, dict) and isinstance(doc.get("peers"), dict):
        peers.update(doc["peers"])
    rows = [{"address": a, "state": (peers.get(a) or {}).get("state", "Missing")} for a in want]
    links = [{"network": str(w.get("network") or ""), "vrf": str(w.get("vrf") or ""), "address": str(w["seIp"]),
              "state": (peers.get(str(w["seIp"])) or {}).get("state", "Missing")}
             for w in sv.get("networks") or [] if isinstance(w, dict) and w.get("seIp")]
    return {"vrf": sv["vrf"], "established": sum(1 for r in rows if r["state"] == "Established"), "known": len(rows),
            "peers": rows, "links": links, "read": rc == 0}


def site_sessions(run, inst):
    import ipaddress
    rc, out = run(["vtysh", "-N", inst["name"], "-c", "show bgp l2vpn evpn summary json"])
    if rc != 0:
        return None
    try:
        peers = json.loads(out or "{}").get("peers", {})
        net = ipaddress.ip_network(inst["siteCidr"], strict=False)
        # the site's own routers (the listen range) and the upstream route reflectors that serve some sites
        rrs = {str(ipaddress.ip_address(a)) for a in inst.get("sitePeers") or []}
        site = [p for p in peers if ipaddress.ip_address(p) in net or str(ipaddress.ip_address(p)) in rrs]
    except (ValueError, KeyError, TypeError):
        return None
    return {"established": sum(1 for p in site if peers[p].get("state") == "Established"), "known": len(site)}


def read_node(inp, run, read=read_file, now=None):
    now = int(now if now is not None else time.time())
    transport = inp.get("transport") or "mpls"
    root_conf = None
    raw = read("%s/root.json" % ETC)
    if raw:
        try:
            root_conf = json.loads(raw)
        except ValueError:
            root_conf = None
    has6 = bool(((root_conf or {}).get("egress") or {}).get("ipv6"))
    insts = inp.get("instances") or []
    plan = {i["name"]: units_of(i, transport, has6) for i in insts}
    names = sorted({u for s, r, o in plan.values() for u in [s, r] + o})
    _, out = run(["systemctl", "show", "-p", UNIT_PROPS, "--"] + names)
    shown = parse_show(out)
    _, out = run(["systemctl", "list-units", "--failed", "--plain", "--no-legend", "shc-*", "frr*"])
    failed = sorted({l.split()[0] for l in out.splitlines() if l.split()})
    try:
        booted = now - int(float((read("/proc/uptime") or "").split()[0]))
    except (ValueError, IndexError):
        booted = 0
    since = max(now - int(inp.get("windowSeconds") or 3600), booted)
    crashes = coredumps(run, since)
    restarted = restarts(run, since, sorted({r for s, r, o in plan.values()}))
    result = {"bootId": (read("/proc/sys/kernel/random/boot_id") or "").strip(), "bootedAt": booted, "now": now,
              "failedUnits": failed, "instances": {}}
    for i in insts:
        su, ru, others = plan[i["name"]]
        units = {u: unit_state(shown.get(u, {})) for u in [su, ru] + others}
        start = dict(units[su], unit=su)
        if start["active"] != "active":
            start["lines"] = start_lines(run, su, shown.get(su, {}).get("InvocationID", ""))
        else:
            start["lines"] = []
        # what the last successful apply left out (its warnings), whoever ran it: the boot unit or a job's direct apply
        start["incomplete"] = [l.strip() for l in (read("%s/%s.incomplete" % (ETC, i["name"])) or "").splitlines()
                               if l.strip()][:5]
        pathspace = "" if i["kind"] == "root" else i["name"]
        files, record = frr_files(i)
        loaded = (read(record) or "").strip()
        d = {"start": start, "units": units,
             "routing": {"unit": ru, "daemons": watchfrr(run, pathspace) if units[ru]["active"] == "active" else None,
                         "crashes": [c for c in crashes if c["pathspace"] == pathspace],
                         "restarts": restarted.get(ru, [])},
             "kernelDrift": kernel_drift(run, i["name"], read),
             "frr": {"reloadDue": bool(loaded) and digest(files, read) != loaded, "recorded": bool(loaded),
                     "pending": frr_pending(run, i, inp.get("frrPendingHelper"), inp.get("frrIgnore") or ()),
                     # the post-reload verification's note: lines FRR did not take after two loads (kept, reported)
                     "notTaken": [l.strip() for l in (read("%s/%s.frr-unapplied" % (ETC, i["name"])) or "").splitlines()
                                  if l.strip()][:MAX_LINES]}}
        if i["kind"] == "root":
            d["egress6"] = egress6(run, root_conf)
            d["serviceSessions"] = service_sessions(run, root_conf)
        elif i.get("siteCidr"):
            d["siteSessions"] = site_sessions(run, i)
        result["instances"][i["name"]] = d
    return result


def main(argv):
    inp = json.loads(base64.b64decode(argv[1]).decode()) if len(argv) > 1 else {}
    print(json.dumps(read_node(inp, make_run(int(inp.get("cmdTimeout") or CMD_TIMEOUT))), sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
