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

.PHONY: help check-env lint-all build-operators build-iaac upload-hybrid-charts check-content
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
