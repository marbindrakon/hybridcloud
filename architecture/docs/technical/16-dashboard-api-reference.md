# Dashboard API Reference

## Overview

Both standalone dashboards (admin: `ui/packages/admin-dashboard`, tenant: `ui/packages/tenant-dashboard`) have a
small Node.js server that serves the page and forwards API calls to the Kubernetes API of the services cluster.
Every route under `/api/` is behind the `ose-oauth-proxy` sidecar; unauthenticated requests never reach the server.

## Authentication and authorization

- The oauth-proxy signs the user in and passes the user's OAuth access token in `X-Forwarded-Access-Token`
  (the console plugins' proxy passes it as a bearer `Authorization` header).
- **Every Kubernetes API call uses the signed-in user's token**, on every route. The servers never call the API with
  the pod's ServiceAccount token for a user request, and they do not impersonate. What a user may do is exactly what
  Kubernetes RBAC allows that user.
- Admin dashboard: the oauth-proxy runs with `--openshift-sar` (only platform administrators get past it), and the
  server re-checks per request (`requirePlatform`, a SelfSubjectAccessReview with the caller's token). Other users
  get `403`.
- A missing or expired token answers `401` ("Your session ended. Sign in again.").

## Errors

- Every `4xx` from the Kubernetes API passes through unchanged, with its `Status` body (`reason`, `message`,
  `details.causes[]` for field errors), so the UI can show what was refused and why.
- A `5xx` answer or a transport error becomes `502` with a short reference id; the details are logged on the server
  under that reference, not sent to the browser.
- An unknown `/api/` path answers `404` with a `Status` body.

## Admin dashboard server

All `/api/` routes require a platform administrator.

| Endpoint | Method | Description |
|---|---|---|
| `/healthz` | GET | Liveness/readiness probe (no authentication) |
| `/api/user` | GET | The signed-in user (from the forwarded headers) |
| `/api/permissions` | GET | The caller's permissions (SelfSubjectAccessReviews), used to hide actions the user may not take |
| `/api/k8s/*` | any | Kubernetes API proxy with the caller's token |
| `/api/uihealth/probe` | POST | Runs the endpoint health checks from the dashboard pod |
| `/api/ui-config` | GET | Display settings of this installation (help and console links) |
| `/api/session/end` | POST | Signs the user out: revokes the caller's own OAuth access token |

## Tenant dashboard server

| Endpoint | Method | Description |
|---|---|---|
| `/healthz` | GET | Liveness/readiness probe |
| `/api/user` | GET | The signed-in user |
| `/api/permissions` | GET | The caller's permissions |
| `/api/ui-config` | GET | Display settings of this installation |
| `/api/session/end` | POST | Signs the user out |
| `/api/k8s/*` | any | Kubernetes API proxy with the caller's token. Responses for OpenStack environments and OpenShift clusters go through the tenant status allow-list (platform-only status fields are removed); watches on those two kinds are refused. |
| `/api/<plural>`, `/api/<plural>/:name` | GET, POST, PUT/PATCH, DELETE | Curated routes per tenant kind (for example `/api/hybridnetworks`, `/api/cloudvirts`, `/api/teams`), all with the caller's token. Writes are rate limited. |

The pages come from one shared library used by both dashboards and the console plugins; see
[15-sovereign-dashboard.md](15-sovereign-dashboard.md) and [20-tenancy-dashboard.md](20-tenancy-dashboard.md).
