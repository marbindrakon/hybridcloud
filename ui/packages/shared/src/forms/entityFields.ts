/**
 * Entity (tenant) form helpers: display name, contacts and allowed sites
 * (Entity.spec.displayName, contacts[], allowedSites[]). Pure: no React, no
 * fetches, so the create form, the edit tab and the unit tests share them.
 */
import type { CloudInfrastructure, TenantContact } from '../types';

/** Limits of the Entity CRD (keep in step with crd-entity.yaml). */
export const ENTITY_LIMITS = {
  nameMax: 56,
  displayNameMax: 64,
  contactsMax: 10,
  contactNameMax: 128,
  contactEmailMax: 254,
  contactUrlMax: 512,
  allowedSitesMax: 64,
} as const;

export type ContactProblem = 'nameRequired' | 'nameTooLong' | 'email' | 'url' | 'tooMany';

export interface CleanContactsResult {
  /** Rows with a value, trimmed; empty optional keys removed. */
  contacts: TenantContact[];
  /** Per row index of `contacts`: what is wrong (empty when the row is fine). */
  problems: Array<ContactProblem[]>;
  /** Problems of the list as a whole. */
  listProblems: ContactProblem[];
}

const EMAIL_RE = /^[^\s@]+@[^\s@]+$/;

function str(v: unknown): string {
  return typeof v === 'string' ? v.trim() : v == null ? '' : String(v).trim();
}

/**
 * The contacts the form sends: fully empty rows dropped, values trimmed, and
 * the CRD's rules checked before the server sees them (name required, URL
 * https, at most ten contacts).
 */
export function cleanContacts(rows: ReadonlyArray<Record<string, unknown>> | undefined | null): CleanContactsResult {
  const contacts: TenantContact[] = [];
  const problems: Array<ContactProblem[]> = [];
  for (const row of rows ?? []) {
    const name = str(row?.name);
    const email = str(row?.email);
    const url = str(row?.url);
    if (!name && !email && !url) continue;
    const c: TenantContact = { name };
    if (email) c.email = email;
    if (url) c.url = url;
    const p: ContactProblem[] = [];
    if (!name) p.push('nameRequired');
    else if (name.length > ENTITY_LIMITS.contactNameMax) p.push('nameTooLong');
    if (email && (email.length > ENTITY_LIMITS.contactEmailMax || !EMAIL_RE.test(email))) p.push('email');
    if (url && (!url.startsWith('https://') || url.length > ENTITY_LIMITS.contactUrlMax)) p.push('url');
    contacts.push(c);
    problems.push(p);
  }
  const listProblems: ContactProblem[] = contacts.length > ENTITY_LIMITS.contactsMax ? ['tooMany'] : [];
  return { contacts, problems, listProblems };
}

/** True when cleanContacts found nothing to fix. */
export function contactsValid(r: CleanContactsResult): boolean {
  return r.listProblems.length === 0 && r.problems.every((p) => p.length === 0);
}

export interface SitesForTenant {
  /** Sites that allow the tenant (no spec.entityRefs, or the tenant is listed), by name. */
  offered: CloudInfrastructure[];
  /** Names of sites that list other tenants only (allowedSites cannot open them). */
  notListing: string[];
}

/**
 * The site catalogue for one tenant. allowedSites only narrows the sites that
 * already allow the tenant through their spec.entityRefs, so only those are
 * offered. With no tenant name yet, sites restricted to named tenants are not
 * offered either.
 */
export function sitesForTenant(items: ReadonlyArray<CloudInfrastructure> | undefined | null, tenant: string): SitesForTenant {
  const offered: CloudInfrastructure[] = [];
  const notListing: string[] = [];
  for (const item of items ?? []) {
    const name = item?.metadata?.name;
    if (!name || !item.spec) continue;
    const refs = (item.spec.entityRefs ?? []).map((r) => r?.name).filter(Boolean);
    if (refs.length === 0 || (tenant && refs.includes(tenant))) offered.push(item);
    else notListing.push(name);
  }
  offered.sort((a, b) => a.metadata.name.localeCompare(b.metadata.name));
  notListing.sort();
  return { offered, notListing };
}

/** "Display name (location, type)" of a site, falling back to its name. */
export function siteCatalogueLabel(item: CloudInfrastructure): string {
  const spec = item.spec;
  const title = spec?.displayName && spec.displayName !== item.metadata.name ? `${spec.displayName} [${item.metadata.name}]` : item.metadata.name;
  const where = [spec?.location, spec?.type].filter(Boolean).join(', ');
  return where ? `${title} (${where})` : title;
}

/**
 * The optional Entity fields as the API takes them: a field without a value is
 * left out (allowedSites has minItems 1, so "every site" is no key at all).
 */
export function entityOptionalSpec(input: {
  displayName?: string;
  contacts?: TenantContact[];
  allowedSites?: string[];
}): { displayName?: string; contacts?: TenantContact[]; allowedSites?: string[] } {
  const out: { displayName?: string; contacts?: TenantContact[]; allowedSites?: string[] } = {};
  const dn = str(input.displayName);
  if (dn) out.displayName = dn;
  if (input.contacts?.length) out.contacts = input.contacts;
  const sites = Array.from(new Set((input.allowedSites ?? []).map(str).filter(Boolean))).sort();
  if (sites.length) out.allowedSites = sites;
  return out;
}
