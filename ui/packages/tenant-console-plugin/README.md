# Tenant Console Plugin

OpenShift 4.x dynamic console plugin for tenant self-service views.

## Extensions

- Navigation section: **My Sovereign Cloud**
- Routes: the tenant pages of the shared navigation registry (`shared/src/nav/registry.ts`), rendered by the shared page library

## Build

```bash
npm run build -w @hybridsovereign/tenant-console-plugin
```

Output is packaged for deployment via the `sovereign-tenant-plugin` Helm chart.
