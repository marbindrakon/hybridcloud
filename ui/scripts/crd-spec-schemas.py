"""Print the spec schema of every platform CRD as JSON ({kind: openAPIV3Schema.properties.spec}).

Reads operator/config/crd/bases/crd-*.yaml (the per-kind CRD sources). Lines holding
Helm template actions ({{ ... }}) only appear in metadata labels there and are
dropped before parsing. Used by ui/scripts/tests/sanitizers.test.mjs.

  python3 -I ui/scripts/crd-spec-schemas.py > out.json
"""
import json
import pathlib
import sys

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
BASES = ROOT / "operator/config/crd/bases"


def main():
    out = {}
    for path in sorted(BASES.glob("crd-*.yaml")):
        text = "\n".join(line for line in path.read_text().splitlines() if "{{" not in line)
        for doc in yaml.safe_load_all(text):
            if not doc or doc.get("kind") != "CustomResourceDefinition":
                continue
            kind = doc["spec"]["names"]["kind"]
            for version in doc["spec"].get("versions", []):
                if not version.get("storage", True):
                    continue
                schema = version.get("schema", {}).get("openAPIV3Schema", {})
                spec = schema.get("properties", {}).get("spec")
                if spec is not None:
                    out[kind] = spec
    json.dump(out, sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
