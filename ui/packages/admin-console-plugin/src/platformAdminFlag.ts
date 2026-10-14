import { useEffect } from 'react';
import { consoleFetchJSON } from '@openshift-console/dynamic-plugin-sdk';

/** Set when the user may list Hybrid Fabrics in the platform namespace, i.e. administers the platform. */
export const PLATFORM_ADMIN_FLAG = 'HYBRIDSOVEREIGN_PLATFORM_ADMIN';

const PLATFORM_NS = 'sovereign-cloud';

/**
 * console.flag/hookProvider: the admin sections of the Sovereign Cloud perspective require this flag, so tenants
 * (who share the perspective with the tenant plugin) see only their own sections.
 */
export const usePlatformAdminFlag = (setFeatureFlag: (flag: string, enabled: boolean) => void): void => {
  useEffect(() => {
    let cancelled = false;
    consoleFetchJSON
      .post('/api/kubernetes/apis/authorization.k8s.io/v1/selfsubjectaccessreviews', {
        apiVersion: 'authorization.k8s.io/v1',
        kind: 'SelfSubjectAccessReview',
        spec: {
          resourceAttributes: {
            group: 'hybridsovereign.redhat',
            resource: 'hybridfabrics',
            verb: 'list',
            namespace: PLATFORM_NS,
          },
        },
      })
      .then((review: { status?: { allowed?: boolean } }) => {
        if (!cancelled) setFeatureFlag(PLATFORM_ADMIN_FLAG, !!review?.status?.allowed);
      })
      .catch(() => {
        if (!cancelled) setFeatureFlag(PLATFORM_ADMIN_FLAG, false);
      });
    return () => {
      cancelled = true;
    };
  }, [setFeatureFlag]);
};
