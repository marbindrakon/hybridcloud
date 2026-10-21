/**
 * Tenant candidates from group names (C2).
 *
 * Tenant access groups are realized as OpenShift groups `<entity>.<group>`.
 * The entity is the part before the first dot and must be a valid Entity
 * name: a DNS-1123 label of at most 56 characters without `--` (the Entity
 * API refuses dots, so the split is unambiguous). Flat legacy groups such as
 * `acme-corp-platform-engineering-admins` and system or platform groups are
 * not tenant groups.
 */

const ENTITY_NAME = /^[a-z0-9]([a-z0-9-]*[a-z0-9])?$/;
export const ENTITY_NAME_MAX = 56;

export function isEntityName(name: string): boolean {
  return (
    typeof name === 'string' &&
    name.length > 0 &&
    name.length <= ENTITY_NAME_MAX &&
    ENTITY_NAME.test(name) &&
    !name.includes('--')
  );
}

export interface TenantCandidate {
  entity: string;
  groups: string[];
}

export function tenantOfGroup(group: string): string | null {
  if (typeof group !== 'string') return null;
  const dot = group.indexOf('.');
  if (dot <= 0 || dot === group.length - 1) return null;
  const entity = group.slice(0, dot);
  return isEntityName(entity) ? entity : null;
}

/** Candidates in first-seen order, each with the groups that named it. */
export function tenantCandidatesFromGroups(groups: readonly string[]): TenantCandidate[] {
  const byEntity = new Map<string, string[]>();
  for (const g of groups ?? []) {
    const e = tenantOfGroup(g);
    if (!e) continue;
    const list = byEntity.get(e) ?? [];
    if (!list.includes(g)) list.push(g);
    byEntity.set(e, list);
  }
  return Array.from(byEntity, ([entity, gs]) => ({ entity, groups: gs }));
}

export function namespaceOfTenant(entity: string): string {
  return `entity-${entity}`;
}

export function tenantOfNamespace(namespace: string | undefined | null): string | null {
  if (!namespace || !namespace.startsWith('entity-')) return null;
  const e = namespace.slice('entity-'.length);
  return isEntityName(e) ? e : null;
}
