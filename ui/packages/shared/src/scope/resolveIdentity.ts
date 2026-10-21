/**
 * Who is signed in, and which tenants they may open (C2).
 *
 * Transport-agnostic: every call goes through the host's raw API proxy as
 * configured with configureK8sClient (`/api/k8s` on the dashboards,
 * `/api/kubernetes` with consoleFetch on the console), always with the
 * caller's own token.
 *
 * 1. SelfSubjectReview -> username and groups (OpenShift `users/~` gives the
 *    display name, and the groups on clusters without SelfSubjectReview).
 * 2. Tenant candidates from `<entity>.<group>` groups, each confirmed with a
 *    SelfSubjectAccessReview `get tenantprofiles/shc` in `entity-<e>`.
 * 3. Platform reader = `list entities -n sovereign-cloud` (sees every tenant
 *    and the All tenants scope); platform admin = `list hybridfabrics -n
 *    sovereign-cloud` (the same review the admin dashboard's proxy runs).
 */
import { getK8sClientConfig, k8sFetchJson } from '../hooks/k8s';
import { selfSubjectAccessReview, rawApiBase } from '../access/ssar';
import { API_VERSION_FULL, Entity, TENANT_PROFILE_NAME, TenantProfile } from '../types';
import { namespaceOfTenant, tenantCandidatesFromGroups } from './parseGroups';
import type { Identity, TenantRef } from './types';

export const PLATFORM_NAMESPACE = 'sovereign-cloud';

/**
 * Transitional rule: until every tenant role grants `get tenantprofiles`, a
 * candidate tenant is also confirmed when the user may list any of these
 * tenant kinds in its namespace. Remove once the roles carry the grant.
 */
export const SCOPE_CONFIRM_FALLBACK = true;
const FALLBACK_KINDS = ['cloudvirts', 'hybridnetworks', 'cloudosos', 'teams', 'rbacs', 'vaults'];

/** Upper bound of tenant candidates checked per sign-in (each costs one review). */
const MAX_CANDIDATES = 50;

interface SelfSubjectReview {
  status?: { userInfo?: { username?: string; groups?: string[] } };
}
interface OpenShiftUser {
  metadata?: { name?: string };
  fullName?: string;
  groups?: string[];
}

async function whoAmI(): Promise<{ username: string; groups: string[]; displayName?: string }> {
  const base = rawApiBase();
  let username = '';
  let groups: string[] = [];
  let displayName: string | undefined;
  try {
    const ssr = await k8sFetchJson<SelfSubjectReview>(
      `${base}/apis/authentication.k8s.io/v1/selfsubjectreviews`,
      {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ apiVersion: 'authentication.k8s.io/v1', kind: 'SelfSubjectReview' }),
      },
      { kind: 'SelfSubjectReview', verb: 'create' },
    );
    username = ssr?.status?.userInfo?.username ?? '';
    groups = ssr?.status?.userInfo?.groups ?? [];
  } catch {
    /* older API server: fall back to users/~ below */
  }
  try {
    const user = await k8sFetchJson<OpenShiftUser>(`${base}/apis/user.openshift.io/v1/users/~`, undefined, {
      kind: 'User',
      verb: 'get',
    });
    if (!username) username = user?.metadata?.name ?? '';
    if (!groups.length && Array.isArray(user?.groups)) groups = user.groups;
    if (user?.fullName) displayName = user.fullName;
  } catch {
    /* not OpenShift, or no access: the username is enough */
  }
  if (!username) throw new Error('Unable to determine the signed-in user');
  return { username, groups, displayName };
}

async function tenantProfile(namespace: string): Promise<TenantProfile | null> {
  try {
    return await k8sFetchJson<TenantProfile>(
      `${rawApiBase()}/apis/${API_VERSION_FULL}/namespaces/${encodeURIComponent(namespace)}/tenantprofiles/${TENANT_PROFILE_NAME}`,
      undefined,
      { kind: 'TenantProfile', verb: 'get', namespace },
    );
  } catch {
    return null;
  }
}

