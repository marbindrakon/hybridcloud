.DEFAULT_GOAL := help

# ─── Required environment variables ──────────────────────────────────────────
REQUIRED_VARS := \
  OCP_CENTRAL_SERVER OCP_CENTRAL_USERNAME OCP_CENTRAL_PASSWORD \
  OCP_SERVICES_SERVER OCP_SERVICES_USERNAME OCP_SERVICES_PASSWORD \
  OCI_REGISTRY OCI_REGISTRY_TOKEN \
  OCI_ROBOT_USERNAME OCI_ROBOT_PASSWORD \
  GITHUB_URL GITHUB_TOKEN \
  IMAGE_REGISTRY IMAGE_REGISTRY_USERNAME IMAGE_REGISTRY_PASSWORD \
  OSO_CLOUDS \
  AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY AWS_ACCOUNT_ID

# ─── Derived OCI values ──────────────────────────────────────────────────────
OCI_HOST := $(shell echo "$(OCI_REGISTRY)" | sed -E 's|^https?://||' | cut -d'/' -f1)
OCI_NAMESPACE := $(shell echo "$(OCI_REGISTRY)" | sed -E 's|^https?://||' | sed -n 's|.*/organization/||p' | cut -d'/' -f1)
ifeq ($(OCI_NAMESPACE),)
  OCI_NAMESPACE := hybrid-sovereign
endif

# ─── Helpers ──────────────────────────────────────────────────────────────────
BOLD  := $(shell tput bold 2>/dev/null || echo "")
GREEN := $(shell tput setaf 2 2>/dev/null || echo "")
RED   := $(shell tput setaf 1 2>/dev/null || echo "")
RESET := $(shell tput sgr0 2>/dev/null || echo "")

define ok_print
printf "  $(GREEN)✓$(RESET)  %s\n" "$(1)"
endef
define ok
	@$(call ok_print,$(1))
endef
define fail_print
printf "  $(RED)✗$(RESET)  %s\n" "$(1)"
endef
define fail
	@$(call fail_print,$(1))
endef

# ─── Import targets from make/ ───────────────────────────────────────────────
include make/check-env.mk
include make/check-env-central.mk
include make/add-docker-repo.mk
include make/upload-acm-chart.mk
include make/upload-rhbk-chart.mk
include make/upload-rhbk-config-chart.mk
include make/upload-sovereign-namespaces-chart.mk
include make/upload-acs-chart.mk
include make/upload-vault-chart.mk
include make/upload-aap-chart.mk
include make/upload-odf-chart.mk
include make/upload-quay-chart.mk
include make/upload-crunchy-postgres-chart.mk
include make/upload-gitea-chart.mk
include make/upload-external-secrets-chart.mk
include make/upload-ansible-job-chart.mk
include make/upload-vault-secret-store-chart.mk
include make/upload-vault-central-namespace-chart.mk
include make/upload-sovereign-job-chart.mk
include make/upload-sovereign-job-rbac-chart.mk
include make/upload-vault-services-init-chart.mk
include make/upload-entity-operator-chart.mk
include make/upload-plugin-rbac-chart.mk
include make/upload-plugin-vault-chart.mk
include make/upload-plugin-aap-chart.mk
include make/upload-plugin-quay-chart.mk
include make/upload-dashboard-chart.mk
include make/upload-tenancy-dashboard-chart.mk
include make/upload-team-operator-chart.mk
include make/upload-assignment-operator-chart.mk
include make/upload-project-operator-chart.mk
include make/upload-platformopenshift-operator-chart.mk
include make/upload-cloudoso-operator-chart.mk
include make/upload-openstackmigration-operator-chart.mk
include make/upload-ocp-base-chart.mk
include make/upload-assignment-central-rbac-chart.mk
include make/upload-sovereign-assignment-chart.mk
include make/aap-load-manifest.mk
include make/aap-store-admin.mk
include make/upload-oso-creds.mk
include make/upload-aws-creds.mk
include make/ansible-runner.mk
include make/run-eda-config-local.mk
include make/init-bootstrap-common.mk
include make/init-central-argo.mk
include make/init-central-secrets.mk
include make/init-central-applicationset.mk
include make/init-services-pull-secrets.mk
include make/init-services-argocd-sa.mk
include make/upload-event-forwarder-chart.mk
include make/init-services-gitea-secret.mk
include make/init-central-gitea-secret.mk
include make/help.mk
include make/eda-sync.mk
include make/upload-cnv-chart.mk
include make/upload-mtv-chart.mk
include make/upload-dns-forwarder-chart.mk
include make/upload-amq-streams-chart.mk
include make/upload-primary-operator-chart.mk
include make/upload-iaac-chart.mk
include make/lab-config.mk

##@ Convenience

UPLOAD_TARGETS := \
  upload-acm-chart \
  upload-sovereign-namespaces-chart \
  upload-rhbk-chart \
  upload-rhbk-config-chart \
  upload-acs-chart \
  upload-vault-chart \
  upload-aap-chart \
  upload-odf-chart \
  upload-quay-chart \
  upload-crunchy-postgres-chart \
  upload-gitea-chart \
  upload-external-secrets-chart \
  upload-ansible-job-chart \
  upload-vault-secret-store-chart \
  upload-sovereign-job-chart \
  upload-sovereign-job-rbac-chart \
  upload-vault-services-init-chart \
  upload-entity-operator-chart \
  upload-plugin-rbac-chart \
  upload-plugin-vault-chart \
  upload-plugin-aap-chart \
  upload-plugin-quay-chart \
  upload-dashboard-chart \
  upload-tenancy-dashboard-chart \
  upload-team-operator-chart \
  upload-assignment-operator-chart \
  upload-project-operator-chart \
  upload-platformopenshift-operator-chart \
  upload-cloudoso-operator-chart \
  upload-ocp-base-chart \
  upload-vault-central-namespace-chart \
  upload-assignment-central-rbac-chart \
  upload-sovereign-assignment-chart \
  upload-cnv-chart \
  upload-mtv-chart \
  upload-dns-forwarder-chart \
  upload-amq-streams-chart \
  upload-primary-operator-chart \
  upload-iaac-chart

.PHONY: upload-all-charts
upload-all-charts: $(UPLOAD_TARGETS) ## Upload ALL Helm charts to OCI registry
	$(call ok,All charts uploaded)

