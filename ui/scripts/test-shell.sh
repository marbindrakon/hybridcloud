#!/usr/bin/env bash
# Unit tests for the UI shell (error model, server modules, scope, registry, gate).
#
# The pure TypeScript modules are bundled with esbuild into a temporary
# directory (SHELL_BUNDLE_DIR) and the node:test files in ui/scripts/tests
# import them from there. Server modules are plain ESM and imported directly.
set -o pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ui="$(cd "$here/.." && pwd)"
out="$(mktemp -d)"
trap 'rm -rf "$out"' EXIT

# entry (relative to ui/packages/shared/src) -> bundle name
entries=(
  "hooks/errors.ts:errors"
  "scope/parseGroups.ts:parseGroups"
  "scope/ScopeProvider.tsx:scopeProvider"
  "access/errorText.ts:errorText"
  "access/roleHints.ts:roleHints"
  "access/permissionGate.ts:permissionGate"
  "nav/registry.ts:registry"
  "nav/paths.ts:paths"
  "status/phase.ts:phase"
  "hooks/fetchState.ts:fetchState"
  "forms/apiErrors.ts:apiErrors"
  "status/completion.ts:completion"
  "forms/entityFields.ts:entityFields"
  "hooks/k8s.ts:k8s"
  "hooks/overviewCrs.ts:overviewCrs"
  "forms/attachable.ts:attachable"
  "access/sharedWithYou.ts:sharedWithYou"
)
for e in "${entries[@]}"; do
  src="${e%%:*}"; name="${e##*:}"
  [ -f "$ui/packages/shared/src/$src" ] || continue
  "$ui/node_modules/.bin/esbuild" "$ui/packages/shared/src/$src" --bundle --format=esm --platform=node \
    --log-level=warning --external:@patternfly/* --jsx=automatic --loader:.json=json \
    --outfile="$out/$name.mjs" || exit 1
done

# Spec schemas of the platform CRDs (sanitizer drift test).
python3 -I "$here/crd-spec-schemas.py" > "$out/crd-specs.json" || exit 1
export CRD_SPECS_JSON="$out/crd-specs.json"
export SHELL_BUNDLE_DIR="$out"
export UI_ROOT="$ui"
rc=0
node --test "$here"/tests/*.test.mjs || rc=1
# Generated files must match their sources.
node "$here/gen-console-extensions.mjs" --check || rc=1
python3 -I "$here/gen-role-hints.py" --check || rc=1
python3 -I "$here/gen-status-reasons.py" --check || rc=1
exit $rc
