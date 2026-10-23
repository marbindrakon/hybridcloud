import { useMemo } from 'react';
import { Entity } from '../types';
import { useK8sResourceList } from './k8s';
import { classifyError } from './errors';

/** Entity CRs are platform-owned and all live in this one namespace. */
export const ENTITY_NS = 'sovereign-cloud';

export interface EntityOption {
  /** Entity CR name, e.g. acme-corp */
  name: string;
  /** Tenant namespace the Entity owns, e.g. entity-acme-corp */
  namespace: string;
  ready?: boolean;
  description?: string;
}

/** entity-<name>, the namespace the Entity operator provisions. */
export function namespaceForEntityName(name: string): string {
  const trimmed = (name ?? '').trim();
  if (!trimmed) return '';
  return trimmed.startsWith('entity-') ? trimmed : `entity-${trimmed}`;
}

/** Inverse of namespaceForEntityName — the Entity CR name behind a tenant namespace. */
export function entityNameForNamespace(namespace: string): string {
  return (namespace ?? '').trim().replace(/^entity-/, '');
}

/**
 * Namespace of an Entity. `status.entity` is what the operator actually
 * provisioned and is authoritative; the entity-<name> convention is only the
 * fallback for an Entity that has not reconciled yet.
 */
export function entityNamespaceOf(entity: Entity): string {
  const fromStatus = entity.status?.entity;
  if (typeof fromStatus === 'string' && fromStatus.trim()) return fromStatus.trim();
  return namespaceForEntityName(entity.metadata?.name ?? '');
}

/** A 403/401 on sovereign-cloud is an access limit, not a broken page. */
function isForbidden(error: Error | null): boolean {
  if (!error) return false;
  const cls = classifyError(error);
  return cls === 'forbidden' || cls === 'unauthenticated';
}

export interface UseEntityOptionsOptions {
  enabled?: boolean;
  /**
   * Namespace the host app has already scoped the user to. Used as the single
   * option when sovereign-cloud is not listable (the usual tenant case).
   */
  fallbackNamespace?: string;
}

export interface UseEntityOptionsResult {
  options: EntityOption[];
  loading: boolean;
  /** A real failure (CRD missing, API down). A 403 is reported as `forbidden`. */
  error: Error | null;
  /** The user may not list sovereign-cloud; `options` is then the fallback, if any. */
  forbidden: boolean;
  /** Resolve an Entity name to its namespace, falling back to the convention. */
  namespaceOf: (name: string) => string;
}

/**
 * List the Entities the current user can see and resolve each to its tenant
 * namespace.
 *
 * Entities live in sovereign-cloud, which a tenant is usually forbidden to
 * list. As with useNetworkOverviewCRs, that platform-scoped read is best
 * effort: a 403 is surfaced as `forbidden` (never as `error`) and degrades to
 * the namespace the host app already scoped the user to, so the tenant
 * dashboard keeps working instead of blanking out.
 */
export function useEntityOptions(options: UseEntityOptionsOptions = {}): UseEntityOptionsResult {
  const { enabled = true, fallbackNamespace } = options;
  const list = useK8sResourceList<Entity>('Entity', { namespace: ENTITY_NS, enabled });
  const forbidden = isForbidden(list.error);

  const entityOptions = useMemo<EntityOption[]>(() => {
    const items = list.items
      .filter((e) => typeof e?.metadata?.name === 'string' && e.metadata.name.length > 0)
      .map((e) => ({
        name: e.metadata.name,
        namespace: entityNamespaceOf(e),
        ready: e.status?.ready,
        description: e.spec?.description,
      }))
      .sort((a, b) => a.name.localeCompare(b.name));
    if (items.length > 0) return items;
    // Nothing readable: offer the entity the host app already put us in, so a
    // tenant who can only see their own namespace still gets a usable control.
    const ns = (fallbackNamespace ?? '').trim();
    if (!ns) return [];
    return [{ name: entityNameForNamespace(ns), namespace: ns }];
  }, [list.items, fallbackNamespace]);

  const namespaceOf = useMemo(() => {
    const byName = new Map(entityOptions.map((o) => [o.name, o.namespace]));
    return (name: string): string => byName.get(name) ?? namespaceForEntityName(name);
  }, [entityOptions]);

  return {
    options: entityOptions,
    loading: list.loading,
    error: forbidden ? null : list.error,
    forbidden,
    namespaceOf,
  };
}
