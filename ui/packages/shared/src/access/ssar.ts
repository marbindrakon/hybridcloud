/**
 * SelfSubjectAccessReview through the host's raw API proxy (`/api/k8s` on the
 * dashboards, `/api/kubernetes` with consoleFetch on the console), with the
 * caller's own token. Used by identity resolution and the console permission
 * gate; the dashboards batch their checks through `/api/permissions`.
 */
import { getK8sClientConfig, k8sFetchJson } from '../hooks/k8s';
import { API_GROUP } from '../types';

export interface ResourceAttributes {
  namespace?: string;
  group?: string;
  resource: string;
  subresource?: string;
  name?: string;
  verb: string;
}

export function rawApiBase(): string {
  return getK8sClientConfig().baseUrl ?? '/api/k8s';
}

/** True when allowed; false when denied or when the review itself failed (fail closed). */
export async function selfSubjectAccessReview(attrs: ResourceAttributes): Promise<boolean> {
  const body = {
    apiVersion: 'authorization.k8s.io/v1',
    kind: 'SelfSubjectAccessReview',
    spec: {
      resourceAttributes: {
        group: attrs.group ?? API_GROUP,
        resource: attrs.resource,
        verb: attrs.verb,
        ...(attrs.namespace && attrs.namespace !== '-' && attrs.namespace !== '*' ? { namespace: attrs.namespace } : {}),
        ...(attrs.subresource ? { subresource: attrs.subresource } : {}),
        ...(attrs.name ? { name: attrs.name } : {}),
      },
    },
  };
  try {
    const res = await k8sFetchJson<{ status?: { allowed?: boolean } }>(
      `${rawApiBase()}/apis/authorization.k8s.io/v1/selfsubjectaccessreviews`,
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) },
      { kind: 'SelfSubjectAccessReview', verb: 'create' },
    );
    return !!res?.status?.allowed;
  } catch {
    return false;
  }
}
