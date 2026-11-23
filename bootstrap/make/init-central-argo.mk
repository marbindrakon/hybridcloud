##@ Bootstrap Cluster — Layer 1: Argo CD

.PHONY: init-central-argo
init-central-argo: check-env-central ## Install OpenShift GitOps operator and wait for Argo CD (no secrets or ApplicationSet)
	@echo "$(BOLD)Logging in to central cluster...$(RESET)"
	@$(call sovereign_login_central)
	$(call ok,Logged in to central cluster)
	@echo "$(BOLD)Installing OpenShift GitOps operator (bootstrap.operator)...$(RESET)"
	@if [ "$$(oc get namespace openshift-gitops -o jsonpath='{.status.phase}' 2>/dev/null)" = "Terminating" ]; then \
	  echo "  Waiting for openshift-gitops namespace to finish terminating..."; \
	  WAIT=0; \
	  while oc get namespace openshift-gitops >/dev/null 2>&1; do \
	    if [ $$WAIT -ge 60 ] && oc get argocd openshift-gitops -n openshift-gitops >/dev/null 2>&1; then \
	      echo "  Clearing ArgoCD finalizer (GitopsService-managed instance)..."; \
	      oc patch argocd openshift-gitops -n openshift-gitops --type=merge -p '{"metadata":{"finalizers":null}}' >/dev/null; \
	    fi; \
	    sleep 5; \
	    WAIT=$$((WAIT + 5)); \
	  done; \
	fi
	@if oc get namespace openshift-gitops-operator >/dev/null 2>&1; then \
	  if [ "$$(oc get namespace openshift-gitops-operator -o jsonpath='{.metadata.labels.app\.kubernetes\.io/managed-by}' 2>/dev/null)" != "Helm" ]; then \
	    echo "  Adopting pre-existing openshift-gitops-operator namespace for Helm..."; \
	    oc label namespace openshift-gitops-operator app.kubernetes.io/managed-by=Helm --overwrite >/dev/null && \
	    oc annotate namespace openshift-gitops-operator \
	      meta.helm.sh/release-name=sovereign-init \
	      meta.helm.sh/release-namespace=openshift-gitops-operator --overwrite >/dev/null; \
	  fi; \
	fi
	@EXISTING_CSV=$$(oc get csv -A -o jsonpath='{.items[?(@.spec.displayName=="Red Hat OpenShift GitOps")].metadata.name}' 2>/dev/null | tr ' ' '\n' | sort -u | head -1); \
	if [ -n "$$EXISTING_CSV" ]; then \
	  echo "  GitOps operator already installed globally ($$EXISTING_CSV) — skipping OLM subscription"; \
	  echo "  Waiting for OLM to create openshift-gitops-operator namespace..."; \
	  for i in $$(seq 1 20); do \
	    NS_PHASE=$$(oc get namespace openshift-gitops-operator -o jsonpath='{.status.phase}' 2>/dev/null); \
	    if [ "$$NS_PHASE" = "Active" ]; then break; fi; \
	    sleep 3; \
	  done; \
	  echo "  Adopting openshift-gitops-operator namespace for Helm..."; \
	  oc label namespace openshift-gitops-operator app.kubernetes.io/managed-by=Helm --overwrite >/dev/null 2>&1 || true; \
	  oc annotate namespace openshift-gitops-operator \
	    meta.helm.sh/release-name=sovereign-init \
	    meta.helm.sh/release-namespace=openshift-gitops-operator --overwrite >/dev/null 2>&1 || true; \
	  helm upgrade --install sovereign-init helm/init \
	    --namespace openshift-gitops-operator \
	    --create-namespace \
	    $(SOVEREIGN_INIT_BOOTSTRAP_OPERATOR) \
	    --set gitopsOperator.installOperator=false \
	    --wait --timeout=5m; \
	  if oc get argocd openshift-gitops -n openshift-gitops >/dev/null 2>&1; then \
	    if [ "$$(oc get argocd openshift-gitops -n openshift-gitops -o jsonpath='{.metadata.labels.app\.kubernetes\.io/managed-by}' 2>/dev/null)" != "Helm" ]; then \
	      echo "  Adopting pre-existing ArgoCD instance for Helm management..."; \
	      oc patch argocd openshift-gitops -n openshift-gitops --type=merge -p \
	        '{"metadata":{"labels":{"app.kubernetes.io/managed-by":"Helm"},"annotations":{"meta.helm.sh/release-name":"sovereign-init","meta.helm.sh/release-namespace":"openshift-gitops-operator"}}}' >/dev/null; \
	    fi; \
	  fi; \
	else \
	  helm upgrade --install sovereign-init helm/init \
	    --namespace openshift-gitops-operator \
	    --create-namespace \
	    $(SOVEREIGN_INIT_BOOTSTRAP_OPERATOR) \
	    --wait --timeout=5m && \
	  echo "$(BOLD)Waiting for OpenShift GitOps operator CSV...$(RESET)" && \
	  CSV="" && \
	  for i in $$(seq 1 60); do \
	    CSV=$$(oc get subscriptions.operators.coreos.com/openshift-gitops-operator -n openshift-gitops-operator -o jsonpath='{.status.installedCSV}' 2>/dev/null); \
	    if [ -z "$$CSV" ]; then \
	      CSV=$$(oc get csv -n openshift-gitops-operator -o jsonpath='{.items[?(@.spec.displayName=="Red Hat OpenShift GitOps")].metadata.name}' 2>/dev/null); \
	    fi; \
	    [ -n "$$CSV" ] && break; \
	    sleep 5; \
	  done && \
	  if [ -z "$$CSV" ]; then \
	    printf "  $(RED)✗$(RESET)  openshift-gitops-operator subscription has no installedCSV after 5 minutes\n"; \
	    exit 1; \
	  fi && \
	  oc wait --for=jsonpath='{.status.phase}'=Succeeded "csv/$$CSV" -n openshift-gitops-operator --timeout=15m && \
	  $(call ok_print,OpenShift GitOps operator ready: $$CSV); \
	fi
	@echo "$(BOLD)Waiting for GitopsService to create openshift-gitops namespace...$(RESET)"
	@for i in $$(seq 1 60); do \
	  if [ "$$(oc get namespace openshift-gitops -o jsonpath='{.status.phase}' 2>/dev/null)" = "Active" ]; then break; fi; \
	  sleep 5; \
	done
	@if [ "$$(oc get namespace openshift-gitops -o jsonpath='{.status.phase}' 2>/dev/null)" != "Active" ]; then \
	  printf "  $(RED)✗$(RESET)  openshift-gitops namespace not Active after 5 minutes\n"; \
	  exit 1; \
	fi
	@echo "$(BOLD)Waiting for Argo CD server...$(RESET)"
	@oc wait --for=condition=Available deployment/openshift-gitops-server -n openshift-gitops --timeout=15m
	$(call ok_print,Argo CD server available)
	@echo "$(BOLD)Configuring Argo CD cmdParams...$(RESET)"
	@oc patch argocd openshift-gitops -n openshift-gitops --type=merge \
	  -p '{"spec":{"cmdParams":{"controller.sync.timeout.seconds":"600"}}}' > /dev/null
	@echo "$(BOLD)Removing default Argo CD resource reservations...$(RESET)"
	@for component in controller server repo applicationSet dex redis; do \
	  oc patch argocd openshift-gitops -n openshift-gitops --type=json \
	    -p "[{\"op\":\"remove\",\"path\":\"/spec/$$component/resources\"}]" 2>/dev/null || true; \
	done
	@echo "$(BOLD)Patching Argo CD: serverSideDiff + clusterview exclusions (RHACM schema fix)...$(RESET)"
	@oc patch argocd openshift-gitops -n openshift-gitops --type=merge -p \
	  '{"spec":{"extraConfig":{"resource.compareoptions":"serverSideDiff: true\n"},"resourceExclusions":"- apiGroups:\n  - clusterview.open-cluster-management.io\n  clusters:\n  - \"*\"\n  kinds:\n  - \"*\"\n- apiGroups:\n  - tekton.dev\n  clusters:\n  - \"*\"\n  kinds:\n  - TaskRun\n  - PipelineRun\n- apiGroups:\n  - internal.open-cluster-management.io\n  clusters:\n  - \"*\"\n  kinds:\n  - ManagedClusterInfo\n"}}' > /dev/null
	@echo "$(BOLD)Restarting Argo CD application-controller to rebuild cluster cache...$(RESET)"
	@oc rollout restart statefulset/openshift-gitops-application-controller -n openshift-gitops > /dev/null
	@oc rollout status statefulset/openshift-gitops-application-controller -n openshift-gitops --timeout=5m > /dev/null
	$(call ok,Argo CD ready — next: make init-central-secrets)
