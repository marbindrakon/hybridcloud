import { consoleFetch } from '@openshift-console/dynamic-plugin-sdk';
import { configureK8sClient, configurePermissionsClient } from '@hybridsovereign/shared';

/**
 * Must run in every exposed console page module (MF codeRef chunks).
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
