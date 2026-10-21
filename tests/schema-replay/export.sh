#!/usr/bin/env bash
# Read-only export of the hybridsovereign.redhat API state of a cluster, as replay input.
#   tests/schema-replay/export.sh OUT_DIR
# Uses the current KUBECONFIG (or $KUBECONFIG) and $KUBECTL (default: kubectl, else oc). Writes
#   OUT_DIR/objects/<plural>.hybridsovereign.redhat.json   every object of every kind, status included
#   OUT_DIR/objects/COUNTS.txt                             "<resource> <count>" per kind
#   OUT_DIR/crds/<name>.json                               every hybridsovereign.redhat CRD
#   OUT_DIR/namespaces.json                                the namespaces those objects live in (name and labels only)
# Never exports Secrets: the hybridsovereign.redhat kinds hold none, and nothing else is read.
set -eo pipefail
OUT="${1:?usage: export.sh OUT_DIR}"
KUBECTL="${KUBECTL:-$(command -v kubectl || command -v oc)}"
GROUP=hybridsovereign.redhat
mkdir -p "${OUT}/objects" "${OUT}/crds"
: >"${OUT}/objects/COUNTS.txt"
for r in $("${KUBECTL}" api-resources --api-group="${GROUP}" -o name | sort); do
  "${KUBECTL}" get "${r}" -A -o json >"${OUT}/objects/${r}.json"
  echo "${r} $(jq '.items | length' "${OUT}/objects/${r}.json")" >>"${OUT}/objects/COUNTS.txt"
done
"${KUBECTL}" get crd -o json | jq --arg g "${GROUP}" '[.items[] | select(.spec.group == $g)]' >"${OUT}/crds.tmp.json"
for n in $(jq -r '.[].metadata.name' "${OUT}/crds.tmp.json"); do
  jq --arg n "${n}" '.[] | select(.metadata.name == $n)' "${OUT}/crds.tmp.json" >"${OUT}/crds/${n}.json"
done
rm -f "${OUT}/crds.tmp.json"
namespaces="$(cat "${OUT}"/objects/*.json | jq -r '.items[].metadata.namespace // empty' | sort -u)"
{
  echo '{"apiVersion": "v1", "kind": "List", "items": ['
  first=1
  for ns in ${namespaces}; do
    [ "${first}" = 1 ] || echo ','
    first=0
    "${KUBECTL}" get namespace "${ns}" -o json | jq '{apiVersion, kind, metadata: {name: .metadata.name, labels: (.metadata.labels // {})}}'
  done
  echo ']}'
} >"${OUT}/namespaces.json"
echo "export: $(wc -l <"${OUT}/objects/COUNTS.txt") kinds, $(awk '{s += $2} END {print s + 0}' "${OUT}/objects/COUNTS.txt") objects, $(find "${OUT}/crds" -name "*.json" | wc -l) CRDs, $(echo "${namespaces}" | grep -c . || true) namespaces -> ${OUT}"
