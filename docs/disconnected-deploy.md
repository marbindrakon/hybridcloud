# Disconnected (air-gap) deploy checklist

**Status**: Partial — platform is *designed* for sovereignty but is **not** turnkey air-gap until mirrors and catalogs are in place.

This document is the operational companion to the cleanup audit. Connected lab may still use public registries for *image builds*; the **runtime cluster** must not depend on the public internet.

## Runtime must be internal

| Dependency | Connected lab (today) | Disconnected target |
|------------|----------------------|---------------------|
| Helm charts | Private Quay via `OCI_REGISTRY` | Same — only internal registry |
| Operator / UI / DE images | Private Quay | Same |
| App-of-apps / primary / iaac | OCI charts under `oci.registry` | OCI only (no public Git) |
| EDA / AAP rulebook SCM | In-cluster Gitea `eda-lab` | Gitea only — mirror from monorepo `eda/` |
| Tenancy CRs | Gitea `tenancy_repo` | Same |
| Cluster builds | Gitea `cluster_builds` | Same |
| OLM operators | `redhat-operators` / `community-operators` | Mirrored `CatalogSource` + IDMS/ICSP |
| DNS upstreams | Lab forwarders (+ optional public) | Lab / corporate recursive only |

## Build host vs cluster

1. **Connected build bastion** (optional): pull from `registry.redhat.io`, Galaxy, PyPI; build ansible-runner, DEs, UI, operators; push to private Quay.
2. **Disconnected clusters**: pull only from private Quay + cluster registry; SCM only from Gitea; OLM only from mirrored catalogs.

Never claim “air-gap ready” until the items below are green.

## Pre-flight checklist

- [ ] `OCI_REGISTRY` / `OCI_HOST` point at the private registry; robot pull secret installed
- [ ] `make upload-all-charts` + image build/push completed into that registry
- [ ] Monorepo (or mirror) available to ArgoCD init via **Gitea or file/OCI**, not public GitHub
- [ ] Gitea repos seeded: `tenancy_repo`, `eda-lab` (contents of `hybridcloud/eda/`), `cluster_builds`
- [ ] AAP/EDA Jobs use `EDA_REPO_URL` / `RULEBOOK_REPO_URL` → Gitea (defaults in central values)
- [ ] OLM: mirrored redhat + community catalogs; Subscriptions retargeted; prefer `installPlanApproval: Manual`
- [ ] Mirrored images pinned for: oauth-proxy, ose-cli, ACS, Crunchy PGO, UBI, DE bases, operator-framework, nginx
- [ ] `allowedRegistries` limited to internal hosts (see `bootstrap/make/add-docker-repo.mk` air-gap profile)
- [ ] No `8.8.8.8` / public DNS in production DNS forwarder config
- [ ] UI CSP does not require Google Fonts (fonts are local PatternFly assets)

## Known gaps (must close for full disconnected)

1. **OLM catalog mirror** — no in-repo oc-mirror / IDMS manifests yet
2. **Ansible-runner / DE builds** — still pull Galaxy, PyPI, mirror.openshift.com, get.helm.sh at *build* time (vendor or mirror)
3. **Hard-coded lab FQDNs** in committed `bootstrap/helm/central/values.yaml` — prefer empty git defaults + `values-lab.yaml` / Vault `lab-config` (see `docs/lab-config.md`)
4. **Crunchy / some Job images** — public registry defaults; override to private Quay
<!-- retired:begin -->
5. **Event forwarder** — retired (`enabled: false`); do not gate disconnected deploy on it
<!-- retired:end -->

## Related

- `docs/lab-config.md` — topology / secrets policy
- `architecture/docs/technical/05-oci-registry.md` — Quay mirroring patterns
- `architecture/docs/technical/14-gitea.md` — Gitea image mirroring
- `tests/argocd-deploy/DEPLOYMENT_GATES.md` — sync gates (ignore retired event-forwarder)
