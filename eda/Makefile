EDA_HOST          ?= $(AAP_CENTRAL_GATEWAY_URL)
EDA_PROJECT_NAME  ?= sovereign-eda-rulebooks
EDA_PROJECT_URL   ?= https://github.com/hybrid-sovereign-cloud/hybridcloud.git
EDA_PROJECT_SCM_PATH ?= eda
EVENT_STREAM_NAME ?= sovereign-operator-events
OCI_REGISTRY_HOST ?= $(OCI_HOST)
OCI_NAMESPACE     ?= hybrid-sovereign

# Derived — EDA admin password from the services cluster
EDA_PASS = $(shell oc get secret sovereign-aap-eda-admin-password -n aap --context=services-admin -o jsonpath='{.data.password}' | base64 -d)

MANAGE = python3 scripts/manage_activations.py
MANAGE_ARGS = --eda-host $(EDA_HOST) --password "$(EDA_PASS)"

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "\nSovereign EDA — Make Targets\n\n  \033[36m%-30s\033[0m %s\n\n", "TARGET", "DESCRIPTION"} \
		/^[a-zA-Z_0-9-]+:.*?##/ { printf "  \033[36m%-30s\033[0m %s\n", $$1, $$2 }' $(MAKEFILE_LIST)

# ─── Activation Management ────────────────────────────────────────────────────

.PHONY: activations-create
activations-create: ## Create all EDA rulebook activations
	$(MANAGE) create $(MANAGE_ARGS) --event-stream $(EVENT_STREAM_NAME)

.PHONY: activations-delete
activations-delete: ## Delete all EDA rulebook activations
	$(MANAGE) delete $(MANAGE_ARGS)

.PHONY: activations-status
activations-status: ## Show status of all activations
	$(MANAGE) status $(MANAGE_ARGS)

.PHONY: activations-restart
activations-restart: ## Restart all activations (picks up latest project code)
	$(MANAGE) restart $(MANAGE_ARGS)

.PHONY: activations-recreate
activations-recreate: activations-delete ## Delete and recreate all activations
	@echo "Waiting for deletions to complete..."
	@sleep 10
	$(MANAGE) create $(MANAGE_ARGS) --event-stream $(EVENT_STREAM_NAME)

# ─── Project Management ──────────────────────────────────────────────────────

.PHONY: project-sync
project-sync: ## Sync the EDA project from GitHub
	$(MANAGE) sync $(MANAGE_ARGS) --project-name $(EDA_PROJECT_NAME)

.PHONY: deploy
deploy: project-sync activations-restart ## Push code, sync project, restart activations
	@echo "Deploy complete."

# ─── Decision Environment Builds ─────────────────────────────────────────────

DE_DIRS = entity team assignment project persona platformopenshift cloudoso cloudaws openstack-migration \
          plugin-rbac plugin-vault plugin-aap plugin-quay

.PHONY: de-build-all
de-build-all: ## Build all 13 decision environment images
	@for d in $(DE_DIRS); do \
		echo "Building DE: $$d"; \
		$(MAKE) -C $$d de-$$(echo $$d | tr '/' '-')-build || exit 1; \
	done

.PHONY: de-push-all
de-push-all: ## Push all 13 decision environment images
	@for d in $(DE_DIRS); do \
		echo "Pushing DE: $$d"; \
		$(MAKE) -C $$d de-$$(echo $$d | tr '/' '-')-push || exit 1; \
	done

.PHONY: de-build-push-all
de-build-push-all: de-build-all de-push-all ## Build and push all DE images
