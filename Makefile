.DEFAULT_GOAL := help

REQUIRED_VARS := \
  OCP_CENTRAL_SERVER OCP_CENTRAL_USERNAME OCP_CENTRAL_PASSWORD \
  OCP_SERVICES_SERVER OCP_SERVICES_USERNAME OCP_SERVICES_PASSWORD \
  OCI_REGISTRY OCI_REGISTRY_TOKEN \
  OCI_ROBOT_USERNAME OCI_ROBOT_PASSWORD \
  GITHUB_URL GITHUB_TOKEN \
  IMAGE_REGISTRY IMAGE_REGISTRY_USERNAME IMAGE_REGISTRY_PASSWORD

OCI_HOST := $(shell echo "$(OCI_REGISTRY)" | sed -E 's|^https?://||' | cut -d'/' -f1)
OCI_NAMESPACE := $(shell echo "$(OCI_REGISTRY)" | sed -E 's|^https?://||' | sed -n 's|.*/organization/||p' | cut -d'/' -f1)
ifeq ($(OCI_NAMESPACE),)
  OCI_NAMESPACE := hybrid-sovereign
endif

include make/help.mk
include make/check-env.mk
include bootstrap/make/*.mk

.PHONY: help check-env lint-all build-operators build-iaac upload-hybrid-charts check-content check-manifests \
        check-includes crds crds-check replay sar-matrix
help:
	@$(MAKE) -f make/help.mk help

build-operators:
	$(MAKE) -C operator/primary operator-build-push
	$(MAKE) -C operator/namespace operator-build-push

build-iaac:
	podman build -t $(OCI_HOST)/hybrid-sovereign/iaac-git-sync:0.1.0 -f iaac/Dockerfile iaac/
	podman push $(OCI_HOST)/hybrid-sovereign/iaac-git-sync:0.1.0

upload-hybrid-charts:
	$(MAKE) -C bootstrap upload-amq-streams-chart upload-primary-operator-chart upload-iaac-chart

test:
	./tests/run-tests.sh

lint-all:
	@find bootstrap/helm/charts -name Chart.yaml -execdir helm lint . \;
	@helm lint bootstrap/helm/central
	@helm lint bootstrap/helm/init
	@helm lint operator/primary/helm
	@helm lint operator/namespace/helm
	@helm lint iaac/helm

# Content lint: banned words, CRD description rules, lab knowledge, generated blocks
# (docs/content/style.md). Repo-only; reads no cluster and no Secret.
check-content:
	python3 -I scripts/check-content.py --self-test
	python3 -I scripts/check-content.py
	python3 -I scripts/crd-descriptions.py lockstep
	python3 -I scripts/check-doc-links.py

# Strict server-side dry run of samples/ and the YAML in docs/ (nothing is created).
# Needs KUBECONFIG (services), SAMPLES_NS (an existing tenant namespace) and optionally OC.
check-manifests:
	OC=$${OC:-oc} python3 -I scripts/check-manifests.py samples docs architecture/docs/tutorial/09-hybrid-networks.md

# --- be-foundations ---
check-includes:
	python3 -I scripts/check-includes.py

crds:
	python3 -I scripts/gen-crds.py

crds-check:
	python3 -I scripts/gen-crds.py --check

# Schema replay (tests/schema-replay/README.md): OBJECTS = an export.sh objects dir; BASELINE_CRDS optional
REPLAY_BACKEND ?= envtest
replay:
	@test -n "$(OBJECTS)" || { echo "usage: make replay OBJECTS=<dir> [BASELINE_CRDS=<dir>] [REPLAY_BACKEND=envtest|kind]"; exit 2; }
	python3 -I tests/schema-replay/replay.py --objects $(OBJECTS) --crds operator/primary/helm/templates/crds.yaml \
	  --backend $(REPLAY_BACKEND) $(if $(BASELINE_CRDS),--baseline-crds $(BASELINE_CRDS))
# SAR matrix (tests/rbac-access/README.md) against the current KUBECONFIG: ENTITY = a tenant, PROFILE optional
sar-matrix:
	@test -n "$(ENTITY)" || { echo "usage: make sar-matrix ENTITY=<tenant> [PROFILE=generated|negative|automation]"; exit 2; }
	python3 -I tests/rbac-access/sar_matrix.py --entity $(ENTITY) $(if $(PROFILE),--profile $(PROFILE))
# --- end be-foundations ---