async function confirmTenant(namespace: string): Promise<{ confirmed: boolean; profileReadable: boolean }> {
  const profileReadable = await selfSubjectAccessReview({
    namespace,
    resource: 'tenantprofiles',
    name: TENANT_PROFILE_NAME,
    verb: 'get',
  });
  if (profileReadable) return { confirmed: true, profileReadable };
  if (!SCOPE_CONFIRM_FALLBACK) return { confirmed: false, profileReadable };
  const any = await Promise.all(
    FALLBACK_KINDS.map((resource) => selfSubjectAccessReview({ namespace, resource, verb: 'list' })),
  );
  return { confirmed: any.some(Boolean), profileReadable };
}

function entityNamespace(e: Entity): string {
  const fromStatus = e.status?.entity;
  if (typeof fromStatus === 'string' && fromStatus.trim()) {
    const v = fromStatus.trim();
    return v.startsWith('entity-') ? v : `entity-${v}`;
  }
  return namespaceOfTenant(e.metadata.name);
}

async function listAllTenants(): Promise<TenantRef[]> {
  try {
    const list = await k8sFetchJson<{ items?: Entity[] }>(
      `${rawApiBase()}/apis/${API_VERSION_FULL}/namespaces/${PLATFORM_NAMESPACE}/entities`,
      undefined,
      { kind: 'Entity', verb: 'list', namespace: PLATFORM_NAMESPACE },
    );
    return (list?.items ?? [])
      .filter((e) => e?.metadata?.name)
      .map((e) => ({
        name: e.metadata.name,
        namespace: entityNamespace(e),
        displayName: e.spec?.displayName || undefined,
        contacts: e.spec?.contacts,
        groups: [],
        via: 'platform' as const,
      }));
  } catch {
    return [];
  }
}

export async function resolveIdentity(): Promise<Identity> {
  // Fail early with a clear error when no client is configured.
  void getK8sClientConfig();
  const me = await whoAmI();

  const [reader, admin] = await Promise.all([
    selfSubjectAccessReview({ namespace: PLATFORM_NAMESPACE, resource: 'entities', verb: 'list' }),
    selfSubjectAccessReview({ namespace: PLATFORM_NAMESPACE, resource: 'hybridfabrics', verb: 'list' }),
  ]);

  const candidates = tenantCandidatesFromGroups(me.groups).slice(0, MAX_CANDIDATES);
  const confirmed = await Promise.all(
    candidates.map(async (c) => {
      const namespace = namespaceOfTenant(c.entity);
      const { confirmed: ok, profileReadable } = await confirmTenant(namespace);
      if (!ok) return null;
      const profile = profileReadable ? await tenantProfile(namespace) : null;
      const ref: TenantRef = {
        name: c.entity,
        namespace,
        displayName: profile?.status?.displayName || undefined,
        contacts: profile?.status?.contacts,
        groups: c.groups,
        via: 'group',
      };
      return ref;
    }),
  );
  const byName = new Map<string, TenantRef>();
  for (const t of confirmed) if (t) byName.set(t.name, t);

  if (reader) {
    for (const t of await listAllTenants()) {
      const mine = byName.get(t.name);
      if (mine) {
        byName.set(t.name, {
          ...mine,
          displayName: mine.displayName ?? t.displayName,
          contacts: mine.contacts ?? t.contacts,
          namespace: t.namespace || mine.namespace,
        });
      } else {
        byName.set(t.name, t);
      }
    }
  }

  const tenants = Array.from(byName.values()).sort((a, b) =>
    (a.displayName ?? a.name).localeCompare(b.displayName ?? b.name),
  );
  return {
    username: me.username,
    displayName: me.displayName,
    groups: me.groups,
    tenants,
    platform: { reader, admin },
  };
}

/** Display name of a tenant: its profile or Entity display name, else its id. */
export function tenantLabel(t: Pick<TenantRef, 'name' | 'displayName'> | undefined | null): string {
  if (!t) return '';
  return t.displayName?.trim() || t.name;
}
