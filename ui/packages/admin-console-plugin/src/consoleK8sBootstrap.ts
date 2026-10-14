import { consoleFetch } from '@openshift-console/dynamic-plugin-sdk';
import { configureK8sClient, configurePermissionsClient } from '@hybridsovereign/shared';

/**
 * Must run in every exposed console page module.
 * OpenShift loads codeRefs as separate MF chunks — plugin.ts side effects do not
 * run for those chunks, so without this the client stays on default `/api/k8s`
 * and Overview KPIs come back empty.
 */
configureK8sClient({
  baseUrl: '/api/kubernetes',
  fetchFn: consoleFetch as unknown as typeof fetch,
  apiStyle: 'raw',
});

configurePermissionsClient('/api/kubernetes', {
  style: 'ssar',
  fetchFn: consoleFetch as unknown as typeof fetch,
});
