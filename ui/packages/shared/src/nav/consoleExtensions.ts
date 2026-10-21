/**
 * Console extensions generated from the registry (UIS-10). Pure: given the
 * English labels, returns the nav sections, nav items and page routes each
 * console plugin declares. ui/scripts/gen-console-extensions.mjs writes them
 * into the plugins' console-extensions.json next to the hand-kept base
 * entries (perspective, flag hook providers).
 *
 * - The tenant plugin owns the tenant sections and pages; its nav hrefs are
 *   `namespaced`, so the console appends /ns/<active namespace> (or
 *   /all-namespaces) and the project selector carries the tenant.
 * - The admin plugin owns the platform sections and pages, flag-gated on
 *   HYBRIDSOVEREIGN_PLATFORM_ADMIN, and the redirects of its old paths.
 * Every route renders the plugin's single page module, which reads the entry
 * and scope from the URL.
 */
import { NAV_REGISTRY, NAV_SECTIONS, hostsOf } from './registry';
import type { NavEntry } from './types';

export const PERSPECTIVE_ID = 'hybridsovereign';
export const PLATFORM_ADMIN_FLAG = 'HYBRIDSOVEREIGN_PLATFORM_ADMIN';

export interface ConsoleExtension {
  type: string;
  properties: Record<string, unknown>;
  flags?: { required?: string[]; disallowed?: string[] };
}

export type LabelOf = (key: string) => string;

const isPlatform = (e: NavEntry) => e.persona.includes('platform') && !e.persona.includes('tenant');
const sectionId = (id: string) => `hybridsovereign-section-${id}`;
const navId = (id: string) => `hybridsovereign-nav-${id}`;

function route(path: string, codeRef: string, exact = true): ConsoleExtension {
  return { type: 'console.page/route', properties: { path, exact, component: { $codeRef: codeRef } } };
}

/** Old route prefixes each plugin keeps answering (they redirect into the registry). */
const LEGACY_PREFIXES: Record<'tenant' | 'admin', string[]> = {
  tenant: ['/hybridsovereign/tenant', '/hybridsovereign/home'],
  admin: [
    '/hybridsovereign/overview',
    '/hybridsovereign/entities',
    '/hybridsovereign/personas',
    '/hybridsovereign/services',
    '/hybridsovereign/operators',
    '/hybridsovereign/teams',
    '/hybridsovereign/projects',
    '/hybridsovereign/platforms',
    '/hybridsovereign/assignments',
    '/hybridsovereign/clouds',
    '/hybridsovereign/networking',
  ],
};

export function consoleExtensionsFor(plugin: 'tenant' | 'admin', label: LabelOf, codeRef: string): ConsoleExtension[] {
  const host = plugin === 'tenant' ? 'tenant-console' : 'admin-console';
  const entries = NAV_REGISTRY.filter((e) => hostsOf(e).includes(host) && (plugin === 'admin' ? isPlatform(e) : !isPlatform(e)));
  const out: ConsoleExtension[] = [];
  const flags = plugin === 'admin' ? { required: [PLATFORM_ADMIN_FLAG] } : undefined;

  // Sections, in registry order, only those with at least one nav item.
  for (const s of NAV_SECTIONS) {
    if (s.id === 'home') continue;
    const items = entries.filter((e) => e.section === s.id && e.nav !== false);
    if (!items.length) continue;
    out.push({
      type: 'console.navigation/section',
      properties: { id: sectionId(s.id), perspective: PERSPECTIVE_ID, name: label(s.labelKey) },
      ...(flags ? { flags } : {}),
    });
  }

  // Nav items.
  for (const e of entries) {
    if (e.nav === false) continue;
    const href = isPlatform(e)
      ? `/hybridsovereign/platform${e.route ? `/${e.route}` : ''}`
      : `/hybridsovereign/${e.route || 'home'}`;
    const props: Record<string, unknown> = {
      id: navId(e.id),
      perspective: PERSPECTIVE_ID,
      name: label(e.labelKey),
      href,
    };
    if (e.section !== 'home') props.section = sectionId(e.section);
    if (!isPlatform(e)) props.namespaced = true;
    out.push({ type: 'console.navigation/href', properties: props, ...(flags ? { flags } : {}) });
  }

  // Routes.
  for (const e of entries) {
    if (isPlatform(e)) {
      const base = `/hybridsovereign/platform${e.route ? `/${e.route}` : ''}`;
      out.push(route(base, codeRef));
      if (e.kinds?.length && !e.route.includes(':')) out.push(route(`${base}/:name`, codeRef));
      continue;
    }
    const base = `/hybridsovereign/${e.route || 'home'}`;
    out.push(route(`${base}/ns/:ns`, codeRef));
    out.push(route(`${base}/all-namespaces`, codeRef));
    if (e.kinds?.length && !e.route.includes(':')) {
      out.push(route(`${base}/ns/:ns/:name`, codeRef));
      out.push(route(`${base}/all-namespaces/:name`, codeRef));
    }
  }
  for (const prefix of LEGACY_PREFIXES[plugin]) out.push(route(prefix, codeRef, false));
  if (plugin === 'admin') {
    out.push(route('/hybridsovereign', codeRef));
    out.push(route('/hybridsovereign/create/:kind', codeRef));
  }
  return out;
}
