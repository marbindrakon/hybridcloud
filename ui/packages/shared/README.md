# @hybridsovereign/shared

Shared library for Hybrid Sovereign Cloud UI packages.

## Contents

- **CRD types** — TypeScript interfaces for `hybridsovereign.redhat/v1alpha1` custom resources
- **K8s hooks** — React hooks for listing and fetching CRs via the dashboard K8s proxy
- **Theme** — PatternFly 6 `SovereignThemeProvider` with dark/light mode toggle

## Usage

```tsx
import {
  SovereignThemeProvider,
  useK8sResourceList,
  type Entity,
  API_GROUP,
} from '@hybridsovereign/shared';
```

## Build

```bash
npm run build -w @hybridsovereign/shared
```
