# Hybrid Sovereign UI

PatternFly 6 monorepo for Hybrid Sovereign Cloud dashboards (React 18) and OpenShift console plugins (React 17).

## Packages

| Package | Description |
|---------|-------------|
| `@hybridsovereign/shared` | CRD TypeScript types, K8s API hooks, theme provider |
| `@hybridsovereign/admin-dashboard` | Platform operator dashboard (React 18 + Vite) |
| `@hybridsovereign/tenant-dashboard` | Tenant self-service dashboard |
| `@hybridsovereign/admin-console-plugin` | OCP 4.x admin console dynamic plugin |
| `@hybridsovereign/tenant-console-plugin` | OCP 4.x tenant console dynamic plugin |

## CRD Types

All types target `hybridsovereign.redhat/v1alpha1`:

Entity, Team, Assignment, Project, Persona, PlatformOpenshift, CloudOSO, CloudAWS, OpenStackMigration, Rbac, RbacConfig, AAPOrg, AAPConfig, QuayOrg, QuayConfig, Vault, VaultKV

## Quick Start

```bash
cd hybridcloud/ui
make install
make build
```

### Development servers

```bash
make dev-admin    # http://localhost:3000
make dev-tenant   # http://localhost:3001
```

## Architecture

```
ui/
├── packages/
│   ├── shared/                 # Types, hooks, scope, nav registry, pages, shell, server modules
│   ├── admin-dashboard/        # Platform pages + tenant pages (read-only in All tenants)
│   ├── tenant-dashboard/       # Self-service forms + scoped lists
│   ├── admin-console-plugin/   # OCP console — platform nav
│   └── tenant-console-plugin/  # OCP console — tenant nav
├── package.json                # npm workspaces root
└── Makefile
```

Dashboards proxy K8s API calls to `/api/k8s` (backed by the dashboard K8s proxy server in production).

### One page library, one navigation registry

The four hosts (tenant dashboard, admin dashboard, tenant console plugin,
admin console plugin) render the same pages from `packages/shared`:

| Module | What it does |
|---|---|
| `shared/src/nav/registry.ts` | Every page: id, label key, section, route, kinds, persona, scopes, required verbs, create form. Nav, routes, breadcrumbs, page titles and legacy redirects come from it. Add a page here, never in a host. |
| `shared/src/nav/paths.ts` | URL forms: dashboards `/t/<tenant>/<route>`, `/all/<route>`, `/platform/<route>`; console `/hybridsovereign/<route>/ns/<namespace>`, `.../all-namespaces`, `/hybridsovereign/platform/<route>`. Old paths redirect through `LEGACY_ROUTES`. |
| `shared/src/scope` | Identity (SelfSubjectReview), tenants from `<tenant>.<group>` groups confirmed by an access review, platform reader/admin, the tenant selector, the no-role and no-access states. There is no default tenant. |
| `shared/src/access` | Permission gate (`/api/permissions` on the dashboards, access reviews on the console), `usePermission`, `GatedButton`, `ForbiddenState`, the error texts, role hints generated from the tenant role catalogue. |
| `shared/src/shell` | Dashboard masthead (tenant selector, Help, theme, language, account menu with Sign out) and the frame. |
| `shared/src/pages` | List, detail and create pages for any registry entry, the tenant home, the network map, integrations, and the console page that serves every console route. |
| `shared/server` | Node modules both dashboard servers use (copied into the images): upstream requests with the caller's token, the 4xx passthrough / 502 helper, the raw API proxy, `/api/permissions`, the platform gate, session end and `/api/ui-config`. |

Generated files (checked by `npm run test:shell`):

- `packages/*-console-plugin/console-extensions.json`: `npm run gen:console` (registry + English labels + `console-extensions.base.json`).
- `packages/shared/src/access/roleHints.generated.ts`: `npm run gen:role-hints` (from `eda/rulebooks/roles/entity_provision/vars/main.yml`).

Checks: `npm run typecheck:baseline` (no new TypeScript errors against
`typecheck-baseline.txt`), `npm run test:shell` (unit tests of the shell,
server modules and generated files), `scripts/tests/admin-dashboard-authz.sh`
(live: the admin dashboard answers only platform administrators).

Installation settings of the dashboards (chart values, empty by default):
`SHC_HELP_BASE_URL` (documentation for the Help menu), `SHC_CONSOLE_URL`,
`oauthProxy.logoutUrl` (where Sign out sends the browser), and for the admin
dashboard `oauthProxy.openshiftSar` (the access review that admits platform
administrators).

## Migration Notes

This monorepo replaces the legacy `user_dashboard` and `tenancy_dashboard` React apps and console plugin sources. Helm chart packaging remains in `bootstrap/helm/charts/`.
