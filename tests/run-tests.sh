#!/usr/bin/env bash
# Hybrid Sovereign — Local Test Runner
# Runs helm lint, YAML validation, and documents manual cluster gates.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
BOOTSTRAP="${REPO_ROOT}/bootstrap"
PASS=0
FAIL=0
SKIP=0

log_pass() { echo "  PASS: $*"; PASS=$((PASS + 1)); }
log_fail() { echo "  FAIL: $*"; FAIL=$((FAIL + 1)); }
log_skip() { echo "  SKIP: $*"; SKIP=$((SKIP + 1)); }
log_info() { echo "==> $*"; }

require_cmd() {
  if ! command -v "$1" &>/dev/null; then
    log_fail "required command not found: $1"
    return 1
  fi
}

# ─── Phase 1: Helm Lint ───────────────────────────────────────────────────────
run_helm_lint() {
  log_info "Helm lint — all charts under hybridcloud/"
  if ! require_cmd helm; then
    return
  fi

  local charts=()
  while IFS= read -r chart; do
    charts+=("$chart")
  done < <(find "${REPO_ROOT}" -name Chart.yaml -not -path '*/.git/*' | sort)

  if [[ ${#charts[@]} -eq 0 ]]; then
    log_fail "no Chart.yaml files found"
    return
  fi

  for chart_yaml in "${charts[@]}"; do
    local chart_dir
    chart_dir="$(dirname "$chart_yaml")"
    local chart_name
    chart_name="$(basename "$chart_dir")"
    if helm lint "$chart_dir" &>/dev/null; then
      log_pass "helm lint ${chart_name} (${chart_dir#"${REPO_ROOT}/"})"
    else
      log_fail "helm lint ${chart_name} (${chart_dir#"${REPO_ROOT}/"})"
      helm lint "$chart_dir" 2>&1 | tail -5 || true
    fi
  done
}

# ─── Phase 2: YAML Validation ─────────────────────────────────────────────────
run_yaml_validation() {
  log_info "YAML validation — templates and samples"
  if ! require_cmd python3; then
    return
  fi

  python3 - <<'PYEOF' "${REPO_ROOT}"
import sys, pathlib

try:
    import yaml
except ImportError:
    print("  FAIL: PyYAML not installed (pip install pyyaml)")
    sys.exit(1)

root = pathlib.Path(sys.argv[1])
dirs = [
    root / "bootstrap" / "helm",
    root / "samples",
    root / "operator",
    root / "iaac" / "helm",
]
errors = []
checked = 0

skip_parts = {"templates", ".git", "config"}

for base in dirs:
    if not base.exists():
        continue
    for path in sorted(base.rglob("*.yaml")) + sorted(base.rglob("*.yml")):
        if skip_parts & set(path.parts):
            continue
        try:
            text = path.read_text()
        except OSError:
            continue
        if "{{" in text or "{%" in text:
            continue
        checked += 1
        try:
            with open(path) as f:
                list(yaml.safe_load_all(f))
        except yaml.YAMLError as e:
            errors.append(f"{path.relative_to(root)}: {e}")

if errors:
    for e in errors:
        print(f"  FAIL: {e}")
    print(f"  FAIL: {len(errors)} YAML errors in {checked} files")
    sys.exit(1)
else:
    print(f"  PASS: {checked} YAML files parsed successfully")
PYEOF

  if [[ $? -eq 0 ]]; then
    PASS=$((PASS + 1))
  else
    FAIL=$((FAIL + 1))
  fi
}

# ─── Phase 3: Secret Pattern Scan (static) ────────────────────────────────────
run_secret_scan() {
  log_info "Static secret pattern scan (hybridcloud/)"
  if ! require_cmd git; then
    log_skip "git not available"
    return
  fi

  local matches
  matches="$(git -C "${REPO_ROOT}" grep -iE '(password|apikey|private_key)\s*[:=]\s*["\x27][^"\x27]{8,}' -- \
    'hybridcloud/' 2>/dev/null | grep -v 'REDACTED\|example\|changeme\|placeholder\|<.*>' || true)"

  if [[ -z "$matches" ]]; then
    log_pass "no suspicious credential literals in hybridcloud/"
  else
    log_fail "potential credential literals found:"
    echo "$matches" | head -10
  fi
}

# ─── Phase 4: Executable Script Permissions ───────────────────────────────────
run_script_permissions() {
  log_info "Test script permissions"
  local scripts=(
    "${SCRIPT_DIR}/run-tests.sh"
    "${SCRIPT_DIR}/argocd-deploy/verify-sync.sh"
  )
  for s in "${scripts[@]}"; do
    if [[ -x "$s" ]]; then
      log_pass "executable: ${s#"${REPO_ROOT}/"}"
    else
      log_fail "not executable: ${s#"${REPO_ROOT}/"}"
    fi
  done
}

# ─── Phase 5: Manual Cluster Gates (documentation only) ───────────────────────
print_manual_gates() {
  log_info "Manual cluster gates (require live cluster — not executed)"
  cat <<'EOF'

  The following gates MUST be run against live clusters before release.
  See tests/argocd-deploy/DEPLOYMENT_GATES.md for per-phase details.

  GATE-M01  ArgoCD sync health
            ./tests/argocd-deploy/verify-sync.sh --context central-admin

  GATE-M02  Entity lifecycle
            Follow tests/functional/README.md (TC-F001–F010)

  GATE-M03  RBAC matrix
            Follow tests/rbac-access/README.md (TC-RBAC-001–008)

  GATE-M04  Cluster connectivity
            Follow tests/connectivity/README.md (TC-CONN-001–015)

  GATE-M05  Pod restart resilience
            Follow tests/restart/README.md (TC-RS-001–012)

  GATE-M06  E2E migration (VDDK deferred)
            Follow tests/e2e/README.md (Phase 1–2 required; Phase 3 deferred)

  GATE-M07  Security review sign-off
            Complete tests/security/SECURITY_REVIEW.md checklist

  GATE-M08  Hardening gap review
            Review hardening-checks/reports/migration-hardening-gap-analysis.md

EOF
  log_skip "manual gates documented (8 gates)"
}

# ─── Main ─────────────────────────────────────────────────────────────────────
# ─── Phase 5: Python unit tests (status library, watches, tenancy) ───────────
run_unit_tests() {
  log_info "Python unit tests — tests/unit"
  if ! require_cmd python3; then
    return
  fi
  if (cd "${REPO_ROOT}" && python3 -m unittest discover -s tests/unit) >/tmp/shc-unit-tests.log 2>&1; then
    log_pass "unit tests ($(grep -Eo 'Ran [0-9]+ tests' /tmp/shc-unit-tests.log))"
  else
    log_fail "unit tests"
    tail -20 /tmp/shc-unit-tests.log || true
  fi
  if "${REPO_ROOT}/scripts/sync-shc-status.sh" --check; then
    log_pass "status library copies in shc-cluster-base match the collection"
  else
    log_fail "status library copies drifted (run scripts/sync-shc-status.sh)"
  fi
}

# --- be-foundations ---
# ─── Phase 6: Repository consistency checks ──────────────────────────────────
run_repo_checks() {
  log_info "Repository consistency — EDA includes, generated CRDs"
  if ! require_cmd python3; then
    return
  fi
  if (cd "${REPO_ROOT}" && python3 -I scripts/check-includes.py) >/tmp/shc-check-includes.log 2>&1; then
    log_pass "EDA includes resolve ($(tail -1 /tmp/shc-check-includes.log))"
  else
    log_fail "EDA includes (scripts/check-includes.py)"
    grep MISSING /tmp/shc-check-includes.log | head -20 || true
  fi
  if (cd "${REPO_ROOT}" && python3 -m unittest discover -s tests/schema-replay -p 'test_*.py') >/tmp/shc-replay-unit.log 2>&1; then
    log_pass "schema-replay unit tests ($(grep -Eo 'Ran [0-9]+ tests' /tmp/shc-replay-unit.log))"
  else
    log_fail "schema-replay unit tests"
    tail -20 /tmp/shc-replay-unit.log || true
  fi
  if (cd "${REPO_ROOT}" && python3 -I scripts/gen-crds.py --check) >/tmp/shc-gen-crds.log 2>&1; then
    log_pass "crds.yaml generated from operator/config/crd/bases"
  else
    log_fail "crds.yaml drift (run python3 -I scripts/gen-crds.py)"
    cat /tmp/shc-gen-crds.log
  fi
}
# --- end be-foundations ---

main() {
  echo "Hybrid Sovereign Test Runner"
  echo "Repository: ${REPO_ROOT}"
  echo "Date: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo ""

  run_helm_lint
  echo ""
  run_yaml_validation
  echo ""
  run_secret_scan
  echo ""
  run_script_permissions
  echo ""
  run_unit_tests
  echo ""
  run_repo_checks
  echo ""
  print_manual_gates
  echo ""

  echo "────────────────────────────────────────"
  echo "Summary: PASS=${PASS}  FAIL=${FAIL}  SKIP=${SKIP}"
  echo "────────────────────────────────────────"

  if [[ ${FAIL} -gt 0 ]]; then
    echo "Result: FAIL — fix failures before merge"
    exit 1
  fi
  echo "Result: PASS — automated checks OK; run manual gates before deploy"
  exit 0
}

main "$@"
