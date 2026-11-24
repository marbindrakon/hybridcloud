"""The CRDs keep every status key the jobs and the operator write.

The API server drops (prunes) a key its schema does not allow: an object that lists `properties` keeps only those,
also below a `status` that preserves unknown fields. A job that writes a key the schema drops and reads it back on
its next run sees nothing there (a cluster's floating IPs were found again by a dropped description, and a second
pair was made). This test walks every status write (tests/unit/status_write_walk.py says how):
- the jobs': the includes of eda/common/tasks/patch_cr_status.yml and the direct calls of the status module
  `hybridsovereign.shc.cr_status`, under eda/rulebooks/roles/ and eda/common/tasks/;
- the operator's: every `operator_sdk.util.k8s_status` under operator/;
also bodies built by Jinja statement templates (`{% set %}`, `{% for %}`, `.append()`), and every key the status
library adds itself, against each kind's CRD schema, and fails for a key the API server would drop.

Where a written value is one the walk cannot see into (a helper's answer, a registered result, a filter it does not
know) and the schema lists the keys it keeps there, the test fails unless the place is in BLIND with the reason a
person checked: a new such place needs that decision.

Not walked: status written by Python (the install observer's module, the helpers that patch status themselves) and
by a generic `k8s` or `uri` task on the status subresource.
"""
import copy
import os
import shutil
import sys
import tempfile
import unittest

import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
BASES = os.path.join(ROOT, "operator", "config", "crd", "bases")
LIB = os.path.join(ROOT, "eda", "rulebooks", "collections", "ansible_collections", "hybridsovereign", "shc",
                   "plugins", "module_utils")
sys.path.insert(0, os.path.dirname(__file__))
sys.path.insert(0, LIB)
import status_write_walk as W  # noqa: E402
import shc_status as S  # noqa: E402

# Keys the walk lists although no run writes them (the walk is flow insensitive), each with the reason.
NOT_WRITTEN = {
    # ipv6_status.yml builds {path: Provider, pending: true} for the site side and writes status.ipv6 without a
    # translation while `pending` is set (combine({} if _translation.pending ...)).
    ("networkplacements", "ipv6.translation.pending"),
}


# Places where a written value is opaque to the walk and the schema lists the keys it keeps: (plural or "*", path):
# the reason the keys there are the schema's. A place listed here that the walk no longer finds fails the test.
BLIND = {
    ("*", "conditions"): "the extra conditions a job hands to fabric_finish.yml (fab_conditions) are condition "
                         "records {type, status, reason, message, lastTransitionTime} built by the job or a helper",
    ("*", "conditions.[]"): "as conditions",
    ("aaporgs", "signInMaps"): "a run without new maps writes back the record read from the object's own status "
                               "(aaporg_recorded.signInMaps): only the keys the server kept",
    ("cloudinfrastructures", "site.signIn"): "a kept sign-in re-writes the previous status.site.signIn read back",
    ("cloudinfrastructures", "site.signIn.account"): "the roles the site probe read: shc_openstack.py writes "
                                                     "{role, scope} per role",
    ("cloudosos", "access"): "the teardown's oso_access is the object's own status.access read back",
    ("hybridfabrics", "reservedRanges"): "the ranges the fabric check helper (shc_fabric.py) answers",
    ("hybridnetworks", "addressSpace"): "the address space the IPAM helper (shc_ipam.py) answers",
    ("hybridnetworks", "platformAccess.prefixes.[]"): "the prefix rows the platform-access helper answers, kept as "
                                                      "they are or combined with state/reason/message",
    ("platformopenshifts", "install"): "built by the status library's filters shc_install_* (their own tests)",
}


