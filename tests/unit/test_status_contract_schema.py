"""The status contract fields are declared in the CRD schemas.

The controllers write phase, phaseSince and summary; the Retry-now bookkeeping writes
lastHandledReconcileRequest; the fabric jobs write realized. The status schemas keep
x-kubernetes-preserve-unknown-fields, so a declaration can only add `oc explain` text and a type check: a field
declared with the wrong type, or with an enum, a format, a pattern, `required` or `nullable`, would make the
status write of a live object fail. These tests pin the declarations to what the writers do.
"""
import glob
import os
import re
import sys
import unittest

import yaml

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
LIB = os.path.join(ROOT, "eda", "rulebooks", "collections", "ansible_collections", "hybridsovereign", "shc",
                   "plugins", "module_utils")
sys.path.insert(0, LIB)

import shc_status as S  # noqa: E402

BASES = os.path.join(ROOT, "operator", "config", "crd", "bases")
PLAIN = ("phase", "phaseSince", "summary")


def crds():
    out = {}
    for path in sorted(glob.glob(os.path.join(BASES, "crd-*.yaml"))):
        with open(path, encoding="utf-8") as fh:
            doc = yaml.safe_load(fh)
        out[doc["spec"]["names"]["kind"]] = doc
    return out


def status_schema(doc):
    return doc["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["status"]


def grep_dirs(dirs, needle):
    for d in dirs:
        for dirpath, _, files in os.walk(d):
            for name in files:
                if name.endswith((".yml", ".yaml")):
                    with open(os.path.join(dirpath, name), encoding="utf-8") as fh:
                        if needle in fh.read():
                            return True
    return False


class StatusContractSchema(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.crds = crds()
        cls.status = {k: status_schema(d) for k, d in cls.crds.items()}

    def declared(self, field):
        return {k for k, s in self.status.items() if field in (s.get("properties") or {})}

    def test_kinds_that_carry_the_contract(self):
        # TenantProfile is written by the Entity job and has no Ready condition or phase of its own
        # (teardown-tier "skip"); every other kind has the Summary column. Iaac has no controller: nothing writes
        # its three fields, so declaring them there is harmless.
        want = set(self.crds) - {"TenantProfile"}
        for field in PLAIN:
            self.assertEqual(self.declared(field), want, field)

    def test_realized_is_declared_where_the_fabric_jobs_write_it(self):
        # eda/common/tasks/fabric_finish.yml writes status.realized (boolean) for the kind of the role that
        # includes it; the attachment circuit job also puts it in its status body.
        roles = os.path.join(ROOT, "eda", "rulebooks", "roles")
        want = set()
        for kind, doc in self.crds.items():
            singular = doc["spec"]["names"]["singular"]
            dirs = [os.path.join(roles, f"{singular}_{part}") for part in ("provision", "teardown")]
            if grep_dirs([d for d in dirs if os.path.isdir(d)], "fabric_finish.yml"):
                want.add(kind)
        self.assertEqual(self.declared("realized"), want)
        self.assertEqual(len(want), 9, sorted(want))

    def test_last_handled_is_declared_where_the_operator_records_it(self):
        # operator/roles/_common/tasks/publish_operator_event.yml records the handled Retry-now value; a kind
        # whose operator role never includes it (UIHealthChecker) or that has no operator role (Iaac,
        # TenantProfile) does not carry the field.
        want = set()
        for kind, doc in self.crds.items():
            singular = doc["spec"]["names"]["singular"]
            dirs = [os.path.join(ROOT, "operator", side, "roles", singular) for side in ("primary", "namespace")]
            if grep_dirs([d for d in dirs if os.path.isdir(d)], "publish_operator_event"):
                want.add(kind)
        self.assertEqual(self.declared("lastHandledReconcileRequest"), want)
        self.assertEqual(want & {"UIHealthChecker", "Iaac", "TenantProfile"}, set())

    def test_declarations_are_permissive(self):
        # Only a type and a description: no enum, format, pattern, required, nullable, default or bounds.
        types = {"realized": "boolean"}
        for kind, schema in self.status.items():
            for field in (*PLAIN, "realized", "lastHandledReconcileRequest"):
                node = (schema.get("properties") or {}).get(field)
                if node is None:
                    continue
                self.assertEqual(set(node), {"type", "description"}, (kind, field))
                self.assertEqual(node["type"], types.get(field, "string"), (kind, field))
            self.assertNotIn("required", schema, kind)

    def test_status_keeps_preserving_unknown_fields(self):
        for kind, schema in self.status.items():
            if kind == "TenantProfile":
                continue
            self.assertIs(schema.get("x-kubernetes-preserve-unknown-fields"), True, kind)

    def test_one_description_per_field(self):
        for field in (*PLAIN, "realized", "lastHandledReconcileRequest"):
            texts = {s["properties"][field]["description"] for s in self.status.values()
                     if field in (s.get("properties") or {})}
            self.assertEqual(len(texts), 1, (field, texts))

    def test_phase_description_names_every_phase_the_library_writes(self):
        text = self.status["HybridNetwork"]["properties"]["phase"]["description"]
        for phase in S.PHASES:
            self.assertRegex(text, r"\b%s\b" % re.escape(phase))


if __name__ == "__main__":
    unittest.main()
