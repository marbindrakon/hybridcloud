#!/usr/bin/env bash
# Self-test of replay.py: it must report a prune and a rejection when the candidate CRDs would cause them.
#   tests/schema-replay/selftest.sh OBJECTS_DIR
# Needs KUBEBUILDER_ASSETS (envtest). OBJECTS_DIR must hold at least one HybridFabric with status.allocated and
# one CloudInfrastructure (the baseline export does).
#   1. HybridFabric status loses x-kubernetes-preserve-unknown-fields and its declared `allocated` field:
#      replay must report status.allocated as pruned.
#   2. CloudInfrastructure gets the root rule `self.spec.type == 'never'`: replay must report rejections.
#   3. The unchanged CRDs: zero rejections, zero diffs.
set -eo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/../.." && pwd)"
OBJECTS="${1:?usage: selftest.sh OBJECTS_DIR}"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/shc-replay-selftest-XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT
CRDS="${ROOT}/operator/primary/helm/templates/crds.yaml"

python3 -I - "${CRDS}" "${WORK}" <<'PYEOF'
import sys, re, yaml
src, work = sys.argv[1], sys.argv[2]
text = re.sub(r"\{\{-?\s*/\*.*?\*/\s*-?\}\}", "", open(src).read(), flags=re.S)
docs = [d for d in yaml.safe_load_all("\n".join(l for l in text.splitlines() if "{{" not in l)) if d]
def write(name, mutate):
    out = []
    for d in docs:
        d = yaml.safe_load(yaml.safe_dump(d))
        mutate(d)
        out.append(d)
    with open(f"{work}/{name}.yaml", "w") as f:
        yaml.safe_dump_all(out, f)
def prune(d):
    if d["spec"]["names"]["kind"] == "HybridFabric":
        st = d["spec"]["versions"][0]["schema"]["openAPIV3Schema"]["properties"]["status"]
        st.pop("x-kubernetes-preserve-unknown-fields", None)
        st["properties"].pop("allocated", None)
def reject(d):
    if d["spec"]["names"]["kind"] == "CloudInfrastructure":
        s = d["spec"]["versions"][0]["schema"]["openAPIV3Schema"]
        s.setdefault("x-kubernetes-validations", []).append({"rule": "self.spec.type == 'never'", "message": "selftest"})
write("prune", prune)
write("reject", reject)
PYEOF

fail=0
run() { python3 -I "${HERE}/replay.py" --objects "${OBJECTS}" --report "${WORK}/$1.json" "${@:2}" >"${WORK}/$1.out" 2>&1; }

if run prune --crds "${WORK}/prune.yaml"; then
  echo "FAIL selftest prune: replay passed"; fail=1
elif jq -e '[.diffs[] | select(.object | startswith("HybridFabric/")) | .paths[] | select(.path == "status.allocated" and .kind == "pruned")] | length > 0' "${WORK}/prune.json" >/dev/null; then
  echo "PASS selftest prune: status.allocated reported as pruned"
else
  echo "FAIL selftest prune: no prune of status.allocated reported"; cat "${WORK}/prune.out"; fail=1
fi

if run reject --crds "${WORK}/reject.yaml"; then
  echo "FAIL selftest reject: replay passed"; fail=1
elif jq -e '[.rejections[] | select(.object | startswith("CloudInfrastructure/"))] | length > 0' "${WORK}/reject.json" >/dev/null; then
  echo "PASS selftest reject: CloudInfrastructure objects rejected by the CEL rule"
else
  echo "FAIL selftest reject: no rejection reported"; cat "${WORK}/reject.out"; fail=1
fi

if run clean --crds "${CRDS}"; then
  echo "PASS selftest clean: zero rejections, zero diffs"
else
  echo "FAIL selftest clean"; cat "${WORK}/clean.out"; fail=1
fi
exit "${fail}"
