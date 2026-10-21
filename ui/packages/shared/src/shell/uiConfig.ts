/** Installation settings served by the dashboard servers at /api/ui-config (empty by default). */
import { useEffect, useState } from 'react';

export interface UiConfig {
  /** Base URL of the documentation (no default: the Help menu says it is not configured). */
  helpBaseUrl?: string;
  /** OpenShift console of the services cluster (link from the account menu). */
  consoleUrl?: string;
}

let cached: Promise<UiConfig> | null = null;

export function loadUiConfig(): Promise<UiConfig> {
  if (!cached) {
    cached = fetch('/api/ui-config', { headers: { Accept: 'application/json' } })
      .then((r) => (r.ok ? (r.json() as Promise<UiConfig>) : {}))
      .catch(() => ({}));
  }
  return cached;
}

export function useUiConfig(enabled = true): UiConfig {
  const [cfg, setCfg] = useState<UiConfig>({});
  useEffect(() => {
    if (!enabled) return;
    let cancelled = false;
    void loadUiConfig().then((c) => {
      if (!cancelled) setCfg(c ?? {});
    });
    return () => {
      cancelled = true;
    };
  }, [enabled]);
  return cfg;
}

/** Help pages, relative to helpBaseUrl (the repository's docs/ tree). */
export const HELP_PAGES: { id: string; labelKey: string; path: string; platformOnly?: boolean }[] = [
  { id: 'getting-started', labelKey: 'shell.help.gettingStarted', path: 'tenant/getting-started.md' },
  { id: 'glossary', labelKey: 'shell.help.glossary', path: 'tenant/glossary.md' },
  { id: 'onboard-tenant', labelKey: 'shell.help.onboardTenant', path: 'platform/onboard-a-tenant.md', platformOnly: true },
  { id: 'all-docs', labelKey: 'shell.help.allDocs', path: 'README.md' },
];

export function helpUrl(base: string, path: string): string {
  return `${base.replace(/\/+$/, '')}/${path}`;
}
