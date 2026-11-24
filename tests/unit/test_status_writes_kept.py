"""The CRDs keep every status key the jobs write.

The API server drops (prunes) a key its schema does not allow: an object that lists `properties` keeps only those,
also below a `status` that preserves unknown fields. A job that writes a key the schema drops and reads it back on
its next run sees nothing there (a cluster's floating IPs were found again by a dropped description, and a second
pair was made). This test walks every status write of the jobs (the includes of eda/common/tasks/patch_cr_status.yml
under eda/rulebooks/roles/ and eda/common/tasks/; tests/unit/status_write_walk.py says how) and every key the status
library adds itself, against each kind's CRD schema, and fails for a key the API server would drop.

Writers outside the status library (the install observer's module, the Python helpers that patch status themselves)
are not walked here.
"""
import copy
import os
import sys
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


def crd_status_schemas():
    """{plural: status schema}, {singular: plural}."""
    schemas, plurals = {}, {}
    for f in sorted(os.listdir(BASES)):
        with open(os.path.join(BASES, f)) as fh:
            d = yaml.safe_load(fh)
        names = d["spec"]["names"]
        root = d["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
        schemas[names["plural"]] = root.get("properties", {}).get("status", {})
        plurals[names["singular"]] = names["plural"]
    return schemas, plurals


SCHEMAS, PLURALS = crd_status_schemas()
INDEX = W.Index(ROOT)
WRITTEN = W.written_paths(INDEX, PLURALS)


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
        reached = {s.split(" <- ")[0] for sites in WRITTEN.values() for s in sites}
        bodies = {w.path for w in INDEX.writes if "cr_status_body" in w.vars}
        self.assertEqual(bodies - reached, set(), "status writes that reach no kind")

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
        ]:
            with self.subTest(plural=plural, path=path):
                self.assertTrue((plural, path) in WRITTEN, f"the walk does not see {plural} {path}")

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


if __name__ == "__main__":
    unittest.main()
