#!/usr/bin/env bash
# Run tests/fabric-jobs/network_jobs_envtest.py inside the automation runner image (the ansible-core the platform's
# automation uses), with the envtest binaries of KUBEBUILDER_ASSETS mounted read-only. Arguments go to the harness.
#   KUBEBUILDER_ASSETS=<envtest dir> RUNNER_IMAGE=<image> tests/fabric-jobs/run-in-runner.sh [--verbose]
#   RUNNER_IMAGE=<the platform's automation runner image>
set -eo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
IMAGE="${RUNNER_IMAGE:-}"
if [[ -z "${KUBEBUILDER_ASSETS:-}" || -z "${IMAGE}" ]]; then
  echo "run-in-runner: set KUBEBUILDER_ASSETS and RUNNER_IMAGE" >&2
  exit 2
fi
exec podman run --rm --security-opt label=disable --entrypoint /usr/bin/env \
  -v "${ROOT}:/src:ro" -v "${KUBEBUILDER_ASSETS}:/envtest:ro" -w /src \
  -e HOME=/tmp -e KUBEBUILDER_ASSETS=/envtest \
  "${IMAGE}" python3 -I tests/fabric-jobs/network_jobs_envtest.py "$@"