def crd_status_schemas():
    """{plural: status schema}, {singular: plural}, {kind: plural}."""
    schemas, plurals, kinds = {}, {}, {}
    for f in sorted(os.listdir(BASES)):
        with open(os.path.join(BASES, f)) as fh:
            d = yaml.safe_load(fh)
        names = d["spec"]["names"]
        root = d["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
        schemas[names["plural"]] = root.get("properties", {}).get("status", {})
        plurals[names["singular"]] = names["plural"]
        kinds[names["kind"]] = names["plural"]
    return schemas, plurals, kinds


SCHEMAS, PLURALS, KIND_PLURALS = crd_status_schemas()
INDEX = W.Index(ROOT)
JOBS = W.walk(INDEX, PLURALS)
WRITTEN = JOBS.written
OP_INDEX = W.Index(ROOT, side="operator")
ROLE_KINDS = W.operator_role_kinds(ROOT)
OPERATOR = W.walk(OP_INDEX, PLURALS, KIND_PLURALS, ROLE_KINDS)


def blind(res):
    """{(plural, dotted path): sites} where a written value is opaque and the schema lists the keys it keeps."""
    out = {}
    for (plural, path), sites in sorted(res.opaque.items()):
        schema = SCHEMAS.get(plural)
        if schema is None or not W.kept(schema, path)[0] or not W.lists_children(schema, path):
            continue
        out[(plural, ".".join(path))] = sorted(sites)
    return out


def _allowed(plural, path):
    return (plural, path) in BLIND or ("*", path) in BLIND


def dropped(written):
    """[(plural, dotted path, sites)] of the shortest written paths the schema drops."""
    out = []
    for (plural, path), sites in sorted(written.items()):
        schema = SCHEMAS.get(plural)
        if schema is None:
            continue
        ok, at = W.kept(schema, path)
        if not ok and at == len(path) - 1:
            out.append((plural, ".".join(path), sorted(sites)))
    return out


class PruningModel(unittest.TestCase):
    """The pruning rule the walk applies, on small schemas (the Kubernetes rule for structural schemas)."""

    S = {"type": "object", "x-kubernetes-preserve-unknown-fields": True, "properties": {
        "fips": {"type": "object", "properties": {"api": {"type": "object", "properties": {"id": {"type": "string"}}}}},
        "free": {"type": "object", "x-kubernetes-preserve-unknown-fields": True},
        "rows": {"type": "array", "items": {"type": "object", "properties": {"a": {"type": "string"}}}},
        "map": {"type": "object", "additionalProperties": {"type": "object", "properties": {"b": {"type": "string"}}}},
    }}

    def test_cases(self):
        for path, want in [
            (("other",), True),                       # status preserves unknown fields
            (("other", "deep"), True),
            (("fips", "api", "id"), True),
            (("fips", "api", "description"), False),  # listed properties drop the rest, below a preserving status
            (("fips", "network"), False),
            (("free", "x", "y"), True),
            (("rows", W.LIST, "a"), True),
            (("rows", W.LIST, "z"), False),
            (("map", "any", "b"), True),
            (("map", "any", "c"), False),
        ]:
            with self.subTest(path=path):
                self.assertEqual(W.kept(self.S, path)[0], want)


class TheWalkSeesTheWrites(unittest.TestCase):
    """The walk reaches the writes it must judge (a walk that found nothing would pass everything)."""

    def test_counts(self):
        self.assertGreater(len(INDEX.writes), 100)
        self.assertGreater(len({p for p, _ in WRITTEN}), 20)

    def test_every_write_reaches_a_kind(self):
        for idx, res in ((INDEX, JOBS), (OP_INDEX, OPERATOR)):
            reached = {s.split(" <- ")[0] for sites in list(res.written.values()) + list(res.opaque.values())
                       for s in sites}
            with self.subTest(side=idx.side):
                self.assertEqual({w.path for w in idx.writes} - reached, set(), "status writes that reach no kind")

    def test_the_three_ways_of_writing_are_walked(self):
        hows = {w.how for w in INDEX.writes}
        self.assertEqual(hows, {"library", "module"})
        self.assertGreater(len([w for w in INDEX.writes if w.how == "module"]), 3)
        self.assertGreater(len(OP_INDEX.writes), 150)
        # the operator's shared role writes reach the kinds of the roles that include it, not every kind
        common = {p for (p, _path), sites in OPERATOR.written.items()
                  for s in sites if s.startswith("operator/roles/_common/tasks/publish_operator_event.yml <- ")}
        self.assertIn("platformopenshifts", common)
        self.assertNotIn("tenantprofiles", common)

    def test_known_keys(self):
        for plural, path in [
            ("platformopenshifts", ("floatingIPs", "api", "id")),
            ("platformopenshifts", ("floatingIPs", "ingress", "id")),
            ("platformopenshifts", ("buildName",)),
            ("cloudosos", ("projectId",)),
            ("aaporgs", ("gatewayOrgId",)),
            ("aaporgs", ("signInMaps",)),
            ("networkplacements", ("siteNetwork", "name")),
            ("cloudinfrastructures", ("site", "signIn", "idpName")),
            # built in a statement template ({% set out = [] %} ... out.append({...}))
            ("aaporgs", ("signInMaps", W.LIST, "id")),
            ("networkplacements", ("siteNetwork", "subnets", W.LIST, "prefix")),
            # a direct call of the status module
            ("cloudinfrastructures", ("realization", "dns", "reverseZones", W.LIST, "id")),
        ]:
            with self.subTest(plural=plural, path=path):
                self.assertTrue((plural, path) in WRITTEN, f"the walk does not see {plural} {path}")
        for plural, path in [
            ("platformopenshifts", ("deletionBlockedBy",)),
            ("platformopenshifts", ("delivery", "publishFailingSince")),
            ("cloudosos", ("phaseSince",)),
        ]:
            with self.subTest(plural=plural, path=path):
                self.assertTrue((plural, path) in OPERATOR.written, f"the walk does not see {plural} {path}")

    def test_a_shared_file_takes_the_kind_of_its_role(self):
        # fabric_finish.yml computes the plural from the event; its keys reach the kinds of the roles including it
        sites = {s for (plural, _p), ss in WRITTEN.items() if plural == "networkplacements" for s in ss}
        self.assertTrue(any(s.startswith("eda/common/tasks/fabric_finish.yml <- eda/rulebooks/roles/networkplacement_")
                            for s in sites))


class TheSchemaKeepsWhatTheJobsWrite(unittest.TestCase):
    def test_no_written_key_is_dropped(self):
        bad = [d for d in dropped(WRITTEN) if (d[0], d[1]) not in NOT_WRITTEN]
        self.assertEqual(bad, [], "status keys the jobs write and the API server drops:\n" + "\n".join(
            f"  {plural} status.{path}  ({', '.join(sites[:3])})" for plural, path, sites in bad))

    def test_no_operator_key_is_dropped(self):
        bad = dropped(OPERATOR.written)
        self.assertEqual(bad, [], "status keys the operator writes and the API server drops:\n" + "\n".join(
            f"  {plural} status.{path}  ({', '.join(sites[:3])})" for plural, path, sites in bad))

    def test_a_value_the_walk_cannot_see_into_is_a_decision(self):
        found = {**blind(JOBS), **blind(OPERATOR)}
        new = {k: v for k, v in found.items() if not _allowed(*k)}
        self.assertEqual(new, {}, "written values the walk cannot see into, where the schema lists the keys it keeps; "
                                  "make the value visible to the walk or add the place to BLIND with the reason")
        used = {(p if (p, path) in BLIND else "*", path) for p, path in found}
        self.assertEqual(set(BLIND) - used, set(), "a BLIND place the walk no longer finds: remove it")

    def test_the_exceptions_are_still_listed_by_the_walk(self):
        listed = {(p, ".".join(path)) for p, path in WRITTEN}
        self.assertEqual(NOT_WRITTEN - listed, set(), "an exception the walk no longer lists: remove it")

    def test_the_status_library_keys_on_every_kind_it_writes(self):
        # what compute_status_patch adds itself: delivery, edaJobs, conditions, phase, phaseSince, summary, retry
        obj = {"metadata": {"name": "web", "namespace": "entity-acme", "resourceVersion": "1", "generation": 2,
                            "deletionTimestamp": "2030-01-01T00:00:00Z"},
               "status": {"delivery": {"eventId": "e1"}, "conditions": [], "retry": {"attempts": 1}}}
        bodies = []
        for action, body in [("reconciling", {"status": "reconciling", "message": "m"}),
                             ("failed", {"status": "failed", "reason": "EDAFailed", "message": "m"}),
                             ("ready", {"status": "ready", "ready": True, "observedGeneration": 2,
                                        "deletionComplete": True})]:
            patch, _info = S.compute_status_patch(copy.deepcopy(obj), body, {
                "action": action, "event_id": "e1", "job": {"name": "entity-ready", "jobId": "7"}})
            bodies.append(patch["status"])
        # the other delivery shape (a completed event)
        bodies.append({"delivery": {"acknowledgedEventId": "e1", "jobId": "7", "jobStartedAt": "t"}})
        shape = W.Shape()
        ev = W.Evaluator(INDEX)
        for b in bodies:
            shape.merge(ev.value(b))
        paths = list(shape.paths())
        self.assertIn(("edaJobs", W.LIST, "jobId"), paths)
        self.assertIn(("delivery", "completedEventId"), paths)
        self.assertIn(("retry", "failingSince"), paths)
        bad = []
        written_kinds = {plural for plural, _path in WRITTEN}
        self.assertGreater(len(written_kinds), 20)
        for plural in sorted(written_kinds & set(SCHEMAS)):
            schema = SCHEMAS[plural]
            for path in paths:
                ok, at = W.kept(schema, path)
                if not ok and at == len(path) - 1:
                    bad.append(f"{plural} status.{'.'.join(path)}")
        self.assertEqual(bad, [])


class TheProbesFail(unittest.TestCase):
    """A key added where the walk used to be blind is caught: a copy of the writing file with the key added, walked
    alone, against the real schemas (and the unchanged copy is clean)."""

    PROBES = [
        # a key added inside a statement template
        ("jobs", "eda/rulebooks/roles/aaporg_provision/tasks/teams_and_roles.yml",
         "{'id': r.json.id | string, 'name': r.json.name}",
         "{'id': r.json.id | string, 'name': r.json.name, 'kind': 'probe'}",
         ("aaporgs", "signInMaps.[].kind")),
        # a key added to a direct call of the status module
        ("jobs", "eda/rulebooks/roles/cloudinfrastructure_provision/tasks/main.yml",
         "status: \"{{ {'realization': {'ipv6Canary': none}} }}\"",
         "status: \"{{ {'realization': {'ipv6Canary': none}, 'site': {'signIn': {'newKey': 1}}} }}\"",
         ("cloudinfrastructures", "site.signIn.newKey")),
        # a key added to an operator write
        ("operator", "operator/namespace/roles/platformopenshift_delete/tasks/main.yml",
         "          message: Awaiting EDA delete\n",
         "          message: Awaiting EDA delete\n          floatingIPs: {api: {probeKey: x}}\n",
         ("platformopenshifts", "floatingIPs.api.probeKey")),
    ]

    def _walk_copy(self, side, rel, text):
        tmp = tempfile.mkdtemp(prefix="status-walk-probe-")
        try:
            dst = os.path.join(tmp, rel)
            os.makedirs(os.path.dirname(dst))
            with open(dst, "w") as fh:
                fh.write(text)
            if side == "operator":
                for op in ("namespace", "primary"):
                    os.makedirs(os.path.join(tmp, "operator", op), exist_ok=True)
                    shutil.copy(os.path.join(ROOT, "operator", op, "watches.yaml"),
                                os.path.join(tmp, "operator", op, "watches.yaml"))
            idx = W.Index(tmp, side=side)
            res = W.walk(idx, PLURALS, KIND_PLURALS, W.operator_role_kinds(tmp))
            return {(p, path) for p, path, _s in dropped(res.written)}
        finally:
            shutil.rmtree(tmp)

    def test_probes(self):
        for side, rel, old, new, key in self.PROBES:
            with self.subTest(file=rel):
                with open(os.path.join(ROOT, rel)) as fh:
                    text = fh.read()
                self.assertIn(old, text, "the probe's anchor is gone: move the probe")
                self.assertNotIn(key, self._walk_copy(side, rel, text))
                self.assertIn(key, self._walk_copy(side, rel, text.replace(old, new, 1)))


if __name__ == "__main__":
    unittest.main()
