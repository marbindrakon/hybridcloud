/**
 * The tenant's profile (TenantProfile `shc` in the tenant namespace): display
 * name, contacts and the sites the tenant may use (OD9). Tenants can read it;
 * they cannot list the platform's sites. The platform writes the object when it
 * sets up the tenant: until then `notReady` is true; `denied` means the caller
 * may not read it. Pickers offer no typed site name in either case.
 */
import { useMemo } from 'react';
import { TENANT_PROFILE_NAME, type TenantProfile, type TenantProfileSite, type TenantSiteType } from '../types';
import { useK8sResource } from './k8s';
import { classifyError } from './errors';

export interface UseTenantProfileResult {
  profile: TenantProfile | null;
  /** The profile was read. */
  available: boolean;
  loading: boolean;
  /** The profile does not exist yet (404): the tenant is still being set up. */
  notReady: boolean;
  /** The caller may not read the profile (403). */
  denied: boolean;
  /** Any other failure (404 and 403 are reported by notReady and denied). */
  error: Error | null;
  /** Sites of the profile, optionally of some types, Available ones first. */
  sites: (types?: TenantSiteType[]) => TenantProfileSite[];
}

export function sitesOfProfile(profile: TenantProfile | null | undefined, types?: TenantSiteType[]): TenantProfileSite[] {
  const all = profile?.status?.sites ?? [];
  return all
    .filter((s) => s && typeof s.name === 'string' && (!types?.length || types.includes(s.type)))
    .sort((a, b) => (a.state === b.state ? a.name.localeCompare(b.name) : a.state === 'Available' ? -1 : 1));
}

export function useTenantProfile(namespace: string | undefined, opts: { enabled?: boolean } = {}): UseTenantProfileResult {
  const enabled = (opts.enabled ?? true) && !!namespace;
  const { resource, loading, error } = useK8sResource<TenantProfile & { spec: object }>('TenantProfile', TENANT_PROFILE_NAME, {
    namespace,
    enabled,
  });
  return useMemo(() => {
    const cls = error ? classifyError(error) : null;
    return {
      profile: (resource as TenantProfile | null) ?? null,
      available: !!resource && !error,
      loading: enabled && loading && !resource,
      notReady: cls === 'notFound',
      denied: cls === 'forbidden',
      error: cls === 'notFound' || cls === 'forbidden' ? null : error,
      sites: (types?: TenantSiteType[]) => sitesOfProfile(resource as TenantProfile | null, types),
    };
  }, [resource, loading, error, enabled]);
}
