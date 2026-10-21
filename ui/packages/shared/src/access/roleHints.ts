import { ROLE_HINTS, type TenantRoleKey } from './roleHints.generated';
import type { GateVerb } from './permissionGate';

const READ_VERBS: GateVerb[] = ['get', 'list'];
/** Roles that exist for one installation's own use and are never suggested. */
const NEVER_SUGGEST = new Set<string>(['BobsTeam']);

/**
 * The few roles worth naming when `verb` on `plural` is denied: the role
 * specific to the kind first, then the tenant-wide one (at most three).
 */
export function hintRoles(plural: string, verb: GateVerb): TenantRoleKey[] {
  const h = ROLE_HINTS[plural];
  if (!h) return [];
  const read = READ_VERBS.includes(verb);
  const pool = (read ? h.read : h.write).filter((k) => !NEVER_SUGGEST.has(k));
  const specific: TenantRoleKey[] = pool.filter((k) => k !== 'entityAdmin' && k !== 'auditor' && (read ? /View$/.test(k) : /Admin$/.test(k)));
  const broad: TenantRoleKey[] = read ? pool.filter((k) => k === 'auditor') : pool.filter((k) => k === 'entityAdmin');
  const rest = pool.filter((k) => !specific.includes(k) && !broad.includes(k));
  const fallback: TenantRoleKey[] = specific.length ? [] : rest.slice(0, 1);
  return [...specific.slice(0, 2), ...broad, ...fallback].slice(0, 3);
}
