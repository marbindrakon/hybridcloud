/** Installation settings served by the dashboard servers at /api/ui-config (empty by default). */
import { useEffect, useState } from 'react';

export interface UiConfig {
  /** Base URL of the documentation (no default: the Help menu says it is not configured). */
  helpBaseUrl?: string;
  /** OpenShift console of the services cluster (link from the account menu). */
  consoleUrl?: string;
  /** Link to an automation job, with "{jobId}" (no default: the admin's diagnostics show the job id without a link). */
  automationJobUrlTemplate?: string;
  /** The platform namespace of the services cluster, where the platform-health banner reads (admin dashboard). */
  platformNamespace?: string;
  /** The longest report interval the platform-health banner believes, in minutes (null: the banner's default). */
  platformHealthMaxReportIntervalMinutes?: number | null;
}

let cached: Promise<UiConfig> | null = null;
let configUrl = '/api/ui-config';

/** Where the settings are read (a console plugin reads the dashboard's through its console proxy). */
export function configureUiConfigUrl(url: string): void {
  configUrl = url;
  cached = null;
}

export function loadUiConfig(): Promise<UiConfig> {
  if (!cached) {
    cached = fetch(configUrl, { headers: { Accept: 'application/json' } })
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
  { id: 'access', labelKey: 'shell.help.access', path: 'tenant/access-and-delegation.md' },
  { id: 'status-reasons', labelKey: 'shell.help.statusReasons', path: 'tenant/status-reasons.md' },
  { id: 'onboard-tenant', labelKey: 'shell.help.onboardTenant', path: 'platform/onboard-a-tenant.md', platformOnly: true },
  { id: 'all-docs', labelKey: 'shell.help.allDocs', path: 'README.md' },
];

/** A help page's URL: the path joined to the base, with "../" segments resolved (pages outside docs/). */
export function helpUrl(base: string, path: string): string {
  const joined = `${base.replace(/\/+$/, '')}/${path}`;
  try {
    return new URL(joined).href;
  } catch {
    return joined;
  }
}
