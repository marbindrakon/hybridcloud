#!/usr/bin/env bash
# Copy the pure status library (collection module_utils) into the shc-cluster-base chart, which
# cannot read files outside itself. --check reports drift without copying (exit 1 on drift).
set -eo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SRC="${ROOT}/eda/rulebooks/collections/ansible_collections/hybridsovereign/shc/plugins/module_utils"
DST="${ROOT}/charts/charts/shc-cluster-base/files"
rc=0
for f in shc_status.py shc_status_reasons.py; do
  if [[ "${1:-}" == "--check" ]]; then
    if ! cmp -s "${SRC}/${f}" "${DST}/${f}"; then
      echo "drift: ${DST#"${ROOT}/"}/${f} differs from ${SRC#"${ROOT}/"}/${f} (run scripts/sync-shc-status.sh)"
      rc=1
    fi
  else
    cp "${SRC}/${f}" "${DST}/${f}"
    echo "synced ${f}"
  fi
done
exit "${rc}"
