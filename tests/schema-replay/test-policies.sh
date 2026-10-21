#!/usr/bin/env bash
# Behaviour test of the primary chart's GitOps admission policies (shc-fabric-ledger-guard, shc-gitops-managed,
# shc-gitops-delete-guard):
# renders them from the chart (P4 once as Warn, once as Deny), replays OBJECTS into an envtest API server with
# them and runs policy_checks.py there.
#   tests/schema-replay/test-policies.sh OBJECTS_DIR [VALUES_FILE ...]
# Needs helm, KUBEBUILDER_ASSETS (envtest binaries). OBJECTS_DIR must hold the HybridFabric and CloudInfrastructure
# that policy_checks.py uses (defaults sovereign-cloud/lab and sovereign-cloud/iaas1; see --fabric/--object there;
# POLICY_CHECK_ARGS passes more, e.g. "--unmarked cloudgateway/sovereign-cloud/iaas1").
set -eo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "${HERE}/../.." && pwd)"
OBJECTS="${1:?usage: test-policies.sh OBJECTS_DIR [VALUES_FILE ...]}"
shift
VALUES=()
for v in "$@"; do VALUES+=(-f "$v"); done
WORK="$(mktemp -d "${TMPDIR:-/tmp}/shc-policy-test-XXXXXX")"
trap 'rm -rf "${WORK}"' EXIT
rc=0
for mode in Warn Deny; do
  helm template hybridsovereign-primary-operator "${ROOT}/operator/primary/helm" "${VALUES[@]}" \
    --set "admission.gitopsManaged.mode=${mode}" --show-only templates/gitops-policies.yaml >"${WORK}/policies-${mode}.yaml"
  echo "== shc-gitops-managed ${mode}, shc-fabric-ledger-guard as rendered"
  python3 -I "${HERE}/replay.py" --objects "${OBJECTS}" --crds "${ROOT}/operator/primary/helm/templates/crds.yaml" \
    --policies "${WORK}/policies-${mode}.yaml" \
    --exec "python3 -I ${HERE}/policy_checks.py --gitops-mode ${mode} ${POLICY_CHECK_ARGS:-}" || rc=1
done
exit "${rc}"
