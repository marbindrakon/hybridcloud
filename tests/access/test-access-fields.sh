#!/usr/bin/env bash
# Behaviour test of the primary chart's shc-access-field-ladder policy (Team/Project access lists): renders it from
# the chart (once as Deny, once as Warn), starts an envtest API server with the candidate CRDs and the policy
# (tests/schema-replay/replay.py, no objects) and runs access_field_checks.py there.
#   tests/access/test-access-fields.sh [VALUES_FILE ...]
# Needs helm and KUBEBUILDER_ASSETS (envtest binaries).
set -eo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/../.." && pwd)"
VALUES=()
for v in "$@"; do VALUES+=(-f "$v"); done
WORK="$(mktemp -d "${TMPDIR:-/tmp}/shc-access-test-XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT
mkdir -p "${WORK}/objects"
rc=0
for mode in Deny Warn; do
  helm template hybridsovereign-primary-operator "${ROOT}/operator/primary/helm" "${VALUES[@]}" \
    --set "admission.accessFieldLadder.mode=${mode}" \
    --show-only templates/admission-policy-access-fields.yaml >"${WORK}/policy-${mode}.yaml"
  echo "== shc-access-field-ladder ${mode}"
  python3 -I "${ROOT}/tests/schema-replay/replay.py" --objects "${WORK}/objects" \
    --crds "${ROOT}/operator/primary/helm/templates/crds.yaml" --policies "${WORK}/policy-${mode}.yaml" \
    --exec "python3 -I ${HERE}/access_field_checks.py --mode ${mode}" || rc=1
done
exit "${rc}"
