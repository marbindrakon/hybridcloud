#!/usr/bin/env bash
# The acme-corp demo tenant exists twice: bootstrap/ansible/project/files/demo/acme-corp.yaml (applied by the
# operator-samples job, baked into the ansible-runner image) and samples/demo/acme-corp.yaml (for readers). They must
# be byte-identical. Exit 1 when they differ.
set -eo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
a="${ROOT}/bootstrap/ansible/project/files/demo/acme-corp.yaml"
b="${ROOT}/samples/demo/acme-corp.yaml"
if cmp -s "${a}" "${b}"; then
  echo "check-demo-samples: samples/demo/acme-corp.yaml matches the seeded demo"
else
  echo "check-demo-samples: samples/demo/acme-corp.yaml differs from ${a#"${ROOT}/"}; copy it" >&2
  diff -u "${a}" "${b}" >&2 || true
  exit 1
fi
