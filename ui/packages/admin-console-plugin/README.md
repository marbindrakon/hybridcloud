# Admin Console Plugin

OpenShift 4.x dynamic console plugin for platform operator views.

## Extensions

- Navigation section: **Hybrid Sovereign**
- Routes: the platform pages of the shared navigation registry (`shared/src/nav/registry.ts`), rendered by the shared page library

## Build

```bash
npm run build -w @hybridsovereign/admin-console-plugin
```

Output is packaged for deployment via the `sovereign-admin-plugin` Helm chart.
