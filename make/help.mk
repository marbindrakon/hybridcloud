.PHONY: help
help:
	@echo "Hybridcloud monorepo — make targets"
	@echo ""
	@echo "  make check-env          Verify required environment variables"
	@echo "  make lint-all           Helm lint all charts"
	@echo "  make build-operators    Build and push primary + namespace operators"
	@echo "  make build-iaac         Build and push iaac-git-sync image"
	@echo "  make upload-hybrid-charts Upload AMQ/operator/IAAC charts to OCI"
	@echo "  make test               Run automated test suite"
	@echo "  make check-content      Content lint: words, CRD descriptions, lab names (docs/content)"
	@echo "  make check-manifests    Server dry run of samples/ and docs/ YAML (KUBECONFIG, SAMPLES_NS)"
	@echo ""
	@echo "Bootstrap targets: see bootstrap/Makefile"
