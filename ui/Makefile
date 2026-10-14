# Hybrid Sovereign UI — build targets

SHELL := /bin/bash
NPM := npm
REGISTRY ?= $(or $(OCI_HOST),quay.io/gauravshankar)
ADMIN_DASH_TAG ?= 2.0.27
TENANT_DASH_TAG ?= 5.0.28
ADMIN_PLUGIN_TAG ?= 1.2.38
TENANT_PLUGIN_TAG ?= 1.3.32

.PHONY: help install build build-shared build-admin build-tenant \
        build-admin-plugin build-tenant-plugin typecheck dev-admin dev-tenant clean \
        images push-images image-admin-dash image-tenant-dash image-admin-plugin image-tenant-plugin

help: ## Show available targets
	@grep -E '^[a-zA-Z0-9_-]+:.*##' $(MAKEFILE_LIST) | awk -F ':|##' '{printf "  %-22s %s\n", $$1, $$3}'

install: ## Install all workspace dependencies
	$(NPM) install

build-shared: ## Build @hybridsovereign/shared
	$(NPM) run build -w @hybridsovereign/shared

build-admin: build-shared ## Build admin dashboard
	$(NPM) run build -w @hybridsovereign/admin-dashboard

build-tenant: build-shared ## Build tenant dashboard
	$(NPM) run build -w @hybridsovereign/tenant-dashboard

build-admin-plugin: build-shared ## Build admin console plugin
	$(NPM) run build -w @hybridsovereign/admin-console-plugin

build-tenant-plugin: build-shared ## Build tenant console plugin
	$(NPM) run build -w @hybridsovereign/tenant-console-plugin

build: build-shared build-admin build-tenant build-admin-plugin build-tenant-plugin ## Build all packages

typecheck: ## Typecheck all packages
	$(NPM) run typecheck --workspaces --if-present

dev-admin: ## Start admin dashboard dev server (port 3000)
	$(NPM) run dev -w @hybridsovereign/admin-dashboard

dev-tenant: ## Start tenant dashboard dev server (port 3001)
	$(NPM) run dev -w @hybridsovereign/tenant-dashboard

image-admin-plugin: build-admin-plugin ## Build admin console plugin image
	podman build -f Containerfile.plugin \
	  --build-arg PLUGIN_DIST=packages/admin-console-plugin/dist \
	  -t $(REGISTRY)/sovereign-admin-plugin:$(ADMIN_PLUGIN_TAG) .

image-tenant-plugin: build-tenant-plugin ## Build tenant console plugin image
	podman build -f Containerfile.plugin \
	  --build-arg PLUGIN_DIST=packages/tenant-console-plugin/dist \
	  -t $(REGISTRY)/sovereign-tenant-plugin:$(TENANT_PLUGIN_TAG) .

image-admin-dash: build-admin ## Build admin dashboard image
	podman build -f Containerfile.dashboard \
	  --build-arg APP_PACKAGE=admin-dashboard \
	  -t $(REGISTRY)/sovereign-cloud-dashboard:$(ADMIN_DASH_TAG) .

image-tenant-dash: build-tenant ## Build tenant dashboard image
	podman build -f Containerfile.dashboard \
	  --build-arg APP_PACKAGE=tenant-dashboard \
	  -t $(REGISTRY)/tenancy-dashboard:$(TENANT_DASH_TAG) .

images: image-admin-plugin image-tenant-plugin image-admin-dash image-tenant-dash ## Build all UI images

push-images: images ## Push all UI images to Quay
	podman push $(REGISTRY)/sovereign-admin-plugin:$(ADMIN_PLUGIN_TAG)
	podman push $(REGISTRY)/sovereign-tenant-plugin:$(TENANT_PLUGIN_TAG)
	podman push $(REGISTRY)/sovereign-cloud-dashboard:$(ADMIN_DASH_TAG)
	podman push $(REGISTRY)/tenancy-dashboard:$(TENANT_DASH_TAG)

clean: ## Remove build artifacts and node_modules
	rm -rf node_modules packages/*/node_modules packages/*/dist
